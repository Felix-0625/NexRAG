"""Administrator-only organization and atomic account-assignment endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from src.api.dependencies import require_admin
from src.midplatform import directory
from src.midplatform.tenant_access import Principal

router = APIRouter(prefix="/workspace/directory", tags=["organization"])


class EgressForm(BaseModel):
    """Account-level model, tool, and search egress grants."""

    external_models: bool = False
    external_search: bool = False
    external_tools: bool = False
    allowed_model_ids: list[str] = Field(default_factory=list, max_length=100)
    allowed_tool_ids: list[str] = Field(default_factory=list, max_length=100)


class AssignmentForm(BaseModel):
    """Validated account assignment and replacement grant request."""

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
        """Normalize email case after requiring an at-sign and forbidding whitespace."""
        if "@" not in value or any(char.isspace() for char in value):
            raise ValueError("邮箱格式无效")
        return value.lower()


class DepartmentForm(BaseModel):
    """Department identity and optional reporting parent."""

    id: str = Field(pattern="^[A-Za-z0-9_-]{1,100}$")
    name: str = Field(min_length=1, max_length=100)
    parent_id: str = Field(default="", max_length=100)


class IdentityForm(BaseModel):
    """Account identity metadata without authentication credentials."""

    user_id: str = Field(min_length=1, max_length=128)
    email: str = Field(min_length=3, max_length=254)
    name: str = Field(min_length=1, max_length=100)


@router.post("/identities")
def register_identity(body: IdentityForm, _: Principal = Depends(require_admin)):
    """Import identity metadata without creating credentials or grants; ignore existing identities."""
    # Directory metadata creates neither credentials nor access grants.
    directory.store().execute(
        "INSERT OR IGNORE INTO company_accounts VALUES (?,?,?,?,?,?,?,?)",
        (body.user_id, body.email.lower(), body.name, "", "", "", "identity-import", 0),
    )
    return {"ok": True}


class TemplateForm(BaseModel):
    """Reusable explicit business-scope and egress grants."""

    id: str = Field(pattern="^[A-Za-z0-9_-]{1,100}$")
    name: str = Field(min_length=1, max_length=100)
    tenant_ids: list[str] = Field(default_factory=list, max_length=100)
    egress: EgressForm = Field(default_factory=EgressForm)


@router.get("")
def get_directory(_: Principal = Depends(require_admin)):
    """Return accounts, departments, templates, business scopes, and grants for administrators."""
    return directory.overview()


@router.post("/validate")
def validate_assignment(body: AssignmentForm, _: Principal = Depends(require_admin)):
    """Validate assignment without writes; reject invalid scopes, departments, or reporting lines
    with 400.
    """
    try:
        directory.validate_assignment(body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@router.post("/accounts")
def assign_account(body: AssignmentForm, principal: Principal = Depends(require_admin)):
    """Atomically assign account details and replace scope and egress grants; reject invalid input
    with 400.
    """
    try:
        directory.assign(body.model_dump(), principal.email)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@router.post("/departments")
def add_department(body: DepartmentForm, _: Principal = Depends(require_admin)):
    """Create or update a department; reject missing parents or cycles with 400."""
    try:
        directory.department(body.id, body.name, body.parent_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@router.post("/templates")
def add_template(body: TemplateForm, _: Principal = Depends(require_admin)):
    """Save a template without changing assigned accounts; reject unknown scopes with 400."""
    try:
        directory.save_template(
            body.id, body.name, {"tenant_ids": body.tenant_ids, "egress": body.egress.model_dump()}
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}
