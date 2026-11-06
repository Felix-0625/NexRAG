"""MCP 审批必须限定在单智能体的实时授权路径内。"""

import asyncio
from unittest.mock import AsyncMock

from src.agent.dependencies import AgentDeps, UserContext
from src.agent.mcp_tools import approved_tools
from src.midplatform import mcp_registry
from src.midplatform.data_policy import save_account_policy, save_resource_policy

SPEC = dict(
    tool_id="mcp:synthetic:public_sum",
    name="public_sum",
    server_name="Synthetic",
    description="Add artificial numbers",
    parameters={"type": "object"},
    revision=3,
    schema_hash="a" * 64,
)


def context():
    return UserContext(user_id="employee", email="employee@local", tenant_id="")


def test_agent_does_not_register_ungranted_or_secret_mcp_tools(monkeypatch):
    monkeypatch.setattr(mcp_registry, "approved_catalog", lambda: [SPEC])
    assert approved_tools(AgentDeps(context())) == []
    save_account_policy(
        "employee@local", {"external_tools": True, "allowed_tool_ids": [SPEC["tool_id"]]}, "test"
    )
    secret = context()
    secret.knowledge_scopes = ["private"]
    save_resource_policy("knowledge", "private", "secret", False, False, False, "test")
    assert approved_tools(AgentDeps(secret)) == []


def test_agent_callback_pins_approval_and_records_real_result(monkeypatch):
    monkeypatch.setattr(mcp_registry, "approved_catalog", lambda: [SPEC])
    save_account_policy(
        "employee@local", {"external_tools": True, "allowed_tool_ids": [SPEC["tool_id"]]}, "test"
    )
    result = {"content": [{"type": "text", "text": '{"total":7}'}]}
    invoke = AsyncMock(return_value=result)
    monkeypatch.setattr(mcp_registry, "invoke_tool", invoke)
    deps = AgentDeps(context())
    tools = approved_tools(deps)
    assert len(tools) == 1
    assert asyncio.run(tools[0].function(left=2, right=5)) == result
    invoke.assert_awaited_once_with(
        deps.context, SPEC["tool_id"], {"left": 2, "right": 5}, revision=3, schema_hash="a" * 64
    )
    assert deps.evidence_calls == 1
    assert deps.ledger.entries[0]["data"] == result


def test_agent_denied_call_does_not_become_evidence(monkeypatch):
    monkeypatch.setattr(mcp_registry, "approved_catalog", lambda: [SPEC])
    save_account_policy(
        "employee@local", {"external_tools": True, "allowed_tool_ids": [SPEC["tool_id"]]}, "test"
    )
    monkeypatch.setattr(
        mcp_registry, "invoke_tool", AsyncMock(side_effect=mcp_registry.MCPDenied("revoked"))
    )
    deps = AgentDeps(context())
    result = asyncio.run(approved_tools(deps)[0].function(left=2, right=5))
    assert result == {"error": "revoked"}
    assert not deps.ledger.entries and deps.evidence_calls == 0
