"""仅限管理员的组织架构与账户授权原子分配接口。"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from src.api.ops_routes import require_admin
from src.midplatform import directory
from src.midplatform.tenant_access import Principal

router = APIRouter(prefix="/workspace/directory", tags=["organization"])


class EgressForm(BaseModel):
    external_models: bool = False
    external_search: bool = False
    external_tools: bool = False
    allowed_model_ids: list[str] = Field(default_factory=list, max_length=100)
    allowed_tool_ids: list[str] = Field(default_factory=list, max_length=100)


class AssignmentForm(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    email: str = Field(min_length=3, max_length=254)
    name: str = Field(min_length=1, max_length=100)
    department_id: str = Field(default="", max_length=100)
    job_level: str = Field(default="", max_length=40)
    manager_id: str = Field(default="", max_length=128)
    tenant_ids: list[str] = Field(default_factory=list, max_length=100)
    egress: EgressForm = Field(default_factory=EgressForm)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value):
        """校验邮箱含 @ 且不含空白字符，并统一转为小写。"""
        if "@" not in value or any(char.isspace() for char in value):
            raise ValueError("邮箱格式无效")
        return value.lower()


class DepartmentForm(BaseModel):
    id: str = Field(pattern="^[A-Za-z0-9_-]{1,100}$")
    name: str = Field(min_length=1, max_length=100)
    parent_id: str = Field(default="", max_length=100)


class IdentityForm(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    email: str = Field(min_length=3, max_length=254)
    name: str = Field(min_length=1, max_length=100)


@router.post("/identities")
def register_identity(body: IdentityForm, _: Principal = Depends(require_admin)):
    """管理员导入一条账号身份记录（已存在则忽略）；只写目录元数据，不创建凭据、不授予权限。"""
    # 导入目录元数据不会创建凭据，也不会授予任何访问权限。
    directory.store().execute(
        "INSERT OR IGNORE INTO company_accounts VALUES (?,?,?,?,?,?,?,?)",
        (body.user_id, body.email.lower(), body.name, "", "", "", "identity-import", 0),
    )
    return {"ok": True}


class TemplateForm(BaseModel):
    id: str = Field(pattern="^[A-Za-z0-9_-]{1,100}$")
    name: str = Field(min_length=1, max_length=100)
    tenant_ids: list[str] = Field(default_factory=list, max_length=100)
    egress: EgressForm = Field(default_factory=EgressForm)


@router.get("")
def get_directory(_: Principal = Depends(require_admin)):
    """管理员查看组织架构总览：账号、部门、授权模板、业务范围及授权关系。"""
    return directory.overview()


@router.post("/validate")
def validate_assignment(body: AssignmentForm, _: Principal = Depends(require_admin)):
    """管理员预校验账号分配（业务范围、部门、上下级关系），不写入任何数据；不合法返回 400。"""
    try:
        directory.validate_assignment(body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@router.post("/accounts")
def assign_account(body: AssignmentForm, principal: Principal = Depends(require_admin)):
    """管理员在一个事务内分配账号：写入账号信息，并整体替换其业务范围授权与外发策略；校验失败返回 400。"""
    try:
        directory.assign(body.model_dump(), principal.email)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@router.post("/departments")
def add_department(body: DepartmentForm, _: Principal = Depends(require_admin)):
    """管理员新增或更新部门；上级部门不存在或上下级成环时返回 400。"""
    try:
        directory.department(body.id, body.name, body.parent_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@router.post("/templates")
def add_template(body: TemplateForm, _: Principal = Depends(require_admin)):
    """管理员新增或更新授权模板；包含不存在的业务范围时返回 400，不会改动已有账号的授权。"""
    try:
        directory.save_template(
            body.id, body.name, {"tenant_ids": body.tenant_ids, "egress": body.egress.model_dump()}
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}
