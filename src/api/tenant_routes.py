"""管理员维护业务租户及账号授权。"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.api.dependencies import require_admin
from src.midplatform import tenant_access
from src.midplatform.tenant_access import Principal

router = APIRouter(prefix="/ops/tenants", tags=["ops"])


class TenantBody(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=200)


class GrantBody(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    tenant_id: str = Field(min_length=1, max_length=128)


@router.get("")
def list_all_tenants(_: Principal = Depends(require_admin)):
    """返回完整租户目录及每位用户当前的授权，供账户页使用。"""
    return {"tenants": tenant_access.list_tenants(), "grants": tenant_access.list_grants()}


@router.post("")
def create_tenant(body: TenantBody, _: Principal = Depends(require_admin)):
    """管理员新增一个业务租户；ID 已存在返回 409。"""
    try:
        tenant_access.create_tenant(body.tenant_id, body.label)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@router.delete("/{tenant_id}")
def delete_tenant(tenant_id: str, _: Principal = Depends(require_admin)):
    """管理员删除一个业务租户，并同时清除所有账号对它的授权。"""
    tenant_access.delete_tenant(tenant_id)
    return {"ok": True}


@router.post("/grant")
def grant_tenant(body: GrantBody, admin: Principal = Depends(require_admin)):
    """管理员授予某邮箱访问指定业务租户的权限，并记录授权人；租户不存在返回 400。"""
    try:
        tenant_access.set_grant(body.email, body.tenant_id, True, admin.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/revoke")
def revoke_tenant(body: GrantBody, _: Principal = Depends(require_admin)):
    """管理员撤销某邮箱对指定业务租户的访问权限。"""
    tenant_access.set_grant(body.email, body.tenant_id, False, "")
    return {"ok": True}
