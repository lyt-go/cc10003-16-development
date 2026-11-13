"""数据服务接口：维护服务版本，覆盖契约预览、契约发布与状态流转。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.contracts import FROZEN_RETURN_FIELDS, contracts
from app.services.delivery import DeliveryService

router = APIRouter(prefix="/api/delivery", tags=["数据服务"])

service = DeliveryService()

LIST_FIELDS = ["服务编号", "数据集名称", "查询参数定义", "返回字段", "速率限制", "分页最大条数", "版本号", "消费方应用"]
STATUSES = ["草稿", "评审", "发布", "下线"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按服务编号检索"),
    status: str | None = Query(default=None, description="草稿、评审、发布、下线"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按服务编号与状态过滤数据服务列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/contracts", response_model=dict)
def list_contracts(service_number: str | None = Query(default=None)) -> dict[str, Any]:
    """发布记录：列出契约及逐字段追溯信息，预览与发布记录共用同一指纹。"""
    items = contracts.list_contracts(service_number)
    return {"items": items, "total": len(items), "frozen_fields": FROZEN_RETURN_FIELDS}


@router.get("/contracts/{contract_id}", response_model=dict)
def get_contract(contract_id: int) -> dict[str, Any]:
    contract = contracts.get_contract(contract_id)
    if contract is None:
        raise HTTPException(status_code=404, detail=f"服务契约 {contract_id} 不存在")
    return contract


@router.get("/audit", response_model=dict)
def contract_audit() -> dict[str, Any]:
    """审计：摘要生成、契约发布、替代、下线与拒绝写入都按时间追加。"""
    return contracts.contract_audit()


@router.get("/resolve", response_model=dict)
def resolve_result(
    rule_version: str | None = Query(default=None),
    channel: str | None = Query(default=None),
    first_touch_weight: str | None = Query(default=None),
    last_touch_weight: str | None = Query(default=None),
    middle_decay: str | None = Query(default=None),
    attributed_value: str | None = Query(default=None),
    attributed_users: str | None = Query(default=None),
    contract_id: int | None = Query(default=None),
    preview_hash: str | None = Query(default=None),
) -> dict[str, Any]:
    """从任一返回字段回查归因结果契约与规则版本。"""
    result = contracts.resolve_result(
        rule_version=rule_version,
        channel=channel,
        first_touch_weight=first_touch_weight,
        last_touch_weight=last_touch_weight,
        middle_decay=middle_decay,
        attributed_value=attributed_value,
        attributed_users=attributed_users,
        contract_id=contract_id,
        preview_hash=preview_hash,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="没有找到与这些返回字段匹配的已发布契约")
    return result


@router.get("/result/{service_number}", response_model=dict)
def service_result(service_number: str) -> dict[str, Any]:
    """服务版本当前契约的接口响应样本：返回字段本身即可回查规则版本。"""
    result = contracts.service_result(service_number)
    if result is None:
        raise HTTPException(status_code=404, detail=f"服务版本 {service_number} 尚未发布契约")
    return result


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出数据服务清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "delivery", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条服务版本明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"服务版本 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条服务版本，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="服务版本已登记", entry=entry)


@router.patch("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """草稿或评审中的服务版本可以调整契约字段，发布后只能新版本重新发布。"""
    entry, message = service.update_entry(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/contract/preview", response_model=dict)
def preview_contract(entry_id: int, payload: EntryPayload) -> dict[str, Any]:
    """契约预览：字段、消费方、可回退版本与指纹，不落库。"""
    preview, message = service.contract_preview(entry_id, payload.values)
    if preview is None:
        return {"ok": False, "message": message}
    return {"ok": True, "message": preview["message"], "preview": preview}


@router.post("/{entry_id}/contract", response_model=ActionResult)
def publish_contract(entry_id: int, payload: EntryPayload) -> ActionResult:
    """按归因结果摘要发布服务契约；同一份契约重复发布只生效一次。"""
    operator = str(payload.values.get("operator") or "值班管理员")
    contract, message = service.publish_contract(entry_id, payload.values, operator=operator)
    if contract is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=contract)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """提交服务评审、发布服务、下线服务；发布时必须带齐契约。"""
    action = str(payload.values.get("action") or "").strip()
    operator = str(payload.values.get("operator") or "值班管理员")
    entry, message = service.run_action(entry_id, action, payload.values, operator=operator)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


