"""Administrator-maintained business tenants and account grants."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.api.dependencies import require_admin
from src.midplatform import tenant_access
from src.midplatform.tenant_access import Principal

router = APIRouter(prefix="/ops/tenants", tags=["ops"])


class TenantBody(BaseModel):
    """Business-tenant identity and display metadata."""

    tenant_id: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=200)


class GrantBody(BaseModel):
    """Account email receiving or losing tenant access."""

    email: str = Field(min_length=3, max_length=320)
    tenant_id: str = Field(min_length=1, max_length=128)


@router.get("")
def list_all_tenants(_: Principal = Depends(require_admin)):
    """Return the full tenant catalog and current grants for account management."""
    return {"tenants": tenant_access.list_tenants(), "grants": tenant_access.list_grants()}


@router.post("")
def create_tenant(body: TenantBody, _: Principal = Depends(require_admin)):
    """Create a business tenant, returning 409 for duplicate IDs."""
    try:
        tenant_access.create_tenant(body.tenant_id, body.label)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@router.delete("/{tenant_id}")
def delete_tenant(tenant_id: str, _: Principal = Depends(require_admin)):
    """Delete a tenant and all account grants to it atomically."""
    tenant_access.delete_tenant(tenant_id)
    return {"ok": True}


@router.post("/grant")
def grant_tenant(body: GrantBody, admin: Principal = Depends(require_admin)):
    """Grant tenant access to an email and record the actor; reject unknown tenants with 400."""
    try:
        tenant_access.set_grant(body.email, body.tenant_id, True, admin.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/revoke")
def revoke_tenant(body: GrantBody, _: Principal = Depends(require_admin)):
    """Revoke the specified account's access to a business tenant."""
    tenant_access.set_grant(body.email, body.tenant_id, False, "")
    return {"ok": True}
