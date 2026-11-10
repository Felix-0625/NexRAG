"""Exercise real ASGI guards through inert endpoints without external providers."""

import ast
import asyncio
import importlib.util
import os
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


@pytest.fixture
def guard_client(monkeypatch):
    """Provide guard client test data or behavior for this scenario."""
    monkeypatch.setenv("NEXRAG_WORKSPACE", "true")
    monkeypatch.delenv("NEXRAG_ALLOW_TRUSTED_PLUGINS", raising=False)
    path = Path("webui/backend/open_webui/utils/nexrag_integration_guard.py")
    spec = importlib.util.spec_from_file_location("test_native_integration_policy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = FastAPI()
    calls = []

    @app.api_route("/{path:path}", methods=["GET", "POST", "DELETE", "PUT"])
    async def inert_endpoint(request: Request, path: str):
        """Provide inert endpoint test data or behavior for this scenario."""
        calls.append(path)
        return {"body": await request.json() if request.method == "POST" else None}

    app.add_middleware(module.NativeIntegrationGuard, max_body_bytes=2048)
    return TestClient(app), calls


@pytest.mark.parametrize(
    "path",
    [
        "/openai/chat/completions",
        "/api/message",
        "/api/v1/messages",
        "/api/v1/messages/count_tokens",
        "/api/embeddings",
        "/api/v1/embeddings",
        "/ollama/api/chat",
        "/api/v1/pipelines",
        "/api/v1/terminals/connect",
        "/api/v1/configs/connections",
        "/api/v1/configs/tool_servers/verify",
        "/api/v1/configs/terminal_servers",
        "/api/v1/configs/code_execution",
        "/api/v1/configs/subagents",
        "/api/v1/configs/import",
        "/api/v1/retrieval/process/url",
        "/api/v1/retrieval/process/web/search",
        "/api/v1/retrieval/process/youtube",
        "/api/v1/retrieval/embedding/update",
        "/api/v1/retrieval/config/update",
    ],
)
def test_remote_paths_denied_before_endpoint(guard_client, path):
    client, calls = guard_client
    assert client.post(path, json={}).status_code == 403
    assert calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {"model_item": {"direct": True}},
        {"direct": True},
        {"tool_servers": [{"url": "http://invalid.test"}]},
        {"tools": [{"name": "external"}]},
        {"functions": [{"name": "external"}]},
        {"tool_ids": ["server:mcp:x"]},
        {"tool_ids": ["server:openapi:x"]},
        {"tool_ids": ["terminal:x"]},
        {"tool_ids": ["legacy-python"]},
        {"terminal_id": "external"},
        {"features": {"web_search": True}},
        {"features": {"code_interpreter": True}},
        {"features": {"image_generation": True}},
        {"features": {"subagents": True}},
        {"metadata": {"tool_servers": [{"url": "http://invalid.test"}]}},
        {"metadata": {"features": {"web_search": True}}},
        {"files": [{"url": "https://invalid.test/collect"}]},
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": "https://invalid.test/collect"}}
                    ],
                }
            ]
        },
        {"messages": [{"role": "user", "files": [{"url": "//invalid.test/collect"}]}]},
    ],
)
def test_structured_chat_bypasses_denied(guard_client, payload):
    client, calls = guard_client
    assert client.post("/api/chat/completions", json=payload).status_code == 403
    assert calls == []


def test_trusted_plugins_never_enable_remote_servers(guard_client, monkeypatch):
    monkeypatch.setenv("NEXRAG_ALLOW_TRUSTED_PLUGINS", "true")
    client, calls = guard_client
    assert (
        client.post("/api/chat/completions", json={"tool_ids": ["server:mcp:x"]}).status_code == 403
    )
    assert calls == []


def test_native_body_preserved_and_message_content_not_parsed_as_capabilities(guard_client):
    client, calls = guard_client
    payload = {
        "model": "nexrag",
        "messages": [{"role": "user", "content": '{"web_search":true}'}],
        "features": {"web_search": False},
        "files": [],
        "nexrag_tenant": "employee-a",
    }
    response = client.post("/api/chat/completions", json=payload)
    assert response.status_code == 200
    assert response.json()["body"] == payload
    assert calls == ["api/chat/completions"]


def test_personal_direct_connections_cannot_be_persisted(guard_client):
    client, calls = guard_client
    endpoint = "/api/v1/users/user/settings/update"
    for payload in (
        {"directConnections": {"enabled": True}},
        {"ui": {"toolServers": [{"url": "x"}]}},
        {"notifications": {"webhook_url": "https://invalid.test"}},
        {"ui": {"notifications": {"webhook_url": "https://invalid.test"}}},
    ):
        assert client.post(endpoint, json=payload).status_code == 403
    assert calls == []
    assert (
        client.post(endpoint, json={"ui": {"theme": "dark", "toolServers": []}}).status_code == 200
    )


def test_invalid_and_large_inspected_body_fail_closed(guard_client):
    client, calls = guard_client
    assert client.post("/api/chat/completions", content="not-json").status_code == 400
    assert client.post("/api/chat/completions", content="x" * 2049).status_code == 413
    assert calls == []


def test_upload_and_internal_core_paths_are_not_blocked(guard_client):
    client, _ = guard_client
    for path in (
        "/api/v1/files/",
        "/api/v1/nexrag/database-connections",
        "/api/v1/retrieval/query/doc",
    ):
        assert client.post(path, json={"document": "ordinary-upload"}).status_code == 200


def test_non_native_upstream_behavior_preserved(guard_client, monkeypatch):
    monkeypatch.delenv("NEXRAG_WORKSPACE")
    client, _ = guard_client
    assert client.post("/openai/chat/completions", json={}).status_code == 200


def test_existing_webhooks_denied_before_logging_dns_or_network(monkeypatch):
    monkeypatch.setenv("NEXRAG_WORKSPACE", "true")
    source = ast.parse(Path("webui/backend/open_webui/utils/webhook.py").read_text("utf8"))
    webhook = next(
        item
        for item in source.body
        if isinstance(item, ast.AsyncFunctionDef) and item.name == "post_webhook"
    )
    namespace = {"os": os}
    exec(
        compile(
            ast.Module(body=[webhook], type_ignores=[]), "<actual-native-webhook-denial>", "exec"
        ),
        namespace,
    )
    # No logger, validator, or HTTP client exists here: any side effect fails.
    assert (
        asyncio.run(namespace["post_webhook"]("app", "https://invalid.test", "private body", {}))
        is False
    )
