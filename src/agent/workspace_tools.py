"""Audited built-in and explicitly allowed bridge capabilities supplied per request.

A ticket alone is insufficient: recheck account and resource policy immediately before
external tool arguments leave the agent.
"""

from __future__ import annotations

import httpx
from pydantic_ai import ModelRetry
from pydantic_ai.tools import Tool
from pydantic_ai.toolsets import FunctionToolset

from src.agent.authorization import current_policy, refresh_policy
from src.agent.builtin_tools import DISCOUNT_TOOL_ID
from src.agent.dependencies import AgentDeps
from src.config import settings


def _business_access_tool(deps: AgentDeps) -> Tool:
    """Build the scope-check tool only when this run requires database evidence."""
    from src.agent.business_access import check_requested_scope

    def check_business_access(scope_id: str):
        """Check explicit scope access using the current turn authorization."""
        return check_requested_scope(deps, scope_id)

    return Tool(
        check_business_access,
        name="check_business_access",
        description="Verify permission only when the user explicitly requests a different business scope ID. Use that exact ID from the current request. Does not read rows or switch scope. A server refusal is authoritative; do not query your own scope as a substitute. For the current selected scope, query_database directly.",
        strict=False,
    )


def _discount_quote_tool(deps: AgentDeps) -> Tool:
    """Build a discount tool that rechecks revocation on every call."""
    context = deps.context

    async def discount_quote(
        basis_evidence_id: str | None = None,
        rate_evidence_id: str | None = None,
        amount_column: str | None = None,
    ):
        """Calculate a quote from authorized evidence selectors, not model-written values."""
        if DISCOUNT_TOOL_ID not in (await refresh_policy(context)).allowed_tool_ids:
            return {"error": "折扣计算工具授权已撤销，未执行计算"}
        from src.agent.derived_calculation import (
            CALCULATION,
            discount_retry_context,
            quote_from_evidence,
        )

        try:
            result, binding = quote_from_evidence(
                deps.ledger.entries,
                deps.ledger.prompt,
                basis_evidence_id=basis_evidence_id,
                rate_evidence_id=rate_evidence_id,
                amount_column=amount_column,
            )
        except ValueError as exc:
            return {
                "error": str(exc),
                **discount_retry_context(deps.ledger.entries, deps.ledger.prompt),
            }
        deps.evidence_calls += 1
        evidence_id = deps.ledger.add(
            "tool",
            result,
            tool_id=DISCOUNT_TOOL_ID,
            calculation_type=CALCULATION,
            binding=binding,
        )
        return {**result, "evidence_id": evidence_id, "calculation_basis": binding}

    return Tool(
        discount_quote,
        name="calculate_discount_quote",
        description="Calculate discount and net quote using this turn's successful SQL and policy evidence. The server reads the amount and percentage directly; do not supply numeric amounts or rates. Optional selectors are ledger IDs such as DB2 and KB1, not filenames. Omit selectors when evidence values are unique; specify them when ambiguous. No network or file access.",
        strict=False,
    )


def _bridge_callback(deps: AgentDeps, original_name, tool_id):
    """Build a bridge callback that revalidates policy before forwarding its ticket."""
    context = deps.context

    async def invoke(**arguments):
        """Recheck authorization before forwarding the bound tool invocation."""
        policy = await refresh_policy(context)
        if not policy.external_tools or tool_id not in policy.allowed_tool_ids:
            return {"error": "当前数据范围或账号未授权调用此工具，未发送参数"}
        async with httpx.AsyncClient(timeout=50, trust_env=False) as client:
            response = await client.post(
                settings.WEBUI_TOOL_BRIDGE_URL.rstrip("/") + "/api/v1/nexrag/tools/invoke",
                headers={"Authorization": f"Bearer {settings.NEXRAG_SHARED_API_KEY}"},
                json={
                    "ticket": context.workspace_tool_ticket,
                    "name": original_name,
                    "arguments": arguments,
                    "user_id": context.user_id,
                },
            )
        if response.status_code == 422:
            raise ModelRetry("Use the exact parameter names and types: " + response.text[:1500])
        if response.status_code != 200:
            return {"error": f"Workspace tool rejected the call ({response.status_code})."}
        deps.evidence_calls += 1
        deps.ledger.add("tool", response.json())
        return response.json()

    return invoke


def _bridge_tools(deps: AgentDeps) -> list[Tool]:
    """Convert valid request tool declarations into numbered bridge tools."""
    tools = []
    for index, spec in enumerate(deps.context.workspace_tools):
        if not spec.get("name") or not isinstance(spec.get("parameters"), dict):
            continue
        tool = Tool.from_schema(
            _bridge_callback(deps, spec["name"], spec.get("tool_id")),
            name=f"workspace_{index}_{spec['name'][:45]}",
            description=spec.get("description"),
            json_schema=spec["parameters"],
        )
        tool.strict = False
        tools.append(tool)
    return tools


def build_workspace_toolset(deps: AgentDeps) -> FunctionToolset:
    """Assemble tools from current obligations, live grants, and bridge tickets without
    cross-account caching.
    """
    from src.agent.mcp_tools import approved_tools

    functions = approved_tools(deps)
    context = deps.context
    if "db" in deps.ledger.required:
        functions.append(_business_access_tool(deps))
    if DISCOUNT_TOOL_ID in current_policy(context).allowed_tool_ids:
        functions.append(_discount_quote_tool(deps))
    if context.workspace_tool_ticket:
        functions.extend(_bridge_tools(deps))
    return FunctionToolset(tools=functions, max_retries=2, strict=False)
