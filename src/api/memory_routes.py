"""自助式长期记忆 API：每位用户只能看到自己的记忆。

有意不提供管理员越权：记忆属于个人数据，硬边界是经签名的
Open WebUI 用户 ID。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, StrictBool

from src.config import settings
from src.memory.local_policy import local_memory_configuration
from src.memory.runtime import get_memory_service
from src.memory.user_policy import memory_enabled, set_memory_enabled
from src.midplatform.tenant_access import Principal, get_principal

router = APIRouter(prefix="/memory", tags=["memory"])


class MemorySettingsForm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool


@router.get("/settings")
def personal_memory_settings(principal: Principal = Depends(get_principal)):
    """查看当前登录用户自己的长期记忆开关，以及服务端是否已配置好记忆功能。"""
    return {
        "enabled": memory_enabled(principal.user_id),
        "configured": bool(settings.ENABLE_MEMORY and local_memory_configuration()),
    }


@router.post("/settings")
def update_personal_memory_settings(
    body: MemorySettingsForm, principal: Principal = Depends(get_principal)
):
    """开启或关闭当前登录用户自己的长期记忆，并返回更新后的设置。"""
    set_memory_enabled(principal.user_id, body.enabled)
    return personal_memory_settings(principal)


def _service():
    service = get_memory_service()
    if service is None:
        raise HTTPException(status_code=503, detail="Long-term memory is disabled")
    return service


def _public(record) -> dict:
    return {
        "id": record.id,
        "scope": record.scope,
        "tenant_id": record.tenant_id,
        "kind": record.kind,
        "subject": record.subject,
        "attribute": record.attribute,
        "value": record.value_text,
        "source": record.source,
        "confidence": record.confidence,
        "status": record.status,
        "disputed_with": record.disputed_with,
        "supersedes_id": record.supersedes_id,
        "valid_from": record.valid_from,
        "valid_to": record.valid_to,
    }


@router.get("")
def list_memories(include_history: bool = False, principal: Principal = Depends(get_principal)):
    """列出当前登录用户自己的长期记忆，可选包含已失效的历史记录；记忆功能未启用时返回 503。"""
    records = _service().store.list_for_user(principal.user_id, include_inactive=include_history)
    return {"data": [_public(r) for r in records]}


@router.post("/{memory_id}/confirm")
def confirm_memory(memory_id: str, principal: Principal = Depends(get_principal)):
    """裁定争议时采纳这条记忆。"""
    if not _service().confirm(principal.user_id, memory_id):
        raise HTTPException(status_code=404, detail="No disputed memory with this ID")
    return {"ok": True}


@router.delete("/{memory_id}")
def forget_memory(memory_id: str, principal: Principal = Depends(get_principal)):
    """删除当前登录用户自己的一条记忆（连同其版本链）；不存在或不属于该用户时返回 404。"""
    removed = _service().forget(principal.user_id, memory_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"removed": removed}


@router.delete("")
def forget_all(principal: Principal = Depends(get_principal)):
    """删除当前登录用户自己的全部记忆，返回删除条数。"""
    return {"removed": _service().forget(principal.user_id)}
