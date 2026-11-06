"""管理员策略控制；策略决定在执行路径上强制生效。"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.api.ops_routes import require_admin
from src.midplatform import agent_models, data_policy
from src.midplatform.tenant_access import Principal, get_principal, list_tenants

router = APIRouter(prefix="/workspace/policy", tags=["data-policy"])


class ResourcePolicyForm(BaseModel):
    classification: str = Field(pattern="^(public|internal|secret)$")
    external_models: bool = False
    external_search: bool = False
    external_tools: bool = False


@router.get("/tools")
def tool_catalog(principal: Principal = Depends(get_principal)):
    """列出当前账号可见的工具与模型目录：管理员看到全部已审批 MCP 工具，普通用户只看到授权给自己的；同时返回是否可管理和联网搜索配置。"""
    from src.midplatform.mcp_registry import approved_catalog
    from src.midplatform.search_configuration import public_search_configuration

    account = data_policy.account_policy(principal.email)
    mcp_tools = [
        dict(
            id=item["tool_id"],
            name=f"{item['server_name']} · {item['name']}",
            description=item["description"],
            kind="mcp",
            allowed=item["tool_id"] in account.get("allowed_tool_ids", []),
        )
        for item in approved_catalog()
        if principal.role == "admin" or item["tool_id"] in account.get("allowed_tool_ids", [])
    ]
    return {
        "data": [
            {
                "id": "builtin:discount_quote",
                "kind": "builtin",
                "name": "折扣报价计算",
                "description": "精确计算原金额、折扣金额与报价；不读取文件、不联网、不执行源码。",
                "allowed": "builtin:discount_quote" in account.get("allowed_tool_ids", []),
            }
        ]
        + mcp_tools,
        "models": [
            agent_models.public_model(row)
            for row in agent_models.records()
            if agent_models.model_available(row, principal)
        ],
        "can_manage": principal.role == "admin",
        "web_search": public_search_configuration(),
    }


@router.get("")
def list_policies(_: Principal = Depends(require_admin)):
    """管理员列出各业务范围的数据分级与外发策略。"""
    return {
        "tenants": [
            data_policy.resource_policy("tenant", identifier) | {"label": label}
            for identifier, label in list_tenants().items()
        ]
    }


@router.post("/tenants/{identifier}")
def update_tenant_policy(
    identifier: str, body: ResourcePolicyForm, principal: Principal = Depends(require_admin)
):
    """管理员设置指定业务范围的数据分级与外发开关；业务范围不存在返回 404，机密数据开启外发等违规组合返回 400。"""
    if identifier not in list_tenants():
        raise HTTPException(404, "业务范围不存在")
    try:
        data_policy.save_resource_policy(
            "tenant", identifier, **body.model_dump(), changed_by=principal.email
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}
