"""归因结果 -> 数据服务版本契约的回归测试。

直接用内存仓库与业务服务，不依赖 HTTP 客户端；运行：
    cd backend && .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import threading
import unittest
from unittest import mock

from app.services.attribution import AttributionService
from app.services.contracts import (
    FROZEN_RETURN_FIELDS,
    ContractError,
    contracts,
)
from app.services.delivery import DeliveryService
from app.store import store


def _delivery_values(service_number: str, version: str, *, rule_ref: str = "ATTR-0003@v6", consumers: str = "增长看板、渠道结算"):
    return {
        "服务编号": service_number,
        "数据集名称": "渠道归因结果集",
        "查询参数定义": "start_date、end_date、channel",
        "返回字段": FROZEN_RETURN_FIELDS,
        "速率限制": "600 次/分钟",
        "分页最大条数": "1000",
        "版本号": version,
        "消费方应用": consumers,
        "归因规则编号": rule_ref,
    }


def _new_service(service_number: str, version: str = "v1") -> int:
    service = DeliveryService()
    entry, missing = service.create_entry(_delivery_values(service_number, version))
    assert entry is not None and not missing
    return int(entry["id"])


class ContractFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.attribution = AttributionService()
        self.delivery = DeliveryService()

    def test_frozen_fields_trace_to_same_rule_version(self) -> None:
        result = contracts.service_result("DELI-0003")
        self.assertIsNotNone(result)
        sample = result["sample_result"]
        self.assertEqual(sample["rule_version"], "ATTR-0003@v6")
        self.assertEqual(sample["attributed_users"], 18420)
        self.assertEqual(sample["attributed_value"], "926500.00")
        for field in FROZEN_RETURN_FIELDS:
            trace = result["trace"][field]
            self.assertEqual(trace["digest_ref"], "ATTR-0003@v6")
            self.assertEqual(trace["contract_id"], result["contract_id"])
            self.assertTrue(trace["digest_hash"])

    def test_resolve_from_any_return_field(self) -> None:
        for query in (
            {"rule_version": "ATTR-0003@v6"},
            {"attributed_value": "926500"},
            {"channel": "付费搜索、信息流、自然搜索"},
            {"first_touch_weight": "0.4"},
            {"last_touch_weight": "0.6"},
            {"middle_decay": "0.15"},
        ):
            resolved = contracts.resolve_result(**query)
            self.assertIsNotNone(resolved, query)
            self.assertEqual(resolved["rule_ref"], "ATTR-0003@v6")
            self.assertEqual(resolved["digest"]["immutable"], True)

    def test_preview_and_publish_share_same_fingerprint(self) -> None:
        service_id = _new_service("DELI-9001", "v3")
        preview, message = self.delivery.contract_preview(service_id, {})
        self.assertEqual(message, "")
        contract, _ = self.delivery.publish_contract(service_id, {}, operator="测试员")
        self.assertIsNotNone(contract)
        self.assertEqual(preview["preview_hash"], contract["preview_hash"])
        self.assertEqual(preview["digest_hash"], contract["digest_hash"])
        self.assertEqual(contract["return_fields"], FROZEN_RETURN_FIELDS)
        self.assertEqual(contract["consumer_count"], 2)
        baseline_row = next(row for row in store.rows("delivery") if row["服务编号"] == "DELI-0003")
        baseline, _ = self.delivery.contract_preview(
            int(baseline_row["id"]),
            {"版本号": "v3", "消费方应用": "增长看板、渠道结算、归因复盘"},
        )
        self.assertEqual(baseline["consumer_count"], 3)
        self.assertEqual(baseline["rule_ref"], "ATTR-0003@v6")

    def test_duplicate_publish_only_takes_effect_once(self) -> None:
        service_id = _new_service("DELI-9002", "v1")
        first, _ = self.delivery.publish_contract(service_id, {}, operator="测试员")
        second, message = self.delivery.publish_contract(service_id, {}, operator="测试员")
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second.get("duplicate"))
        self.assertIn("未再次生效", message)
        self.assertEqual(
            len([row for row in store.aux("delivery_contract") if row["service_number"] == "DELI-9002"]),
            1,
        )

    def test_incomplete_fields_leave_no_contract(self) -> None:
        service_id = _new_service("DELI-9003", "v1")
        before = len(store.aux("delivery_contract"))
        contract, message = self.delivery.publish_contract(
            service_id, {"返回字段": ["rule_version", "channel"]}, operator="测试员"
        )
        self.assertIsNone(contract)
        self.assertIn("缺少冻结字段", message)
        self.assertEqual(len(store.aux("delivery_contract")), before)

    def test_write_failure_rolls_back_half_contract(self) -> None:
        service_id = _new_service("DELI-9004", "v1")
        before = len(store.aux("delivery_contract"))
        class FailingList(list):
            def append(self, item):  # noqa: D401 - 模拟字段写入失败
                raise RuntimeError("磁盘写入失败")

        original_aux = store.aux

        def failing_aux(name: str):
            if name == "delivery_contract":
                return FailingList(original_aux(name))
            return original_aux(name)

        with mock.patch.object(store, "aux", failing_aux):
            contract, message = self.delivery.publish_contract(service_id, {}, operator="测试员")
        self.assertIsNone(contract)
        self.assertIn("写入失败", message)
        self.assertEqual(len(store.aux("delivery_contract")), before)
        self.assertIsNone(store.aux_find("delivery_contract", "service_number", "DELI-9004"))

    def test_competing_same_version_conflict_explains_reason(self) -> None:
        service_id = _new_service("DELI-9005", "v1")
        first, _ = self.delivery.publish_contract(service_id, {}, operator="测试员A")
        conflict, message = self.delivery.publish_contract(
            service_id,
            {"归因规则编号": "ATTR-0003@v2", "消费方应用": "增长看板、渠道结算"},
            operator="测试员B",
        )
        self.assertIsNone(conflict)
        self.assertIn("冲突", message)
        self.assertIn(f"契约 {first['id']}", message)
        current = contracts.get_service_contract("DELI-9005")
        self.assertEqual(current["id"], first["id"])

    def test_concurrent_publish_yields_one_complete_contract(self) -> None:
        service_id = _new_service("DELI-9006", "v1")
        results: list[object] = []

        def worker(version: str) -> None:
            published, message = self.delivery.publish_contract(
                service_id, {"版本号": version}, operator=f"并发-{version}"
            )
            results.append((version, published["id"] if published else None, message))

        threads = [threading.Thread(target=worker, args=(version,)) for version in ("v1", "v2", "v3")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        rows = [row for row in store.aux("delivery_contract") if row["service_number"] == "DELI-9006"]
        self.assertEqual(len(rows), 3)
        current = [row for row in rows if row["current"]]
        self.assertEqual(len(current), 1)
        self.assertEqual(sum(1 for row in rows if row.get("superseded_by")), 2)

    def test_offlining_rule_keeps_published_contract(self) -> None:
        rule = next(row for row in store.rows("attribution") if row["规则编号"] == "ATTR-0003")
        rule["status"] = "已下线"
        result = contracts.service_result("DELI-0003")
        self.assertIsNotNone(result)
        self.assertEqual(result["sample_result"]["rule_version"], "ATTR-0003@v6")
        blocked, message = self.delivery.publish_contract(
            _new_service("DELI-9007", "v1"), {}, operator="测试员"
        )
        self.assertIsNone(blocked)
        self.assertIn("已下线", message)
        rule["status"] = "已发布"

    def test_service_transition_rejects_skip_and_reverse(self) -> None:
        service_id = _new_service("DELI-9008", "v1")
        entry, message = self.delivery.run_action(service_id, "发布服务", {})
        self.assertIsNone(entry)
        self.assertIn("不能跳到", message)
        self.delivery.run_action(service_id, "提交服务评审", {})
        self.delivery.run_action(service_id, "发布服务", {})
        entry, message = self.delivery.run_action(service_id, "提交服务评审", {})
        self.assertIsNone(entry)
        self.delivery.run_action(service_id, "下线服务", {})
        entry, message = self.delivery.run_action(service_id, "发布服务", {})
        self.assertIsNone(entry)
        self.assertIn("不能恢复", message)

    def test_duplicate_rule_digest_publish_only_once(self) -> None:
        rule = next(row for row in store.rows("attribution") if row["规则编号"] == "ATTR-0003")
        first = contracts.publish_rule_digest(int(rule["id"]), operator="测试员")
        second = contracts.publish_rule_digest(int(rule["id"]), operator="测试员")
        self.assertTrue(second.get("duplicate"))
        self.assertEqual(first["digest_hash"], second["digest_hash"])
        self.assertEqual(
            len([row for row in store.aux("attribution_digest") if row["rule_ref"] == "ATTR-0003@v6"]),
            1,
        )

    def test_concurrent_same_version_conflict_keeps_complete_one(self) -> None:
        service_id = _new_service("DELI-9011", "v1")
        barrier = threading.Barrier(2)
        results: list[tuple[bool, str]] = []

        def worker(rule_ref: str) -> None:
            barrier.wait()
            published, message = self.delivery.publish_contract(
                service_id, {"归因规则编号": rule_ref}, operator=f"并发-{rule_ref}"
            )
            results.append((published is not None, message))

        threads = [
            threading.Thread(target=worker, args=(rule_ref,))
            for rule_ref in ("ATTR-0003@v6", "ATTR-0003@v2")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        rows = [row for row in store.aux("delivery_contract") if row["service_number"] == "DELI-9011"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["current"])
        winners = [message for ok, message in results if ok]
        losers = [message for ok, message in results if not ok]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 1)
        self.assertIn("冲突", losers[0])
        self.assertIn(f"契约 {rows[0]['id']}", losers[0])

    def test_archived_contract_remains_traceable(self) -> None:
        service_id = _new_service("DELI-9010", "v1")
        published, _ = self.delivery.publish_contract(service_id, {}, operator="测试员")
        self.delivery.run_action(service_id, "提交服务评审", {})
        self.delivery.run_action(service_id, "发布服务", {})
        self.delivery.run_action(service_id, "下线服务", {})
        archived = contracts.service_result("DELI-9010")
        self.assertIsNotNone(archived)
        self.assertFalse(archived["current"])
        self.assertEqual(archived["sample_result"]["rule_version"], "ATTR-0003@v6")
        self.assertEqual(archived["contract_id"], published["id"])

    def test_audit_records_contract_changes(self) -> None:
        service_id = _new_service("DELI-9009", "v1")
        self.delivery.publish_contract(service_id, {}, operator="审计复核")
        audit = contracts.contract_audit()["items"]
        actions = [item["action"] for item in audit]
        self.assertIn("归因结果摘要已生成", actions)
        self.assertIn("服务契约已发布", actions)
        self.assertTrue(any(item["operator"] == "审计复核" for item in audit))


if __name__ == "__main__":
    unittest.main()
