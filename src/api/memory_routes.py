"""Self-service long-term memory API with strict ownership isolation.

Administrators have no override: signed Open WebUI user IDs are the hard privacy boundary.
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
    """Personal long-term memory opt-in or opt-out request."""

    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool


@router.get("/settings")
def personal_memory_settings(principal: Principal = Depends(get_principal)):
    """Return the caller's memory preference and server-side availability."""
    return {
        "enabled": memory_enabled(principal.user_id),
        "configured": bool(settings.ENABLE_MEMORY and local_memory_configuration()),
    }


@router.post("/settings")
def update_personal_memory_settings(
    body: MemorySettingsForm, principal: Principal = Depends(get_principal)
):
    """Update the caller's long-term memory preference and return the new settings."""
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
    """List the caller's memories, optionally including history; return 503 when memory is
    disabled.
    """
    records = _service().store.list_for_user(principal.user_id, include_inactive=include_history)
    return {"data": [_public(r) for r in records]}


@router.post("/{memory_id}/confirm")
def confirm_memory(memory_id: str, principal: Principal = Depends(get_principal)):
    """Resolve a dispute in favor of this memory."""
    if not _service().confirm(principal.user_id, memory_id):
        raise HTTPException(status_code=404, detail="No disputed memory with this ID")
    return {"ok": True}


@router.delete("/{memory_id}")
def forget_memory(memory_id: str, principal: Principal = Depends(get_principal)):
    """Delete an owned memory and its version chain; return 404 for missing or unowned records."""
    removed = _service().forget(principal.user_id, memory_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"removed": removed}


@router.delete("")
def forget_all(principal: Principal = Depends(get_principal)):
    """Delete all memories owned by the caller and return the deletion count."""
    return {"removed": _service().forget(principal.user_id)}
