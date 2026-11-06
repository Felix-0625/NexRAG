"""经管理员审核的 MCP 接口；凭据与调用均保留在服务端。"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.api.ops_routes import require_admin
from src.midplatform import mcp_registry as registry
from src.midplatform.tenant_access import Principal

router = APIRouter(prefix="/workspace/mcp", tags=["mcp"])


def failure(exc):
    """把 MCP 异常转为 HTTP 错误：拒绝 403、审批过期 409、服务不可用 503，其余 400。"""
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
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(max_length=2048)
    identifier: str | None = Field(default=None, max_length=64)
    token: str | None = Field(default=None, max_length=4096)
    clear_token: bool = False
    enabled: bool = True


class ApprovalForm(BaseModel):
    revision: int = Field(ge=1)
    schema_hash: str = Field(pattern="^[a-f0-9]{64}$")
    read_only_confirmed: bool = False


@router.get("")
def servers(principal: Principal = Depends(require_admin)):
    """管理员列出已登记的 MCP 连接（不含凭据，仅标明是否已设置）及最近的审计事件。"""
    return {"data": registry.list_servers(principal), "events": registry.audit_events(principal)}


@router.post("")
def save(body: ServerForm, principal: Principal = Depends(require_admin)):
    """管理员新增或更新 MCP 连接；保存会使该连接已有的工具审批失效，需重新发现并审批。"""
    try:
        return registry.save_server(principal, **body.model_dump())
    except registry.MCPError as exc:
        failure(exc)


@router.post("/{identifier}/discover")
async def discover(identifier: str, principal: Principal = Depends(require_admin)):
    """管理员连接指定 MCP 服务并发现其公布的工具；新发现或变更的工具默认未审批。"""
    try:
        return await registry.discover_server(principal, identifier)
    except registry.MCPError as exc:
        failure(exc)


@router.post("/{identifier}/tools/{name}/approve")
def approve(
    identifier: str, name: str, body: ApprovalForm, principal: Principal = Depends(require_admin)
):
    """管理员批准一个 MCP 工具；须确认其为只读，且连接修订号与工具 schema 哈希仍与当前一致，否则返回 409。"""
    try:
        return registry.approve_tool(principal, identifier, name, **body.model_dump())
    except registry.MCPError as exc:
        failure(exc)


@router.post("/{identifier}/tools/{name}/revoke")
def revoke(identifier: str, name: str, principal: Principal = Depends(require_admin)):
    """管理员撤销一个 MCP 工具的审批，撤销后 Agent 不再能调用它。"""
    try:
        return registry.revoke_tool(principal, identifier, name)
    except registry.MCPError as exc:
        failure(exc)
