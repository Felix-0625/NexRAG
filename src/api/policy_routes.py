"""Administrator policy controls enforced along execution paths."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.api.dependencies import require_admin
from src.midplatform import agent_models, data_policy
from src.midplatform.tenant_access import Principal, get_principal, list_tenants

router = APIRouter(prefix="/workspace/policy", tags=["data-policy"])


class ResourcePolicyForm(BaseModel):
    """Resource classification and explicit data-egress switches."""

    classification: str = Field(pattern="^(public|internal|secret)$")
    external_models: bool = False
    external_search: bool = False
    external_tools: bool = False


@router.get("/tools")
def tool_catalog(principal: Principal = Depends(get_principal)):
    """List account-visible tools and models, management rights, and web-search configuration."""
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
    """List business-scope classifications and egress policies for administrators."""
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
    """Update scope policy; return 404 for unknown scopes and 400 for prohibited egress
    combinations.
    """
    if identifier not in list_tenants():
        raise HTTPException(404, "业务范围不存在")
    try:
        data_policy.save_resource_policy(
            "tenant", identifier, **body.model_dump(), changed_by=principal.email
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}
