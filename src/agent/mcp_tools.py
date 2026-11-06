"""只把已批准的 MCP schema 作为工具暴露给现有 Agent 循环。"""

import re

from pydantic_ai.tools import Tool

from src.agent.authorization import current_policy
from src.midplatform import mcp_registry


def _invocation_clauses(prompt):
    """保留肯定的执行请求，排除解释性语句和否定句。"""
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
    """只在本账号自己的授权范围内解析显式的调用名称。

    动作与确切名称之间可能夹有描述词和引号；不会参考先前轮次的文字，也不会查询全局注册表。
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
    # 未知的 MCP 名称只来自当前用户自己的请求，绝不来自未披露的注册表条目；这也能识别已被撤销的授权。
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
    """说明不可用的、被显式点名的授权，且不重放旧答案。

    只检查本账号授权内的名称，绝不披露全局注册表；调用时仍会重新检查实时授权和 schema 版本。
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
        async def invoke(**arguments):
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
