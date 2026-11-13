"""归因结果到数据服务发布的版本契约。

维护三类只追加、可追溯的数据：

* 归因结果摘要（digest）：归因规则发布时对触点权重、回溯窗口和规则版本做一次
  不可变快照。摘要一旦生成就不再修改，规则后续停用或调整都不会改变它。
* 服务契约（contract）：数据服务发布时按摘要登记返回字段、查询参数和消费方，
  并把预览指纹一并写入发布记录。预览与发布记录拥有同一个 digest_hash /
  preview_hash，调用方可以据此证明字段来自同一规则版本。
* 审计（audit）：摘要生成、契约发布、契约替代、契约下线和被拒绝的写入都留痕。

内存实现里用一把可重入锁把“校验 -> 完整写入 -> 审计”包在一起：并发发布时
只有一份完整契约能成为当前版本，校验或字段写入失败不会留下半套契约。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
from datetime import datetime, timezone
from typing import Any

from app.seed import SEED_CONTRACT_BASELINE, SEED_ROWS
from app.store import store

# 冻结字段来自现有归因规则与数据服务链路：
#   规则版本 / 渠道 / 首次触点权重 / 末次触点权重 / 中间触点衰减 <- 归因规则字段
#   attributed_value                                     <- 按该摘要计算的归因结果
# 这六个字段是服务契约的完整集合，缺一个或混入别的字段都视为“半套契约”。
FROZEN_RETURN_FIELDS: list[str] = [
    "rule_version",
    "channel",
    "first_touch_weight",
    "last_touch_weight",
    "middle_decay",
    "attributed_value",
]

FIELD_SOURCES: dict[str, str] = {
    "rule_version": "归因规则发布摘要.规则版本",
    "channel": "归因规则.关联渠道",
    "first_touch_weight": "归因规则.首次触点权重",
    "last_touch_weight": "归因规则.末次触点权重",
    "middle_decay": "归因规则.中间触点衰减",
    "attributed_value": "归因结果集（按该摘要计算）",
}

RULE_SNAPSHOT_FIELDS = [
    "归因类型",
    "回溯窗口天",
    "触点权重",
    "首次触点权重",
    "末次触点权重",
    "中间触点衰减",
    "关联渠道",
    "结果样本用户数",
    "结果样本金额",
]

RULE_EDITABLE_FIELDS = ["规则编号", "规则版本", *RULE_SNAPSHOT_FIELDS]

SERVICE_EDITABLE_FIELDS = [
    "服务编号",
    "数据集名称",
    "查询参数定义",
    "返回字段",
    "速率限制",
    "分页最大条数",
    "版本号",
    "消费方应用",
    "归因规则编号",
]

# 服务版本只允许 草稿 -> 评审 -> 发布 -> 下线，每次只前进一步。
SERVICE_STATUS_ORDER = ["草稿", "评审", "发布", "下线"]
SERVICE_ACTIONS = {
    "提交评审": "评审",
    "提交服务评审": "评审",
    "发布": "发布",
    "发布服务": "发布",
    "下线": "下线",
    "下线服务": "下线",
}
PUBLISHED = "发布"
OFFLINE = "下线"


class ContractError(Exception):
    """契约校验失败：说明会原样写入审计，调用方也能读到同一句原因。"""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def _money(value: Any) -> str:
    """金额统一保留两位小数的字符串，摘要才不会被 926500 和 926500.00 撕成两份。"""
    text = _as_text(value)
    if not text:
        return "0.00"
    try:
        return f"{float(text):.2f}"
    except ValueError:
        return text


def _split_list(value: Any) -> list[str]:
    """把「a、b、c」「a,b,c」或多选数组统一成去重且保序的字段列表。"""
    if isinstance(value, (list, tuple)):
        raw = [str(item) for item in value]
    else:
        raw = re.split(r"[,，、\n]", str(value or ""))
    seen: list[str] = []
    for item in raw:
        text = item.strip()
        if text and text not in seen:
            seen.append(text)
    return seen


def _rule_number(rule_ref: str) -> str:
    return rule_ref.split("@", 1)[0].strip()


class ContractService:
    def __init__(self) -> None:
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 状态机
    # ------------------------------------------------------------------
    def validate_transition(
        self,
        current: str,
        action: str,
        *,
        order: list[str],
        action_rules: dict[str, str],
        module_label: str,
    ) -> str:
        if action not in action_rules:
            raise ContractError(f"动作「{action}」不属于{module_label}可执行范围")
        if current not in order:
            raise ContractError(f"{module_label}当前状态「{current}」不在允许的状态序列里")
        if current == order[-1]:
            raise ContractError(f"{module_label}已下线，不能恢复或再次流转")
        index = order.index(current)
        expected = order[index + 1]
        target = action_rules[action]
        if target != expected:
            raise ContractError(
                f"{module_label}只能按{'、'.join(order)}顺序流转，"
                f"当前「{current}」只能进入「{expected}」，不能跳到「{target}」"
            )
        return target

    # ------------------------------------------------------------------
    # 归因规则发布摘要
    # ------------------------------------------------------------------
    def _digest_payload(self, rule_number: str, rule_version: str, values: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "规则编号": rule_number,
            "规则版本": rule_version,
            **{field: _as_text(values.get(field)) for field in RULE_SNAPSHOT_FIELDS},
        }
        payload["结果样本金额"] = _money(payload.get("结果样本金额"))
        return payload

    def _append_digest(self, payload: dict[str, Any], operator: str) -> dict[str, Any]:
        rule_number = payload["规则编号"]
        rule_version = payload["规则版本"]
        digest_ref = f"{rule_number}@{rule_version}"
        digest_hash = _hash(payload)

        existing_ref = store.aux_find("attribution_digest", "digest_ref", digest_ref)
        if existing_ref is not None:
            if existing_ref.get("digest_hash") == digest_hash:
                self._audit(
                    "归因摘要重复发布未重复生效",
                    target=digest_ref,
                    detail=f"摘要 {digest_hash[:12]} 已存在，本次未生成新记录",
                    operator=operator,
                    outcome="重复发布已忽略",
                )
                return {**copy.deepcopy(existing_ref), "duplicate": True}
            raise ContractError(
                f"规则版本 {digest_ref} 已按另一份触点权重发布摘要 "
                f"{str(existing_ref.get('digest_hash'))[:12]}，同一规则版本不能覆盖；请提升规则版本"
            )

        # 同一规则版本按内容去重即可；内容相同但规则版本不同仍要各自保留摘要，
        # 否则返回字段里的 rule_version 会指向错误的版本。
        record = {
            "id": max(
                (int(row.get("id", 0)) for row in store.aux("attribution_digest")), default=0
            )
            + 1,
            "rule_number": rule_number,
            "rule_version": rule_version,
            "rule_ref": digest_ref,
            "digest_ref": digest_ref,
            "digest_hash": digest_hash,
            "backtrack_window_days": payload["回溯窗口天"],
            "touchpoint_weights": {
                "触点权重": payload["触点权重"],
                "首次触点权重": payload["首次触点权重"],
                "末次触点权重": payload["末次触点权重"],
                "中间触点衰减": payload["中间触点衰减"],
                "关联渠道": payload["关联渠道"],
            },
            "attributed_users": int(payload.get("结果样本用户数") or 0),
            "attributed_revenue": payload["结果样本金额"],
            "published_at": _now(),
            "published_by": operator,
            "immutable": True,
            "snapshot": copy.deepcopy(payload),
        }
        store.aux("attribution_digest").append(record)
        self._audit(
            "归因结果摘要已生成",
            target=digest_ref,
            detail=f"摘要 {digest_hash[:12]} 冻结触点权重、回溯窗口与规则版本",
            operator=operator,
        )
        return {**copy.deepcopy(record), "duplicate": False}

    def publish_rule_digest(self, rule_id: int, *, operator: str) -> dict[str, Any]:
        """归因规则发布时生成不可变结果摘要；重复发布同一规则版本只生效一次。"""
        with self._lock:
            rule = store.find("attribution", rule_id)
            if rule is None:
                raise ContractError(f"归因规则 {rule_id} 不存在或已归档")
            missing = [
                field
                for field in ("规则编号", "规则版本", "回溯窗口天", "触点权重", "关联渠道")
                if not _as_text(rule.get(field))
            ]
            if missing:
                raise ContractError(f"归因规则缺少发布所需字段：{'、'.join(missing)}")
            payload = self._digest_payload(
                _as_text(rule.get("规则编号")), _as_text(rule.get("规则版本")), rule
            )
            result = self._append_digest(payload, operator)
            result["message"] = (
                f"{result['digest_ref']} 的同一结果摘要已发布过，重复发布未再次生效"
                if result.get("duplicate")
                else f"{result['digest_ref']} 结果摘要已生成"
            )
            return result

    def get_digest(self, digest_hash: str) -> dict[str, Any] | None:
        digest = store.aux_find("attribution_digest", "digest_hash", digest_hash)
        return copy.deepcopy(digest) if digest else None

    def find_digest_for_rule(self, rule_ref: str) -> dict[str, Any] | None:
        return store.aux_find("attribution_digest", "digest_ref", rule_ref)

    def list_digests(self, rule_number: str | None = None) -> list[dict[str, Any]]:
        rows = store.aux("attribution_digest")
        if rule_number:
            rows = [row for row in rows if row.get("rule_number") == rule_number]
        return copy.deepcopy(rows)

    # ------------------------------------------------------------------
    # 服务契约：预览 -> 发布
    # ------------------------------------------------------------------
    def _plan_contract(self, service_values: dict[str, Any]) -> dict[str, Any]:
        missing = [
            field
            for field in ("服务编号", "数据集名称", "查询参数定义", "返回字段", "版本号", "消费方应用")
            if not _as_text(service_values.get(field))
        ]
        if missing:
            raise ContractError(f"数据服务缺少发布所需字段：{'、'.join(missing)}")

        return_fields = _split_list(service_values.get("返回字段"))
        if return_fields != FROZEN_RETURN_FIELDS:
            lack = [field for field in FROZEN_RETURN_FIELDS if field not in return_fields]
            extra = sorted(set(return_fields) - set(FROZEN_RETURN_FIELDS))
            parts = []
            if lack:
                parts.append(f"缺少冻结字段：{'、'.join(lack)}")
            if extra:
                parts.append(f"出现未冻结字段：{'、'.join(extra)}")
            if not parts:
                parts.append("字段顺序必须与冻结顺序一致")
            raise ContractError("；".join(parts))

        query_parameters = _split_list(service_values.get("查询参数定义"))
        if not query_parameters:
            raise ContractError("查询参数定义不能为空")

        consumers = _split_list(service_values.get("消费方应用"))
        if not consumers:
            raise ContractError("消费方应用不能为空，契约必须登记消费方")

        rule_ref = _as_text(service_values.get("归因规则编号"))
        if not rule_ref:
            raise ContractError("必须指定归因规则编号@规则版本，服务只能按已发布摘要登记")
        digest = self.find_digest_for_rule(rule_ref)
        if digest is None:
            raise ContractError(f"归因规则 {rule_ref} 尚未发布结果摘要，不能登记服务契约")

        rule = next(
            (
                row
                for row in store.rows("attribution")
                if _as_text(row.get("规则编号")) == _rule_number(rule_ref)
            ),
            None,
        )
        if rule is not None and rule.get("status") == "已下线":
            raise ContractError(
                f"归因规则 {_rule_number(rule_ref)} 已下线，不能作为新契约来源；已发布服务保持原契约"
            )

        service_number = _as_text(service_values.get("服务编号"))
        version = _as_text(service_values.get("版本号"))
        plan = {
            "服务编号": service_number,
            "service_number": service_number,
            "数据集名称": _as_text(service_values.get("数据集名称")),
            "版本号": version,
            "version": version,
            "rule_ref": rule_ref,
            "digest_ref": digest["digest_ref"],
            "digest_hash": digest["digest_hash"],
            # 返回字段里的 rule_version 统一用「规则编号@规则版本」，与接口响应一致；
            # 摘要内部保存的规则版本另存一份，避免两个口径混用。
            "rule_version": rule_ref,
            "digest_rule_version": digest["rule_version"],
            "backtrack_window_days": digest["backtrack_window_days"],
            "touchpoint_weights": copy.deepcopy(digest["touchpoint_weights"]),
            "attributed_users": digest["attributed_users"],
            "attributed_revenue": digest["attributed_revenue"],
            "return_fields": return_fields,
            "field_sources": {field: FIELD_SOURCES[field] for field in return_fields},
            "query_parameters": query_parameters,
            "consumers": consumers,
            "consumer_count": len(consumers),
        }
        fingerprint = {
            "service_number": service_number,
            "version": version,
            "rule_ref": rule_ref,
            "digest_hash": digest["digest_hash"],
            "return_fields": return_fields,
            "query_parameters": query_parameters,
            "consumers": consumers,
        }
        plan["preview_hash"] = _hash(fingerprint)
        plan["fingerprint"] = fingerprint
        return plan

    def _current_contract(self, service_number: str) -> dict[str, Any] | None:
        for row in store.aux("delivery_contract"):
            if row.get("service_number") == service_number and row.get("current"):
                return row
        return None

    def _fallback_contracts(self, service_number: str) -> list[dict[str, Any]]:
        """其他可回退版本：被替代的、已下线的，以及当前正在生效的版本。"""
        return [
            row
            for row in store.aux("delivery_contract")
            if row.get("service_number") == service_number
        ]

    def preview_service_contract(self, service_values: dict[str, Any]) -> dict[str, Any]:
        """契约预览：不落库，但使用与发布完全相同的指纹算法。"""
        with self._lock:
            plan = self._plan_contract(service_values)
            service_number = plan["service_number"]
            current = self._current_contract(service_number)
            existing = store.aux_find("delivery_contract", "preview_hash", plan["preview_hash"])
            fallbacks = [
                {
                    "version": row.get("version"),
                    "contract_id": row.get("id"),
                    "rule_ref": row.get("rule_ref"),
                    "published_at": row.get("published_at"),
                    "status": row.get("status"),
                    "current": bool(row.get("current")),
                }
                for row in self._fallback_contracts(service_number)
            ]
            view = {
                **copy.deepcopy({key: plan[key] for key in plan if key != "fingerprint"}),
                "current_contract_id": current.get("id") if current else None,
                "fallback_versions": fallbacks,
                "already_published": bool(existing),
                "published_contract_id": existing.get("id") if existing else None,
                "digest_immutable": True,
                "preview_only": True,
                "message": "契约预览已生成，字段指纹与发布记录一致；发布前不会写入任何契约",
            }
            view["trace"] = self._provenance(view)
            return view

    def publish_service_contract(
        self, service_values: dict[str, Any], *, operator: str
    ) -> dict[str, Any]:
        with self._lock:
            plan = self._plan_contract(service_values)
            return self._persist_contract(plan, operator)

    def _persist_contract(self, plan: dict[str, Any], operator: str) -> dict[str, Any]:
        service_number = plan["service_number"]
        version = plan["version"]
        preview_hash = plan["preview_hash"]
        same = store.aux_find("delivery_contract", "preview_hash", preview_hash)
        if same is not None:
            if same.get("current"):
                self._audit(
                    "服务契约重复发布未重复生效",
                    target=service_number,
                    detail=f"契约 {same.get('id')} 指纹 {preview_hash[:12]} 已生效，本次未写入",
                    operator=operator,
                    outcome="重复发布已忽略",
                    extra={"contract_id": same.get("id")},
                )
                return {
                    **self._contract_view(same),
                    "duplicate": True,
                    "message": f"同一份契约已发布并生效（契约 {same.get('id')}），重复发布未再次生效",
                }
            raise ContractError(
                f"服务版本 {service_number}@{version} 的契约 {same.get('id')} 已被替代或下线，"
                "不能重新启用；请发布新版本并保留旧返回契约"
            )

        current = self._current_contract(service_number)
        if current is not None and current.get("version") == version:
            raise ContractError(
                f"服务版本 {service_number}@{version} 已由契约 {current.get('id')}"
                f"（摘要 {str(current.get('digest_hash'))[:12]}）完整发布并作为当前版本；"
                f"本次提交的摘要 {plan['digest_hash'][:12]} 与其冲突，不能覆盖，请提升服务版本号后重新发布"
            )

        entry = {
            "id": max(
                (int(row.get("id", 0)) for row in store.aux("delivery_contract")), default=0
            )
            + 1,
            "service_number": service_number,
            "版本号": version,
            "version": version,
            "rule_ref": plan["rule_ref"],
            "digest_ref": plan["digest_ref"],
            "digest_hash": plan["digest_hash"],
            "rule_version": plan["rule_version"],
            "digest_rule_version": plan["digest_rule_version"],
            "backtrack_window_days": plan["backtrack_window_days"],
            "touchpoint_weights": copy.deepcopy(plan["touchpoint_weights"]),
            "attributed_users": plan["attributed_users"],
            "attributed_revenue": plan["attributed_revenue"],
            "return_fields": copy.deepcopy(plan["return_fields"]),
            "field_sources": copy.deepcopy(plan["field_sources"]),
            "query_parameters": copy.deepcopy(plan["query_parameters"]),
            "consumers": copy.deepcopy(plan["consumers"]),
            "consumer_count": plan["consumer_count"],
            "preview_hash": preview_hash,
            "status": PUBLISHED,
            "current": True,
            "published_at": _now(),
            "published_by": operator,
            "supersedes": current.get("id") if current else None,
        }
        try:
            store.aux("delivery_contract").append(entry)
        except Exception as error:
            self._audit(
                "服务契约发布失败",
                target=service_number,
                detail=f"字段写入失败：{error}",
                operator=operator,
                outcome="失败未生效",
            )
            raise ContractError(f"服务契约写入失败，未生效：{error}") from error

        if current is not None:
            current["current"] = False
            current["superseded_by"] = entry["id"]
            current["superseded_at"] = entry["published_at"]
        self._audit(
            "服务契约已发布",
            target=service_number,
            detail=(
                f"版本 {version} 按 {plan['digest_ref']} 摘要登记，契约 {entry['id']} "
                f"指纹 {preview_hash[:12]}，消费方 {plan['consumer_count']} 个"
            ),
            operator=operator,
            extra={"contract_id": entry["id"], "preview_hash": preview_hash},
        )
        return {
            **self._contract_view(entry),
            "duplicate": False,
            "message": f"服务契约已发布，绑定 {plan['digest_ref']}",
        }

    def _contract_view(self, contract: dict[str, Any]) -> dict[str, Any]:
        view = copy.deepcopy(contract)
        view["sample_result"] = self._sample_result(contract)
        view["trace"] = self._provenance(contract)
        return view

    def _sample_result(self, contract: dict[str, Any]) -> dict[str, Any]:
        weights = contract.get("touchpoint_weights") or {}
        return {
            "rule_version": contract.get("rule_ref") or contract.get("rule_version"),
            "channel": weights.get("关联渠道"),
            "first_touch_weight": weights.get("首次触点权重"),
            "last_touch_weight": weights.get("末次触点权重"),
            "middle_decay": weights.get("中间触点衰减"),
            "attributed_value": contract.get("attributed_revenue"),
            "attributed_users": contract.get("attributed_users"),
            "attributed_revenue": contract.get("attributed_revenue"),
        }

    def _provenance(self, record: dict[str, Any]) -> dict[str, Any]:
        """任一返回字段都能追回摘要、规则版本和契约，字段值逐个附上来源。"""
        weights = record.get("touchpoint_weights") or {}
        values = {
            "rule_version": record.get("rule_ref") or record.get("rule_version"),
            "channel": weights.get("关联渠道"),
            "first_touch_weight": weights.get("首次触点权重"),
            "last_touch_weight": weights.get("末次触点权重"),
            "middle_decay": weights.get("中间触点衰减"),
            "attributed_value": record.get("attributed_revenue"),
        }
        return {
            field: {
                "value": values[field],
                "source": FIELD_SOURCES[field],
                "service_number": record.get("service_number"),
                "version": record.get("version"),
                "contract_id": record.get("id"),
                "digest_ref": record.get("digest_ref"),
                "digest_hash": record.get("digest_hash"),
                "rule_version": record.get("rule_ref") or record.get("rule_version"),
                "backtrack_window_days": record.get("backtrack_window_days"),
                "preview_hash": record.get("preview_hash"),
            }
            for field in FROZEN_RETURN_FIELDS
        }

    def _apply_contract_to_service(self, service: dict[str, Any], contract: dict[str, Any]) -> None:
        service.update(
            {
                "返回字段": "、".join(contract["return_fields"]),
                "查询参数定义": "、".join(contract["query_parameters"]),
                "消费方应用": "、".join(contract["consumers"]),
                "版本号": contract["version"],
                "归因规则编号": contract["rule_ref"],
                "contract_id": contract["id"],
                "contract_preview_hash": contract["preview_hash"],
                "digest_ref": contract["digest_ref"],
                "digest_hash": contract["digest_hash"],
                "rule_version": contract["rule_ref"],
                "attribution_return": self._sample_result(contract),
            }
        )

    # ------------------------------------------------------------------
    # 服务版本状态流转
    # ------------------------------------------------------------------
    def run_delivery_action(
        self,
        service_id: int,
        action: str,
        values: dict[str, Any] | None,
        *,
        operator: str,
    ) -> dict[str, Any]:
        values = values or {}
        with self._lock:
            service = store.find("delivery", service_id)
            if service is None:
                raise ContractError(f"服务版本 {service_id} 不存在或已归档")
            current = _as_text(service.get("status"))
            try:
                target = self.validate_transition(
                    current,
                    action,
                    order=SERVICE_STATUS_ORDER,
                    action_rules=SERVICE_ACTIONS,
                    module_label="数据服务",
                )
            except ContractError as error:
                self._audit(
                    "数据服务状态流转被拒绝",
                    target=_as_text(service.get("服务编号")),
                    detail=str(error),
                    operator=operator,
                    outcome="被拒绝",
                )
                raise

            if target == PUBLISHED:
                merged = {**service, **values}
                plan = self._plan_contract(merged)
                # 契约可能已经通过 /contract 单独发布过；同一指纹直接复用，
                # 服务发布动作此时只负责把版本推进到“发布”，不会重复写契约。
                existing = store.aux_find("delivery_contract", "preview_hash", plan["preview_hash"])
                created_here = False
                if existing is not None and existing.get("current"):
                    contract = self._contract_view(existing)
                else:
                    contract = self._persist_contract(plan, operator)
                    created_here = True
                    if contract.get("duplicate"):
                        raise ContractError(contract.get("message", "同一份契约已生效，不能重复发布"))
                try:
                    self._apply_contract_to_service(service, contract)
                    service["status"] = target
                    service["pending"] = False
                    service["abnormal"] = False
                    self._audit(
                        "数据服务状态已流转",
                        target=_as_text(service.get("服务编号")),
                        detail=f"{current} -> {target}，绑定契约 {contract['id']}",
                        operator=operator,
                    )
                except Exception as error:
                    if created_here:
                        self._rollback_contract(contract["id"])
                    raise ContractError(f"服务绑定契约失败，服务未发布：{error}") from error
                return {**copy.deepcopy(service), "contract": self._contract_view(contract)}

            if target == OFFLINE:
                contract = self._current_contract(_as_text(service.get("服务编号")))
                if contract is not None:
                    contract["status"] = OFFLINE
                    contract["current"] = False
                    contract["offline_at"] = _now()
                    self._audit(
                        "服务契约已下线",
                        target=_as_text(service.get("服务编号")),
                        detail=f"契约 {contract['id']} 下线，返回契约作为历史保留",
                        operator=operator,
                    )

            service["status"] = target
            service["pending"] = target != OFFLINE
            service["abnormal"] = target == OFFLINE
            self._audit(
                "数据服务状态已流转",
                target=_as_text(service.get("服务编号")),
                detail=f"{current} -> {target}",
                operator=operator,
            )
            return copy.deepcopy(service)

    def _rollback_contract(self, contract_id: int) -> None:
        rows = store.aux("delivery_contract")
        entry = store.aux_find("delivery_contract", "id", contract_id)
        if entry is None:
            return
        supersedes = entry.get("supersedes")
        rows.remove(entry)
        previous = store.aux_find("delivery_contract", "id", supersedes) if supersedes else None
        if previous is not None:
            previous["current"] = True
            previous.pop("superseded_by", None)
            previous.pop("superseded_at", None)

    # ------------------------------------------------------------------
    # 查询与追溯
    # ------------------------------------------------------------------
    def get_contract(self, contract_id: int) -> dict[str, Any] | None:
        row = store.aux_find("delivery_contract", "id", contract_id)
        return self._contract_view(row) if row else None

    def get_service_contract(self, service_number: str) -> dict[str, Any] | None:
        row = self._current_contract(service_number)
        return self._contract_view(row) if row else None

    def list_contracts(self, service_number: str | None = None) -> list[dict[str, Any]]:
        rows = store.aux("delivery_contract")
        if service_number:
            rows = [row for row in rows if row.get("service_number") == service_number]
        return [self._contract_view(row) for row in rows]

    def service_result(self, service_number: str) -> dict[str, Any] | None:
        """按服务编号返回契约的接口响应样本，并带上逐字段追溯信息。

        服务下线后当前契约会被归档，但历史返回契约仍然保留，因此这里回退到
        该服务最近一份契约，让旧响应字段依旧可以追溯。
        """
        contract = self.get_service_contract(service_number)
        if contract is None:
            archived = [
                row
                for row in store.aux("delivery_contract")
                if row.get("service_number") == service_number
            ]
            if not archived:
                return None
            contract = self._contract_view(archived[-1])
        return {
            "service_number": service_number,
            "contract_id": contract["id"],
            "status": contract.get("status"),
            "current": bool(contract.get("current")),
            "sample_result": contract["sample_result"],
            "trace": contract["trace"],
        }

    def resolve_result(self, **query: Any) -> dict[str, Any] | None:
        """从任一返回字段值回查契约，再沿摘要回到规则版本。"""
        contract = self._resolve_contract(query)
        if contract is None:
            return None
        digest = self.get_digest(contract.get("digest_hash") or "")
        return {
            "service_number": contract.get("service_number"),
            "contract_id": contract.get("id"),
            "version": contract.get("version"),
            "status": contract.get("status"),
            "rule_ref": contract.get("rule_ref"),
            "rule_version": contract.get("rule_ref") or contract.get("rule_version"),
            "digest_ref": contract.get("digest_ref"),
            "digest_hash": contract.get("digest_hash"),
            "backtrack_window_days": contract.get("backtrack_window_days"),
            "touchpoint_weights": contract.get("touchpoint_weights"),
            "attributed_users": contract.get("attributed_users"),
            "attributed_revenue": contract.get("attributed_revenue"),
            "return_fields": contract.get("return_fields"),
            "consumers": contract.get("consumers"),
            "preview_hash": contract.get("preview_hash"),
            "digest": digest,
            "sample_result": self._sample_result(contract),
            "trace": self._provenance(contract),
        }

    def _resolve_contract(self, query: dict[str, Any]) -> dict[str, Any] | None:
        rule_version = _as_text(query.get("rule_version"))
        attributed_value = _as_text(query.get("attributed_value"))
        channel = _as_text(query.get("channel"))
        first = _as_text(query.get("first_touch_weight"))
        last = _as_text(query.get("last_touch_weight"))
        decay = _as_text(query.get("middle_decay"))
        users = _as_text(query.get("attributed_users"))
        contract_id = query.get("contract_id")
        preview_hash = _as_text(query.get("preview_hash"))
        if not any(
            [rule_version, attributed_value, channel, first, last, decay, users, contract_id, preview_hash]
        ):
            return None
        if contract_id is not None:
            try:
                contract_id = int(contract_id)
            except (TypeError, ValueError):
                return None

        def matches(row: dict[str, Any]) -> bool:
            if contract_id is not None and row.get("id") != contract_id:
                return False
            if preview_hash and row.get("preview_hash") != preview_hash:
                return False
            weights = row.get("touchpoint_weights") or {}
            if rule_version:
                accepted = {
                    _as_text(row.get("rule_ref")),
                    _as_text(row.get("rule_version")),
                }
                if rule_version not in accepted:
                    return False
            if attributed_value and _money(attributed_value) != _money(row.get("attributed_revenue")):
                return False
            if users and _as_text(row.get("attributed_users")) != users:
                return False
            if channel and channel != _as_text(weights.get("关联渠道")):
                return False
            if first and first != _as_text(weights.get("首次触点权重")):
                return False
            if last and last != _as_text(weights.get("末次触点权重")):
                return False
            if decay and decay != _as_text(weights.get("中间触点衰减")):
                return False
            return True

        rows = list(store.aux("delivery_contract"))
        for row in rows:
            if row.get("current") and matches(row):
                return row
        for row in rows:
            if matches(row):
                return row
        return None

    def contract_audit(self) -> dict[str, Any]:
        return {
            "items": copy.deepcopy(store.aux("contract_audit")),
            "total": len(store.aux("contract_audit")),
        }

    def _audit(
        self,
        action: str,
        *,
        target: str,
        detail: str,
        operator: str = "",
        outcome: str = "已生效",
        extra: dict[str, Any] | None = None,
    ) -> None:
        store.aux("contract_audit").append(
            {
                "id": max(
                    (int(row.get("id", 0)) for row in store.aux("contract_audit")), default=0
                )
                + 1,
                "action": action,
                "target": target,
                "detail": detail,
                "operator": operator or "值班管理员",
                "outcome": outcome,
                "at": _now(),
                **({"extra": extra} if extra else {}),
            }
        )


contracts = ContractService()


def _bootstrap_from_seed() -> None:
    """把示例数据里已发布的 ATTR-0003@v6 与 DELI-0003 接成一份契约基线。"""
    baseline = SEED_CONTRACT_BASELINE
    rule_number = _rule_number(baseline["rule_ref"])
    rule = next(
        (
            row
            for row in SEED_ROWS.get("attribution", [])
            if row.get("规则编号") == rule_number
        ),
        None,
    )
    service = next(
        (
            row
            for row in SEED_ROWS.get("delivery", [])
            if row.get("服务编号") == baseline["service_number"]
        ),
        None,
    )
    if rule is None or service is None:
        return

    store_rule = store.find("attribution", int(rule["id"]))
    store_service = store.find("delivery", int(service["id"]))
    if store_rule is None or store_service is None:
        return

    # 上一版本：预置摘要与契约，供契约预览列出可回退版本。
    previous = baseline.get("previous") or {}
    if previous:
        previous_payload = contracts._digest_payload(
            rule_number,
            previous["rule_version"],
            {**store_rule, **previous["rule_values"]},
        )
        contracts._append_digest(previous_payload, baseline["operator"])
        contracts.publish_service_contract(
            {
                **baseline["service_values"],
                **previous.get("service_values", {}),
                "归因规则编号": previous["rule_ref"],
            },
            operator=baseline["operator"],
        )

    # 当前版本：把示例行升级成 ATTR-0003@v6 的规则与契约数据。
    store_rule.update(baseline["rule_values"])
    store_service.update(baseline["service_values"])
    contracts.publish_rule_digest(int(store_rule["id"]), operator=baseline["operator"])
    current = contracts.publish_service_contract(
        {
            **baseline["service_values"],
            "归因规则编号": baseline["rule_ref"],
            "返回字段": FROZEN_RETURN_FIELDS,
        },
        operator=baseline["operator"],
    )
    contract_row = store.aux_find("delivery_contract", "id", current["id"])
    if contract_row is not None:
        contracts._apply_contract_to_service(store_service, contract_row)
        store_service["status"] = PUBLISHED


_bootstrap_from_seed()
