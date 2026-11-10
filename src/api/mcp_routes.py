"""Administrator-reviewed MCP endpoints that keep credentials and calls server-side."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.api.dependencies import require_admin
from src.midplatform import mcp_registry as registry
from src.midplatform.tenant_access import Principal

router = APIRouter(prefix="/workspace/mcp", tags=["mcp"])


def failure(exc):
    """Map MCP denial, stale approval, and unavailability to 403, 409, and 503; use 400 otherwise."""
    code = (
        403
        if isinstance(exc, registry.MCPDenied)
        else 409
        if isinstance(exc, registry.MCPStaleApproval)
        else 503
        if isinstance(exc, registry.MCPUnavailable)
        else 400
    )
    raise HTTPException(code, str(exc)) from exc


class ServerForm(BaseModel):
    """MCP server configuration and credential update request."""

    name: str = Field(min_length=1, max_length=100)
    url: str = Field(max_length=2048)
    identifier: str | None = Field(default=None, max_length=64)
    token: str | None = Field(default=None, max_length=4096)
    clear_token: bool = False
    enabled: bool = True


class ApprovalForm(BaseModel):
    """Explicit read-only approval pinned to a server revision and schema."""

    revision: int = Field(ge=1)
    schema_hash: str = Field(pattern="^[a-f0-9]{64}$")
    read_only_confirmed: bool = False


@router.get("")
def servers(principal: Principal = Depends(require_admin)):
    """List registered MCP servers and recent audit events without disclosing credentials."""
    return {"data": registry.list_servers(principal), "events": registry.audit_events(principal)}


@router.post("")
def save(body: ServerForm, principal: Principal = Depends(require_admin)):
    """Save a server and invalidate its tool approvals until rediscovery and review."""
    try:
        return registry.save_server(principal, **body.model_dump())
    except registry.MCPError as exc:
        failure(exc)


@router.post("/{identifier}/discover")
async def discover(identifier: str, principal: Principal = Depends(require_admin)):
    """Discover tools on a server, leaving new or changed schemas unapproved."""
    try:
        return await registry.discover_server(principal, identifier)
    except registry.MCPError as exc:
        failure(exc)


@router.post("/{identifier}/tools/{name}/approve")
def approve(
    identifier: str, name: str, body: ApprovalForm, principal: Principal = Depends(require_admin)
):
    """Approve an explicitly read-only tool only if its server revision and schema hash still
    match.
    """
    try:
        return registry.approve_tool(principal, identifier, name, **body.model_dump())
    except registry.MCPError as exc:
        failure(exc)


@router.post("/{identifier}/tools/{name}/revoke")
def revoke(identifier: str, name: str, principal: Principal = Depends(require_admin)):
    """Revoke tool approval so the agent can no longer invoke it."""
    try:
        return registry.revoke_tool(principal, identifier, name)
    except registry.MCPError as exc:
        failure(exc)
