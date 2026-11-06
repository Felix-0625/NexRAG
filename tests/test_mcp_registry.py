"""用模拟 HTTP 验证 MCP 审批、出站拒绝以及真实 SDK 协议。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from src.agent.dependencies import UserContext
from src.config import settings
from src.midplatform import mcp_registry as registry
from src.midplatform import mcp_transport as transport
from src.midplatform.data_policy import DataAccessPolicy, save_account_policy
from src.midplatform.tenant_access import Principal

ADMIN = Principal("synthetic-admin", "admin@local", "admin")
EMPLOYEE = Principal("synthetic-employee", "employee@local", "user")
SPEC = {
    "name": "public_sum",
    "description": "Sum invented public numbers.",
    "inputSchema": {
        "type": "object",
        "properties": {"left": {"type": "integer"}, "right": {"type": "integer"}},
        "required": ["left", "right"],
        "additionalProperties": False,
    },
}


def run(awaitable):
    return asyncio.run(awaitable)


def allow(monkeypatch, tool_id):
    policy = DataAccessPolicy("public", external_tools=True, allowed_tool_ids=(tool_id,))
    monkeypatch.setattr(registry, "refresh_policy", AsyncMock(return_value=policy))
    return policy


def approved(monkeypatch):
    monkeypatch.setattr(transport, "probe", AsyncMock(return_value=[SPEC]))
    server = registry.save_server(ADMIN, name="Synthetic MCP", url="http://127.0.0.1:9876/mcp")
    discovered = run(registry.discover_server(ADMIN, server["id"]))
    tool = discovered["tools"]["public_sum"]
    registry.approve_tool(
        ADMIN,
        server["id"],
        "public_sum",
        revision=discovered["revision"],
        schema_hash=tool["schema_hash"],
        read_only_confirmed=True,
    )
    return registry.approved_catalog()[0]


def invoke(item, context=None, arguments=None):
    return registry.invoke_tool(
        context or UserContext(user_id="employee", email="employee@local", tenant_id=""),
        item["tool_id"],
        {"left": 2, "right": 5} if arguments is None else arguments,
        revision=item["revision"],
        schema_hash=item["schema_hash"],
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@example.com/mcp",
        "http://example.com/mcp",
        "stdio://tool",
        "file:///tmp/tool",
        "https://example.com/mcp?token=hidden",
        "https://example.com/#fragment",
        "https://example.com:99999/mcp",
        "https://[::1/mcp",
    ],
)
def test_unsafe_connection_targets_rejected(url):
    with pytest.raises(registry.MCPError):
        registry.save_server(ADMIN, name="Unsafe", url=url)


def test_only_admin_fixed_http_and_credentials_encrypted(monkeypatch, isolated_ops):
    with pytest.raises(registry.MCPDenied):
        registry.save_server(EMPLOYEE, name="No", url="https://example.com/mcp")
    with pytest.raises(registry.MCPError, match="stdio"):
        registry.save_server(ADMIN, name="No", url="http://127.0.0.1:9876/mcp", transport="stdio")
    monkeypatch.setattr(settings, "NEXRAG_USER_JWT_SECRET", "synthetic" * 8)
    saved = registry.save_server(
        ADMIN, name="Synthetic", url="https://example.com/mcp", token="synthetic-token-only"
    )
    assert saved["has_token"] and "encrypted_token" not in saved
    raw = isolated_ops.query_one(
        "SELECT value FROM workspace_settings WHERE key=?", (registry.REGISTRY_KEY,)
    )["value"]
    assert "synthetic-token-only" not in raw
    assert registry._token(registry._get(saved["id"])) == "synthetic-token-only"
    with pytest.raises(registry.MCPError, match="清除"):
        registry.save_server(
            ADMIN, name="Changed", url="https://other.example.com/mcp", identifier=saved["id"]
        )
    events = registry.audit_events(ADMIN)
    assert events[0]["action"] == "mcp.server.save"
    assert "token" not in json.dumps(events)


def test_discovery_is_not_approval_and_approval_binds_hash_and_revision(monkeypatch):
    monkeypatch.setattr(transport, "probe", AsyncMock(return_value=[SPEC]))
    saved = registry.save_server(ADMIN, name="Synthetic", url="http://127.0.0.1:9876/mcp")
    found = run(registry.discover_server(ADMIN, saved["id"]))
    assert registry.approved_catalog() == []
    tool = found["tools"]["public_sum"]
    with pytest.raises(registry.MCPDenied, match="只读"):
        registry.approve_tool(
            ADMIN,
            saved["id"],
            "public_sum",
            revision=found["revision"],
            schema_hash=tool["schema_hash"],
            read_only_confirmed=False,
        )
    with pytest.raises(registry.MCPStaleApproval):
        registry.approve_tool(
            ADMIN,
            saved["id"],
            "public_sum",
            revision=found["revision"] - 1,
            schema_hash=tool["schema_hash"],
            read_only_confirmed=True,
        )
    registry.approve_tool(
        ADMIN,
        saved["id"],
        "public_sum",
        revision=found["revision"],
        schema_hash=tool["schema_hash"],
        read_only_confirmed=True,
    )
    assert registry.approved_catalog()[0]["tool_id"] == f"mcp:{saved['id']}:public_sum"


@pytest.mark.parametrize(
    "policy",
    [
        DataAccessPolicy("secret", external_tools=True),
        DataAccessPolicy("public", external_tools=False),
        DataAccessPolicy("public", external_tools=True, allowed_tool_ids=("mcp:another:tool",)),
    ],
)
def test_denied_scope_never_constructs_transport_or_sends_arguments(monkeypatch, policy):
    item = approved(monkeypatch)
    if not policy.allowed_tool_ids:
        policy = replace(policy, allowed_tool_ids=(item["tool_id"],))
    monkeypatch.setattr(registry, "refresh_policy", AsyncMock(return_value=policy))
    wire = AsyncMock()
    monkeypatch.setattr(transport, "call", wire)
    with pytest.raises(registry.MCPDenied):
        run(invoke(item))
    wire.assert_not_awaited()


def test_revoked_approval_and_changed_schema_reject_old_tool(monkeypatch):
    item = approved(monkeypatch)
    allow(monkeypatch, item["tool_id"])
    identifier = item["tool_id"].split(":")[1]
    registry.revoke_tool(ADMIN, identifier, item["name"])
    with pytest.raises(registry.MCPStaleApproval):
        run(invoke(item))
    monkeypatch.setattr(
        transport, "probe", AsyncMock(return_value=[{**SPEC, "description": "Changed public tool"}])
    )
    found = run(registry.discover_server(ADMIN, identifier))
    assert found["tools"]["public_sum"]["schema_hash"] != item["schema_hash"]
    assert registry.approved_catalog() == []


def test_invalid_arguments_cannot_reach_network(monkeypatch):
    item = approved(monkeypatch)
    allow(monkeypatch, item["tool_id"])
    wire = AsyncMock()
    monkeypatch.setattr(transport, "call", wire)
    with pytest.raises(registry.MCPError, match="schema"):
        run(invoke(item, arguments={"left": "not a number", "right": 5}))
    wire.assert_not_awaited()


def test_revocation_after_connection_before_call_is_rechecked(monkeypatch):
    item = approved(monkeypatch)
    allow(monkeypatch, item["tool_id"])
    sent = []

    async def connecting(*args, before_call, **kwargs):
        identifier = item["tool_id"].split(":")[1]
        registry.revoke_tool(ADMIN, identifier, item["name"])
        await before_call()
        sent.append(args)

    monkeypatch.setattr(transport, "call", connecting)
    with pytest.raises(registry.MCPStaleApproval):
        run(invoke(item))
    assert sent == []


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "$ref": "https://example.com/schema.json"},
        {
            "type": "object",
            "properties": {"token": {"type": "string", "x-mcp-header": "Authorization"}},
        },
        {"type": "object", "properties": {"x": {"type": "not-json-schema"}}},
    ],
)
def test_unapproved_schema_capabilities_rejected(schema):
    with pytest.raises(registry.MCPError):
        registry.normalize_spec({**SPEC, "inputSchema": schema})


def test_sdk_missing_reports_unavailable(monkeypatch):
    from importlib.metadata import PackageNotFoundError

    def missing(_):
        raise PackageNotFoundError("mcp")

    monkeypatch.setattr(transport, "version", missing)
    with pytest.raises(registry.MCPUnavailable, match="未安装"):
        run(transport.probe("http://127.0.0.1:9876/mcp", ""))


@pytest.mark.parametrize(
    "host,address",
    [
        ("public.example", "127.0.0.1"),
        ("public.example", "169.254.169.254"),
        ("public.example", "192.168.1.1"),
        ("localhost", "8.8.8.8"),
    ],
)
def test_dns_rebinding_into_nonapproved_network_is_rejected(monkeypatch, host, address):
    async def check():
        resolver = AsyncMock(return_value=[(2, 1, 6, "", (address, 443))])
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolver)
        with pytest.raises(registry.MCPError):
            await transport._target_ip(f"https://{host}/mcp")

    run(check())


def test_dns_target_is_pinned_with_original_host_and_tls_name(monkeypatch):
    import httpx2

    captured = []

    async def pin(_):
        return "8.8.8.8"

    monkeypatch.setattr(transport, "_target_ip", pin)

    def reply(request):
        captured.append(
            (str(request.url), request.headers["host"], request.extensions["sni_hostname"])
        )
        return httpx2.Response(200, json={"synthetic": True})

    async def check():
        wire = transport.guarded_transport(
            httpx2.MockTransport(reply), endpoint="https://public.example/mcp"
        )
        async with httpx2.AsyncClient(transport=wire) as client:
            response = await client.get("https://public.example/mcp")
            assert response.json() == {"synthetic": True}
            with pytest.raises(registry.MCPError, match="偏离"):
                await client.get("https://other.example/mcp")

    run(check())
    assert captured == [("https://8.8.8.8/mcp", "public.example", "public.example")]


@pytest.mark.parametrize(
    "status,headers,body",
    [
        (307, {"location": "http://127.0.0.1:9876/other"}, b""),
        (200, {}, b"x" * (transport.MAX_RESPONSE_BYTES + 1)),
        (200, {"content-encoding": "gzip"}, b"compressed"),
    ],
    ids=["redirect", "oversize", "compressed"],
)
def test_transport_blocks_redirect_oversize_and_compressed_responses(status, headers, body):
    import httpx2

    requests = []

    class RawBody(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield body

    def reply(request):
        requests.append(str(request.url))
        return httpx2.Response(status, headers=headers, stream=RawBody())

    async def check():
        guarded = transport.guarded_transport(httpx2.MockTransport(reply))
        async with httpx2.AsyncClient(transport=guarded) as client:
            with pytest.raises(registry.MCPError):
                await client.get("http://127.0.0.1:9876/mcp")

    run(check())
    assert len(requests) == 1


def test_real_official_sdk_streamable_http_protocol_without_external_network(monkeypatch):
    import httpx2
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("nexrag-synthetic-mcp")
    actual_calls = []

    @server.tool()
    def public_sum(left: int, right: int) -> dict:
        """把两个虚构的公开数字相加，不涉及文件、SQL 或网络。"""
        actual_calls.append((left, right))
        return {"total": left + right}

    app = server.streamable_http_app(json_response=True, stateless_http=True)
    guard = transport.guarded_transport
    connections = []

    def in_process_http(**kwargs):
        connections.append(True)
        return guard(httpx2.ASGITransport(app=app), **kwargs)

    monkeypatch.setattr(transport, "guarded_transport", in_process_http)

    async def scenario():
        async with app.router.lifespan_context(app):
            saved = registry.save_server(
                ADMIN, name="Synthetic SDK server", url="http://127.0.0.1:9876/mcp"
            )
            discovered = await registry.discover_server(ADMIN, saved["id"])
            spec = discovered["tools"]["public_sum"]
            registry.approve_tool(
                ADMIN,
                saved["id"],
                "public_sum",
                revision=discovered["revision"],
                schema_hash=spec["schema_hash"],
                read_only_confirmed=True,
            )
            item = registry.approved_catalog()[0]
            save_account_policy(
                "employee@local",
                {"external_tools": True, "allowed_tool_ids": [item["tool_id"]]},
                ADMIN.user_id,
            )
            result = await invoke(item)
            assert any(
                item.get("type") == "text" and json.loads(item["text"]) == {"total": 7}
                for item in result["content"]
            )
            server.remove_tool("public_sum")
            server.add_tool(
                public_sum, name="public_sum", description="Changed tool requiring fresh approval"
            )
            with pytest.raises(registry.MCPStaleApproval, match="变化"):
                await invoke(item)
            before = len(connections)
            save_account_policy(
                "employee@local",
                {"external_tools": False, "allowed_tool_ids": [item["tool_id"]]},
                ADMIN.user_id,
            )
            with pytest.raises(registry.MCPDenied):
                await invoke(item)
            assert len(connections) == before

    run(scenario())
    assert actual_calls == [(2, 5)]


def test_root_http_contract_enforces_admin_and_maps_registry_failures(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.api.mcp_routes import router
    from src.midplatform.tenant_access import get_principal

    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[get_principal] = lambda: EMPLOYEE
    monkeypatch.setattr(transport, "probe", AsyncMock(return_value=[SPEC]))
    with TestClient(app) as client:
        assert client.get("/v1/workspace/mcp").status_code == 403
        app.dependency_overrides[get_principal] = lambda: ADMIN
        invalid = client.post(
            "/v1/workspace/mcp", json={"name": "bad port", "url": "https://example.com:99999/mcp"}
        )
        assert invalid.status_code == 400
        saved = client.post(
            "/v1/workspace/mcp", json={"name": "Synthetic", "url": "http://127.0.0.1:9876/mcp"}
        )
        assert saved.status_code == 200
        identifier = saved.json()["id"]
        found = client.post(f"/v1/workspace/mcp/{identifier}/discover").json()
        body = {
            "revision": found["revision"],
            "schema_hash": found["tools"]["public_sum"]["schema_hash"],
            "read_only_confirmed": False,
        }
        endpoint = f"/v1/workspace/mcp/{identifier}/tools/public_sum/approve"
        assert client.post(endpoint, json=body).status_code == 403
        body["read_only_confirmed"] = True
        body["revision"] -= 1
        assert client.post(endpoint, json=body).status_code == 409
        body["revision"] += 1
        assert client.post(endpoint, json=body).status_code == 200
        assert registry.approved_catalog()
        assert (
            client.post(f"/v1/workspace/mcp/{identifier}/tools/public_sum/revoke").status_code
            == 200
        )
        assert registry.approved_catalog() == []
