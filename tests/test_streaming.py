"""验证兼容 OpenAI 的 token 流式输出（Server-Sent Events）。"""

from __future__ import annotations

import json
import time

import jwt
import pytest
from fastapi.testclient import TestClient

from src.agent.agent import runner
from src.config import settings
from src.main import app

SELLER_A = "seller_streaming_001"


@pytest.fixture
def streaming_client(tmp_path, monkeypatch):
    policy = tmp_path / "tenant-access.yaml"
    policy.write_text(
        f"tenants:\n  {SELLER_A}: Streaming Seller\n"
        f"users:\n  streamer@example.local:\n    - {SELLER_A}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "TENANT_ACCESS_PATH", str(policy))
    monkeypatch.setattr(settings, "NEXRAG_SHARED_API_KEY", "test-service-key-with-32-characters")
    monkeypatch.setattr(settings, "NEXRAG_USER_JWT_SECRET", "s" * 32)
    return TestClient(app)


def auth_headers(*, secret="s" * 32):
    claims = {
        "sub": "stream-user-1",
        "email": "streamer@example.local",
        "role": "user",
        "iss": "open-webui",
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
    }
    return {
        "Authorization": "Bearer test-service-key-with-32-characters",
        "X-OpenWebUI-User-Jwt": jwt.encode(claims, secret, algorithm="HS256"),
        "X-NexRAG-Tenant-Id": SELLER_A,
    }


def test_chat_completions_streaming_success(streaming_client, monkeypatch):
    """stream=True 时，端点以 SSE 分块输出，并以 [DONE] 结束。"""

    async def mock_run_stream(prompt, context, history=None):
        yield "Hello "
        yield "world, "
        yield "this is streamed!"

    monkeypatch.setattr(runner, "run_stream", mock_run_stream)

    payload = {
        "model": "NexRAG",
        "messages": [{"role": "user", "content": "Tell me something"}],
        "stream": True,
    }

    response = streaming_client.post("/v1/chat/completions", headers=auth_headers(), json=payload)
    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")

    lines = [line.strip() for line in response.text.split("\n") if line.strip()]
    assert lines[-1] == "data: [DONE]"

    # 解析并拼接 delta 内容
    collected_tokens = []
    for line in lines[:-1]:
        assert line.startswith("data: ")
        data = json.loads(line[6:])
        choices = data.get("choices", [])
        if choices:
            delta = choices[0].get("delta", {})
            if "content" in delta and delta["content"]:
                collected_tokens.append(delta["content"])

    assert "".join(collected_tokens) == "Hello world, this is streamed!"


def test_chat_completions_streaming_forbidden_tenant(streaming_client):
    """流式模式下访问未授权的租户会返回 403。"""
    headers = auth_headers()
    headers["X-NexRAG-Tenant-Id"] = "unauthorized_seller"

    payload = {
        "model": "NexRAG",
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": True,
    }

    response = streaming_client.post("/v1/chat/completions", headers=headers, json=payload)
    assert response.status_code == 403
