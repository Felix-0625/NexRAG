"""Administrator-approved MCP capabilities for the existing agent.

Approval pins endpoint revisions and complete schemas. Account grants alone never permit
sending protected data to a server.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from urllib.parse import urlsplit
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken

from src.agent.authorization import refresh_policy, require_business
from src.config import settings
from src.midplatform.data_policy import is_loopback_endpoint
from src.midplatform.ops_store import dumps, get_ops_store, now

REGISTRY_KEY = "mcp_registry_v1"
AUDIT_KEY = "mcp_audit_v1"
NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MAX_ARGUMENT_BYTES = 16_384


class MCPError(ValueError):
    """Report MCP failure without fabricated results."""


class MCPDenied(MCPError):
    """Reject an MCP operation that current authorization does not permit."""

    pass


class MCPUnavailable(MCPError):
    """Report a safe MCP connectivity or transport failure."""

    pass


class MCPStaleApproval(MCPError):
    """Reject tool schemas or server revisions that no longer match approval."""

    pass


def _jsonschema():
    try:
        import jsonschema
    except ImportError as exc:
        raise MCPUnavailable("MCP schema 校验依赖未安装，请按锁文件安装依赖") from exc
    return jsonschema


def _admin(principal):
    if principal.role != "admin":
        raise MCPDenied("仅管理员可以维护 MCP 连接和审批工具")


def _cipher():
    secret = settings.NEXRAG_USER_JWT_SECRET
    if not secret or len(secret) < 32:
        raise MCPUnavailable("服务端密钥未配置，不能保存或读取 MCP 凭据")
    key = hashlib.sha256(("nexrag-mcp-credentials:" + secret).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _token(server):
    try:
        return (
            _cipher().decrypt(server["encrypted_token"].encode()).decode()
            if server["encrypted_token"]
            else ""
        )
    except InvalidToken as exc:
        raise MCPUnavailable("MCP 凭据无法解密，请管理员重新填写") from exc


def _registry(connection=None):
    if connection is None:
        row = get_ops_store().query_one(
            "SELECT value FROM workspace_settings WHERE key=?", (REGISTRY_KEY,)
        )
    else:
        row = connection.execute(
            "SELECT value FROM workspace_settings WHERE key=?", (REGISTRY_KEY,)
        ).fetchone()
    return json.loads(row["value"]) if row else {}


def _write(connection, records):
    connection.execute(
        "INSERT OR REPLACE INTO workspace_settings VALUES (?,?)", (REGISTRY_KEY, dumps(records))
    )


def _audit(connection, action, actor_id, target, status="ok"):
    row = connection.execute(
        "SELECT value FROM workspace_settings WHERE key=?", (AUDIT_KEY,)
    ).fetchone()
    events = json.loads(row["value"]) if row else []
    events.append(
        dict(action=action, actor_id=actor_id, target=target, status=status, created_at=now())
    )
    connection.execute(
        "INSERT OR REPLACE INTO workspace_settings VALUES (?,?)", (AUDIT_KEY, dumps(events[-200:]))
    )


def audit_events(principal):
    """Return up to 200 recent audit events for administrators."""
    _admin(principal)
    row = get_ops_store().query_one(
        "SELECT value FROM workspace_settings WHERE key=?", (AUDIT_KEY,)
    )
    return json.loads(row["value"]) if row else []


def _get(identifier):
    server = _registry().get(identifier)
    if server is None:
        raise MCPError("MCP 连接不存在")
    return server


def _public(server):
    return {key: value for key, value in server.items() if key != "encrypted_token"} | {
        "has_token": bool(server["encrypted_token"])
    }


def list_servers(principal):
    """List secret-free server views with has_token for administrators."""
    _admin(principal)
    return [_public(server) for server in _registry().values()]


def _url(value):
    if not isinstance(value, str):
        raise MCPError("MCP 地址必须是字符串")
    try:
        parsed = urlsplit(value.strip())
        _ = parsed.port
    except ValueError as exc:
        raise MCPError("MCP 地址或端口格式无效") from exc
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or any(ord(character) < 32 for character in value)
        or parsed.scheme not in {"http", "https"}
        or len(value) > 2048
    ):
        raise MCPError("MCP 地址须为固定 HTTP(S) 地址，不能包含凭据、查询参数或片段")
    if parsed.scheme != "https" and not is_loopback_endpoint(value):
        raise MCPError("远程 MCP 必须使用 HTTPS；HTTP 仅允许本机 loopback 服务")
    return value.strip()


def save_server(
    principal,
    *,
    name: str,
    url: str,
    identifier: str | None = None,
    token: str | None = None,
    clear_token: bool = False,
    enabled: bool = True,
    transport: str = "streamable_http",
):
    """Save an administrator-owned Streamable HTTP server.

    Encrypt keys, increment revisions, and clear discovered tools and approvals on save.
    Endpoint changes require replacement or clearing of credentials; return a public view.
    """
    _admin(principal)
    if transport != "streamable_http":
        raise MCPError("仅支持 MCP Streamable HTTP，禁止 stdio、命令和源码执行")
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        raise MCPError("MCP 名称必须为 1 至 100 字符")
    if type(enabled) is not bool or type(clear_token) is not bool:
        raise MCPError("MCP 启用和清除凭据选项必须是布尔值")
    url = _url(url)
    if token is not None and (
        not isinstance(token, str) or len(token) > 4096 or any(c in token for c in "\r\n")
    ):
        raise MCPError("MCP token 格式无效")
    identifier = identifier or uuid4().hex
    if not NAME.fullmatch(identifier):
        raise MCPError("MCP 连接 ID 格式无效")
    with get_ops_store().transaction() as connection:
        records = _registry(connection)
        previous = records.get(identifier)
        if (
            previous
            and previous["url"] != url
            and previous["encrypted_token"]
            and token is None
            and not clear_token
        ):
            raise MCPError("更换 MCP 地址时请重新填写凭据或明确清除旧凭据")
        record = dict(
            id=identifier,
            name=name.strip(),
            url=url,
            enabled=enabled,
            transport=transport,
            encrypted_token=(previous or {}).get("encrypted_token", ""),
            revision=(previous or {}).get("revision", 0) + 1,
            tools={},
            changed_by=principal.user_id,
            updated_at=now(),
        )
        if clear_token or token is not None:
            record["encrypted_token"] = _cipher().encrypt(token.encode()).decode() if token else ""
        # Saving intentionally invalidates approvals; discover and review current tools
        # before distributing this revision.
        records[identifier] = record
        _write(connection, records)
        _audit(connection, "mcp.server.save", principal.user_id, identifier)
    return _public(record)


def _safe_schema(schema):
    if not isinstance(schema, dict) or len(dumps(schema).encode()) > 32_768:
        raise MCPError("MCP 工具 schema 必须是有限大小的 JSON 对象")

    def walk(value, depth=0):
        """Validate nested schema content within the depth and reference limits."""
        if depth > 30:
            raise MCPError("MCP 工具 schema 嵌套过深")
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"$ref", "$dynamicRef", "$recursiveRef"} and (
                    not isinstance(item, str) or not item.startswith("#/")
                ):
                    raise MCPError("MCP schema 禁止外部引用")
                if key == "x-mcp-header":
                    raise MCPError("MCP 工具不允许把参数映射为 HTTP 请求头")
                walk(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                walk(item, depth + 1)

    walk(schema)
    jsonschema = _jsonschema()
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError as exc:
        raise MCPError("MCP 工具提供了无效 JSON Schema") from exc


def normalize_spec(raw: dict) -> tuple[dict, str]:
    """Validate tool names, sizes, and self-contained schemas; return spec and SHA-256."""
    name = raw.get("name", "")
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise MCPError("MCP 工具名称格式无效")
    spec = {
        key: raw[key]
        for key in ("name", "description", "inputSchema", "outputSchema", "annotations")
        if key in raw
    }
    if len(dumps(spec).encode()) > 65_536 or not isinstance(spec.get("description", ""), str):
        raise MCPError("MCP 工具描述或 schema 过大或无效")
    _safe_schema(spec.get("inputSchema"))
    if spec["inputSchema"].get("type") != "object":
        raise MCPError("MCP 工具输入 schema 顶层必须是 object")
    if "outputSchema" in spec:
        _safe_schema(spec["outputSchema"])
    digest = hashlib.sha256(
        json.dumps(spec, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()
    return spec, digest


async def discover_server(principal, identifier):
    """Discover enabled-server tools for administrators.

    Keep approvals only for unchanged hashes. Reject concurrent configuration changes with
    MCPStaleApproval.
    """
    _admin(principal)
    from src.midplatform.mcp_transport import probe

    snapshot = _get(identifier)
    if not snapshot["enabled"]:
        raise MCPDenied("MCP 连接已停用")
    discovered = {}
    for raw in await probe(snapshot["url"], _token(snapshot)):
        spec, digest = normalize_spec(raw)
        if spec["name"] in discovered:
            raise MCPError("MCP 返回重复工具名称")
        discovered[spec["name"]] = dict(spec=spec, schema_hash=digest, approved=False)
    with get_ops_store().transaction() as connection:
        records = _registry(connection)
        current = records[identifier]
        if current["revision"] != snapshot["revision"]:
            raise MCPStaleApproval("MCP 配置已变更，请重新发现工具")
        for name, tool in discovered.items():
            old = current["tools"].get(name)
            if old and old["schema_hash"] == tool["schema_hash"]:
                discovered[name] = old
        if discovered != current["tools"]:
            current.update(tools=discovered, revision=current["revision"] + 1, updated_at=now())
        _write(connection, records)
        _audit(connection, "mcp.server.discover", principal.user_id, identifier)
    return _public(current)


def approve_tool(
    principal, identifier, name, *, revision: int, schema_hash: str, read_only_confirmed: bool
):
    """Approve an explicitly read-only tool with matching revision and hash.

    Reject denied or stale approvals and increment the server revision on success.
    """
    _admin(principal)
    if read_only_confirmed is not True:
        raise MCPDenied("当前只支持经管理员确认的只读 MCP 工具；工具声明不是安全保证")
    with get_ops_store().transaction() as connection:
        records = _registry(connection)
        server = records.get(identifier)
        tool = (server or {}).get("tools", {}).get(name)
        if (
            not server
            or not server["enabled"]
            or not tool
            or server["revision"] != revision
            or tool["schema_hash"] != schema_hash
        ):
            raise MCPStaleApproval("MCP 工具或配置已变化，请重新查看后审批")
        tool.update(approved=True, approved_by=principal.user_id, approved_at=now())
        server["revision"] += 1
        _write(connection, records)
        _audit(connection, "mcp.tool.approve", principal.user_id, f"mcp:{identifier}:{name}")
    return _public(server)


def revoke_tool(principal, identifier, name):
    """Revoke approval and increment server revision; reject unknown tools."""
    _admin(principal)
    with get_ops_store().transaction() as connection:
        records = _registry(connection)
        server = records.get(identifier)
        if not server or name not in server["tools"]:
            raise MCPError("MCP 工具不存在")
        server["tools"][name]["approved"] = False
        server["revision"] += 1
        _write(connection, records)
        _audit(connection, "mcp.tool.revoke", principal.user_id, f"mcp:{identifier}:{name}")
    return _public(server)


def approved_catalog():
    """Return nonsecret descriptions, deferring account and data-policy enforcement to use."""
    return [
        dict(
            tool_id=f"mcp:{server['id']}:{name}",
            server_name=server["name"],
            name=name,
            description=tool["spec"].get("description", ""),
            parameters=tool["spec"]["inputSchema"],
            revision=server["revision"],
            schema_hash=tool["schema_hash"],
        )
        for server in _registry().values()
        if server["enabled"]
        for name, tool in server["tools"].items()
        if tool["approved"]
    ]


async def _authorized(context, tool_id, revision, schema_hash):
    try:
        policy = await refresh_policy(context)
        if context.tenant_id:
            require_business(context)
    except ValueError as exc:
        raise MCPDenied("当前账号或数据授权已变更，MCP 未执行，请重新开始对话") from exc
    if (
        policy.classification == "secret"
        or not policy.external_tools
        or tool_id not in policy.allowed_tool_ids
    ):
        raise MCPDenied("当前数据范围或账号禁止此 MCP 工具，未发送参数")
    parts = tool_id.split(":")
    if len(parts) != 3 or parts[0] != "mcp":
        raise MCPDenied("无效的 MCP 工具 ID")
    server = _get(parts[1])
    tool = server["tools"].get(parts[2])
    if (
        not server["enabled"]
        or not tool
        or not tool["approved"]
        or server["revision"] != revision
        or tool["schema_hash"] != schema_hash
    ):
        raise MCPStaleApproval("MCP 授权或 schema 已变化，本轮旧工具未执行")
    return server, tool


async def _invoke_tool(context, tool_id, arguments, *, revision, schema_hash):
    server, tool = await _authorized(context, tool_id, revision, schema_hash)
    if (
        not isinstance(arguments, dict)
        or len(json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode())
        > MAX_ARGUMENT_BYTES
    ):
        raise MCPError("MCP 参数必须是有限大小的 JSON 对象")
    jsonschema = _jsonschema()
    try:
        jsonschema.Draft202012Validator(tool["spec"]["inputSchema"]).validate(arguments)
    except jsonschema.ValidationError as exc:
        raise MCPError("MCP 参数与审批 schema 不符") from exc
    from src.midplatform.mcp_transport import call

    async def recheck():
        """Revalidate the current MCP approval immediately before invocation."""
        await _authorized(context, tool_id, revision, schema_hash)

    return await call(
        server["url"],
        _token(server),
        tool["spec"]["name"],
        arguments,
        schema_hash=schema_hash,
        before_call=recheck,
    )


async def invoke_tool(context, tool_id: str, arguments: dict, *, revision: int, schema_hash: str):
    """Recheck grants before connection and invocation; audit metadata only, never content."""
    try:
        result = await _invoke_tool(
            context, tool_id, arguments, revision=revision, schema_hash=schema_hash
        )
    except Exception as exc:
        with get_ops_store().transaction() as connection:
            _audit(
                connection,
                "mcp.tool.call",
                context.user_id,
                tool_id,
                "denied" if isinstance(exc, MCPDenied) else "failed",
            )
        raise
    with get_ops_store().transaction() as connection:
        _audit(connection, "mcp.tool.call", context.user_id, tool_id)
    return result
