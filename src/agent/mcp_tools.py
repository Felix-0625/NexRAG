"""Expose only approved MCP schemas to the existing agent loop."""

import re

from pydantic_ai.tools import Tool

from src.agent.authorization import current_policy
from src.midplatform import mcp_registry


def _invocation_clauses(prompt):
    """Keep affirmative execution clauses, excluding explanatory and negated requests."""
    from src.agent.task_intent import positive_request_text

    for clause in re.split(r"[，,。！？!?；;\n]", positive_request_text(prompt)):
        action = re.search(r"调用|使用|执行|\b(?:call|invoke|use|execute|run)\b", clause, re.I)
        if action is None:
            continue
        prefix = clause[: action.start()]
        if re.search(
            r"如何|怎样|怎么|解释|说明|介绍|\b(?:explain|describe|how\s+to)\b", prefix, re.I
        ):
            continue
        if re.search(r"\b(?:not|never|without)\b|\b\w+n['’]t\b", prefix, re.I):
            continue
        yield clause[action.end() :]


def requested_tool_ids(prompt, policy):
    """Resolve explicit invocation names only within this account's authorized scope.

    Allow descriptions and quotes between action and exact name, but consult neither earlier
    turns nor the global registry.
    """
    clauses = list(_invocation_clauses(prompt))
    requested = set()
    for tool_id in policy.allowed_tool_ids:
        parts = tool_id.split(":")
        if len(parts) != 3 or parts[0] != "mcp":
            continue
        name = re.compile(rf"(?<![A-Za-z0-9_.-]){re.escape(parts[2])}(?![A-Za-z0-9_.-])", re.I)
        if any(name.search(clause[:160]) for clause in clauses):
            requested.add(tool_id)
    return requested


def _explicit_mcp_names(prompt):
    # Unknown names come only from the user's request, never undisclosed registry entries; this detects revoked grants.
    names = set()
    for clause in _invocation_clauses(prompt):
        names.update(
            re.findall(
                r"\bMCP\b\s*(?:工具|tool)?\s*[:：]?\s*[`\"'“‘]?([A-Za-z_][A-Za-z0-9_.-]{0,63})",
                clause[:160],
                re.I,
            )
        )
    return names


def explicit_tool_refusal(prompt, policy):
    """Explain explicitly requested unavailable grants without replaying old answers.

    Inspect only this account's authorized names; recheck live grants and schema versions
    at invocation without disclosing the global registry.
    """
    requested = requested_tool_ids(prompt, policy)
    explicit_names = _explicit_mcp_names(prompt)
    granted_names = {tool_id.split(":", 2)[2].lower() for tool_id in requested}
    if not requested and not explicit_names:
        return None
    approved = {spec["tool_id"] for spec in mcp_registry.approved_catalog()}
    if (
        not policy.external_tools
        or policy.classification == "secret"
        or any(name.lower() not in granted_names for name in explicit_names)
        or any(tool_id not in approved for tool_id in requested)
    ):
        return "请求的 MCP 工具未获当前有效授权或审批已撤销，本次请求已拒绝，未调用工具、未发送参数。请联系管理员检查授权与审批。"
    return None


def approved_tools(deps):
    """Build at most 30 policy-approved tools and recheck approval versions at execution."""
    policy = current_policy(deps.context)
    if not policy.external_tools or policy.classification == "secret":
        return []
    selected = [
        spec
        for spec in mcp_registry.approved_catalog()
        if spec["tool_id"] in policy.allowed_tool_ids
    ][:30]
    tools = []

    def callback(spec):
        """Bind one approved tool specification into an invocation callback."""

        async def invoke(**arguments):
            """Recheck authorization before forwarding the bound tool invocation."""
            try:
                result = await mcp_registry.invoke_tool(
                    deps.context,
                    spec["tool_id"],
                    arguments,
                    revision=spec["revision"],
                    schema_hash=spec["schema_hash"],
                )
            except mcp_registry.MCPError as exc:
                return {"error": str(exc)}
            deps.evidence_calls += 1
            deps.ledger.add("tool", result, tool_id=spec["tool_id"])
            return result

        return invoke

    for index, spec in enumerate(selected):
        name = re.sub(r"[^A-Za-z0-9_-]", "_", spec["name"])[:40]
        tool = Tool.from_schema(
            callback(spec),
            name=f"mcp_{index}_{name}",
            description=f"Approved read-only MCP tool from {spec['server_name']}: {spec['description']}",
            json_schema=spec["parameters"],
        )
        tool.strict = False
        tools.append(tool)
    return tools
