"""数据服务业务规则：服务版本状态流转与契约发布都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services.contracts import (
    FROZEN_RETURN_FIELDS,
    SERVICE_EDITABLE_FIELDS,
    ContractError,
    contracts,
)
from app.store import store

MODULE = "delivery"
REQUIRED_FIELDS = ["服务编号", "数据集名称", "查询参数定义"]
STATUS_ORDER = ["草稿", "评审", "发布", "下线"]
ACTION_RULES = {"提交服务评审": "评审", "发布服务": "发布", "下线服务": "下线"}
NEGATIVE_ACTIONS = ["下线服务"]


def _contract_identity(values: dict[str, Any], service: dict[str, Any] | None = None) -> dict[str, Any]:
    """收敛出契约登记需要的最小字段集合，服务行上已有的值可以补齐。"""
    identity = dict(service or {})
    identity.update({key: value for key, value in values.items() if value not in (None, "")})
    if not str(identity.get("返回字段") or "").strip():
        identity["返回字段"] = FROZEN_RETURN_FIELDS
    if str(identity.get("归因规则编号") or "").strip():
        identity.setdefault("版本号", identity.get("版本号") or "v1")
    return identity


class DeliveryService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("服务编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in SERVICE_EDITABLE_FIELDS})
        entry["版本号"] = str(entry.get("版本号") or "v1")
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def update_entry(
        self, entry_id: int, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"服务版本 {entry_id} 不存在或已归档"
        if entry.get("status") not in (STATUS_ORDER[0], STATUS_ORDER[1]):
            return None, "已发布或已下线的服务版本不能修改返回契约；新版本必须重新发布并保留旧返回契约"
        for field in SERVICE_EDITABLE_FIELDS:
            if field in values and str(values.get(field) or "").strip():
                entry[field] = values.get(field)
        return entry, "服务版本已更新，发布时将按归因摘要重新登记契约"

    def run_action(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any] | None = None,
        *,
        operator: str = "",
    ) -> tuple[dict[str, Any] | None, str]:
        try:
            entry = contracts.run_delivery_action(
                entry_id,
                action,
                values or {},
                operator=operator or "值班管理员",
            )
        except ContractError as error:
            return None, str(error)
        return entry, f"服务版本已{action}"

    def contract_preview(
        self, entry_id: int, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"服务版本 {entry_id} 不存在或已归档"
        try:
            return contracts.preview_service_contract(_contract_identity(values, entry)), ""
        except ContractError as error:
            return None, str(error)

    def publish_contract(
        self, entry_id: int, values: dict[str, Any], *, operator: str
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"服务版本 {entry_id} 不存在或已归档"
        if entry.get("status") not in (STATUS_ORDER[0], STATUS_ORDER[1]):
            return None, "只有草稿或评审中的服务版本可以发布契约；已发布版本不能偷偷换用另一条规则"
        try:
            contract = contracts.publish_service_contract(
                _contract_identity(values, entry), operator=operator or "值班管理员"
            )
        except ContractError as error:
            return None, str(error)
        if contract.get("duplicate"):
            return contract, contract.get("message", "")
        return contract, f"契约 {contract['id']} 已发布，绑定 {contract['digest_ref']}"
