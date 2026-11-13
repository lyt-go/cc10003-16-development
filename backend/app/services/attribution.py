"""归因模型业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services.contracts import RULE_EDITABLE_FIELDS, ContractError, contracts
from app.store import store

MODULE = "attribution"
REQUIRED_FIELDS = ["规则编号", "归因类型", "回溯窗口天"]
STATUS_ORDER = ["草稿", "规则评审中", "已发布", "已下线"]
ACTION_RULES = {"提交规则评审": "规则评审中", "发布规则": "已发布", "下线规则": "已下线"}
NEGATIVE_ACTIONS = ["下线规则"]
PUBLISHED_STATUS = "已发布"
OFFLINE_STATUS = "已下线"


class AttributionService:
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
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
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
        entry.update({field: values.get(field) for field in RULE_EDITABLE_FIELDS})
        entry["规则版本"] = str(entry.get("规则版本") or "v1")
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def update_entry(
        self, entry_id: int, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        """仅草稿允许调整规则内容；已发布规则的权重不能原地修改。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"归因规则 {entry_id} 不存在或已归档"
        if entry.get("status") != STATUS_ORDER[0]:
            return None, "只有草稿状态的归因规则可以修改，发布后的权重变更必须提升规则版本"
        for field in RULE_EDITABLE_FIELDS:
            if field in values and str(values.get(field) or "").strip():
                entry[field] = values.get(field)
        return entry, "归因规则已更新，发布时会重新生成结果摘要"

    def run_action(self, entry_id: int, action: str, *, operator: str = "") -> tuple[dict[str, Any] | None, str]:
        with contracts._lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"归因规则 {entry_id} 不存在或已归档"
            current = str(entry.get("status") or "")
            try:
                target = contracts.validate_transition(
                    current,
                    action,
                    order=STATUS_ORDER,
                    action_rules=ACTION_RULES,
                    module_label="归因规则",
                )
            except ContractError as error:
                return None, str(error)

            if target == PUBLISHED_STATUS:
                try:
                    digest = contracts.publish_rule_digest(entry_id, operator=operator or "值班管理员")
                except ContractError as error:
                    return None, str(error)
                entry.update(
                    {
                        "status": target,
                        "pending": False,
                        "abnormal": False,
                        "digest_ref": digest["digest_ref"],
                        "digest_hash": digest["digest_hash"],
                        "published_at": digest.get("published_at") or entry.get("published_at"),
                    }
                )
                suffix = "（同一规则版本重复发布，摘要只生效一次）" if digest.get("duplicate") else ""
                return entry, f"归因规则已发布，结果摘要 {digest['digest_ref']} 已生成{suffix}"

            if target == OFFLINE_STATUS:
                entry["status"] = target
                entry["pending"] = False
                entry["abnormal"] = True
                active = [
                    row
                    for row in store.aux("delivery_contract")
                    if row.get("current") and row.get("rule_ref", "").startswith(f"{entry.get('规则编号')}@")
                ]
                detail = (
                    f"规则下线，{len(active)} 份已发布服务契约保持原摘要不变"
                    if active
                    else "规则下线，当前没有已发布服务契约引用该规则"
                )
                contracts._audit(
                    "归因规则已下线",
                    target=str(entry.get("规则编号")),
                    detail=detail,
                    operator=operator or "值班管理员",
                )
                return entry, f"归因规则已下线；{detail}"

            entry["status"] = target
            entry["pending"] = True
            entry["abnormal"] = False
            return entry, f"归因规则已{action}"
