"""归因模型接口：维护归因规则，发布时生成不可变结果摘要。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.attribution import AttributionService
from app.services.contracts import contracts

router = APIRouter(prefix="/api/attribution", tags=["归因模型"])

service = AttributionService()

LIST_FIELDS = ["规则编号", "归因类型", "回溯窗口天", "触点权重", "首次触点权重", "末次触点权重", "中间触点衰减", "关联渠道"]
STATUSES = ["草稿", "规则评审中", "已发布", "已下线"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、规则评审中、已发布、已下线"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与状态过滤归因模型列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/digests", response_model=dict)
def list_digests(rule_number: str | None = Query(default=None)) -> dict[str, Any]:
    """归因结果摘要清单：发布结果不可修改，重复发布同一规则版本只保留一份。"""
    items = contracts.list_digests(rule_number)
    return {"items": items, "total": len(items)}


@router.get("/digests/{digest_hash}", response_model=dict)
def get_digest(digest_hash: str) -> dict[str, Any]:
    digest = contracts.get_digest(digest_hash)
    if digest is None:
        raise HTTPException(status_code=404, detail=f"归因结果摘要 {digest_hash} 不存在")
    return digest


@router.get("/contracts", response_model=dict)
def list_contracts(rule_number: str | None = Query(default=None)) -> dict[str, Any]:
    """引用该归因规则的已发布服务契约，便于核对哪些服务用了哪一版规则。"""
    items = contracts.list_contracts()
    if rule_number:
        items = [item for item in items if item.get("rule_ref", "").startswith(f"{rule_number}@")]
    return {"items": items, "total": len(items)}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出归因模型清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "attribution", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条归因规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"归因规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条归因规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="归因规则已登记", entry=entry)


@router.patch("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """草稿可以调整触点权重；已发布规则要改权重必须提升规则版本。"""
    entry, message = service.update_entry(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """提交规则评审、发布规则、下线规则；发布时生成不可变结果摘要。"""
    action = str(payload.values.get("action") or "").strip()
    operator = str(payload.values.get("operator") or "值班管理员")
    entry, message = service.run_action(entry_id, action, operator=operator)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


