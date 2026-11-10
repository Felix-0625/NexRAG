"""Bounded official MCP 2.x Streamable HTTP transport without shell execution.

Enter, call, and exit in one task. Do not expose TLS-disable or redirect options, including
redirects the SDK might otherwise follow.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version
from urllib.parse import urlsplit

from src.midplatform.mcp_registry import MCPError, MCPStaleApproval, MCPUnavailable, normalize_spec

TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 262_144
MAX_RESULT_BYTES = 65_536
MAX_TOOLS = 100


async def _target_ip(url):
    """Resolve DNS once and pin it, rejecting remote hosts rebound onto local networks."""
    parsed = urlsplit(url)
    try:
        literal = ipaddress.ip_address(parsed.hostname or "")
        addresses = [literal]
    except ValueError:
        literal = None
        records = await asyncio.get_running_loop().getaddrinfo(
            parsed.hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
        addresses = list({ipaddress.ip_address(record[4][0]) for record in records})
    declared_loopback = parsed.hostname == "localhost" or bool(literal and literal.is_loopback)
    if not addresses or (
        declared_loopback and not all(address.is_loopback for address in addresses)
    ):
        raise MCPError("本机 MCP 地址解析到非 loopback 目标，连接已拒绝")
    if not declared_loopback and (
        parsed.scheme != "https" or any(not address.is_global for address in addresses)
    ):
        raise MCPError("远程 MCP 必须使用 HTTPS 公网地址，禁止解析到内网、链路本地或保留地址")
    # Prefer IPv4 and do not resolve again during connection;
    # TLS still verifies the original hostname through SNI.
    return str(sorted(addresses, key=lambda address: (address.version, str(address)))[0])


def _sdk():
    try:
        major, minor = (int(part) for part in version("mcp").split(".")[:2])
        if major != 2 or minor < 2:
            raise MCPUnavailable("MCP 需要官方 SDK >=2.2,<3，请按锁文件安装依赖")
        import httpx2
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        from mcp_types import PaginatedRequestParams
    except (ImportError, PackageNotFoundError) as exc:
        raise MCPUnavailable("官方 MCP SDK 未安装，MCP 当前不可用；请按锁文件安装依赖") from exc
    return httpx2, ClientSession, streamable_http_client, PaginatedRequestParams


def guarded_transport(inner=None, *, endpoint=None):
    """Guard response sizes with injectable transport for offline protocol tests."""
    httpx2, _, _, _ = _sdk()

    class BoundedStream(httpx2.AsyncByteStream):
        """Count streamed response bytes before they reach the MCP parser."""

        def __init__(self, stream, budget):
            self.stream, self.budget = stream, budget

        async def __aiter__(self):
            async for chunk in self.stream:
                self.budget[0] += len(chunk)
                if self.budget[0] > MAX_RESPONSE_BYTES:
                    raise MCPError("MCP 响应超过安全大小限制")
                yield chunk

        async def aclose(self):
            """Close the wrapped HTTP resource."""
            await self.stream.aclose()

    class Guard(httpx2.AsyncBaseTransport):
        """Enforce pinned destinations and bounded MCP HTTP responses."""

        def __init__(self):
            self.inner = inner or httpx2.AsyncHTTPTransport(verify=True, trust_env=False, retries=0)
            self.budget = [0]
            self.target_ip = None

        async def handle_async_request(self, request):
            """Validate and forward an MCP request through the guarded transport."""
            outbound = request
            if endpoint is not None:
                target = httpx2.URL(endpoint)
                if (request.url.scheme, request.url.host, request.url.port, request.url.path) != (
                    target.scheme,
                    target.host,
                    target.port,
                    target.path,
                ):
                    raise MCPError("MCP 请求偏离管理员固定的服务地址")
                if self.target_ip is None:
                    self.target_ip = await _target_ip(endpoint)
                outbound = httpx2.Request(
                    request.method,
                    request.url.copy_with(host=self.target_ip),
                    headers=request.headers,
                    stream=request.stream,
                    extensions={**request.extensions, "sni_hostname": target.host},
                )
            response = await self.inner.handle_async_request(outbound)
            response.request = request
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                await response.aclose()
                raise MCPError("MCP 服务未遵守禁用压缩约定，响应未读取")
            if 300 <= response.status_code < 400:
                await response.aclose()
                raise MCPError("MCP 不允许重定向，请管理员直接配置最终服务地址")
            declared = response.headers.get("content-length")
            if declared and (not declared.isdigit() or int(declared) > MAX_RESPONSE_BYTES):
                await response.aclose()
                raise MCPError("MCP 响应超过安全大小限制")
            response.stream = BoundedStream(response.stream, self.budget)
            return response

        async def aclose(self):
            """Close the wrapped HTTP resource."""
            await self.inner.aclose()

    return Guard()


@asynccontextmanager
async def _session(url, token):
    httpx2, ClientSession, streamable_http_client, _ = _sdk()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    # Disable compression so the byte limit bounds parsed JSON and SSE,
    # not a decompression bomb's compressed wire representation.
    headers["Accept-Encoding"] = "identity"
    async with httpx2.AsyncClient(
        transport=guarded_transport(endpoint=url),
        headers=headers,
        timeout=httpx2.Timeout(TIMEOUT_SECONDS, connect=5),
        trust_env=False,
        follow_redirects=False,
    ) as client:
        async with streamable_http_client(url, http_client=client) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=TIMEOUT_SECONDS) as session:
                await session.initialize()
                yield session


async def _tools(session):
    _, _, _, PaginatedRequestParams = _sdk()
    tools, cursors, cursor = [], set(), None
    while True:
        page = await session.list_tools(
            params=PaginatedRequestParams(cursor=cursor) if cursor else None
        )
        tools.extend(
            tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in page.tools
        )
        if len(tools) > MAX_TOOLS:
            raise MCPError("MCP 工具数量超过上限")
        cursor = page.next_cursor
        if not cursor:
            return tools
        if cursor in cursors or len(cursors) >= 20:
            raise MCPError("MCP 工具分页无效或超过上限")
        cursors.add(cursor)


async def probe(url, token):
    """Perform real initialization and tools/list without fabricated fallback."""
    try:
        async with asyncio.timeout(TIMEOUT_SECONDS):
            async with _session(url, token) as session:
                return await _tools(session)
    except TimeoutError as exc:
        raise MCPUnavailable("MCP 连接或工具发现超时") from exc
    except Exception as exc:
        raise _safe_error(exc) from exc


async def call(url, token, name, arguments, *, schema_hash, before_call):
    """Call a remote tool and return bounded JSON.

    Require the approved schema hash and recheck grants immediately before the call. Raise
    safe MCP errors for oversized responses, tool failures, and timeouts.
    """
    try:
        async with asyncio.timeout(TIMEOUT_SECONDS):
            async with _session(url, token) as session:
                offered = [
                    normalize_spec(raw) for raw in await _tools(session) if raw.get("name") == name
                ]
                if len(offered) != 1 or offered[0][1] != schema_hash:
                    raise MCPStaleApproval("远端 MCP 工具 schema 或描述已变化，请重新发现并审批")
                await before_call()
                result = await session.call_tool(
                    name, arguments=arguments, read_timeout_seconds=TIMEOUT_SECONDS
                )
                payload = result.model_dump(mode="json", by_alias=True, exclude_none=True)
                if len(json.dumps(payload, ensure_ascii=False).encode()) > MAX_RESULT_BYTES:
                    raise MCPError("MCP 工具结果超过安全大小限制")
                if payload.get("isError"):
                    raise MCPError("MCP 工具报告执行失败")
                return payload
    except TimeoutError as exc:
        raise MCPUnavailable("MCP 工具调用超时") from exc
    except Exception as exc:
        raise _safe_error(exc) from exc


def _safe_error(exc):
    """Unwrap TaskGroup failures without exposing raw network or credential details."""
    if isinstance(exc, MCPError):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for error in exc.exceptions:
            translated = _safe_error(error)
            if not isinstance(translated, MCPUnavailable):
                return translated
    return MCPUnavailable("MCP 连接或协议失败，请检查已审批的服务地址与凭据")
