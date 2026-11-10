"""Verify Open WebUI identities and seller grants at the HTTP boundary."""

import time
from pathlib import Path
from unittest.mock import AsyncMock

import jwt
import pytest
from fastapi.testclient import TestClient

from src.agent.agent import runner
from src.config import settings
from src.main import app

SELLER_A = "7299e27ed73d2ad986de7f7c77d919fa"
SELLER_B = "fa40cc5b934574b62717c68f3d678b6d"
SHARED_ORDER = "002f98c0f7efd42638ed6100ca699b42"


@pytest.fixture
def authorized_client(tmp_path, monkeypatch):
    """Provide authorized client test data or behavior for this scenario."""
    policy = tmp_path / "tenant-access.yaml"
    policy.write_text(
        f"tenants:\n  {SELLER_A}: Seller A\n  {SELLER_B}: Seller B\n"
        f"users:\n  viewer@example.local:\n    - {SELLER_A}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "TENANT_ACCESS_PATH", str(policy))
    monkeypatch.setattr(settings, "NEXRAG_SHARED_API_KEY", "test-service-key-with-32-characters")
    monkeypatch.setattr(settings, "NEXRAG_USER_JWT_SECRET", "a" * 32)
    return TestClient(app)


def headers(role="user", *, secret="a" * 32):
    """Build signed request headers for the test identity."""
    claims = {
        "sub": "webui-user-1",
        "email": "viewer@example.local",
        "role": role,
        "iss": "open-webui",
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
    }
    return {
        "Authorization": "Bearer test-service-key-with-32-characters",
        "X-OpenWebUI-User-Jwt": jwt.encode(claims, secret, algorithm="HS256"),
    }


def test_tenant_list_uses_signed_role_and_email(authorized_client):
    viewer = authorized_client.get("/v1/tenants", headers=headers())
    assert viewer.status_code == 200
    assert [item["tenant_id"] for item in viewer.json()["data"]] == [SELLER_A]

    admin = authorized_client.get("/v1/tenants", headers=headers(role="admin"))
    assert {item["tenant_id"] for item in admin.json()["data"]} == {SELLER_A, SELLER_B}


def test_plain_headers_and_invalid_signature_cannot_grant_access(authorized_client):
    assert authorized_client.get("/v1/tenants").status_code == 401
    forged = headers(secret="wrong" * 8)
    forged["X-OpenWebUI-User-Role"] = "admin"
    assert authorized_client.get("/v1/tenants", headers=forged).status_code == 401


def test_ungranted_seller_is_rejected_before_sql(authorized_client):
    response = authorized_client.post(
        "/v1/business/query",
        headers=headers(),
        json={"tenant_id": SELLER_B, "sql": "SELECT COUNT(*) FROM seller_items"},
    )
    assert response.status_code == 403


def test_authorized_real_olist_query(authorized_client, monkeypatch):
    database = Path(__file__).resolve().parents[1] / "data" / "sqlite" / "business.db"
    if not database.is_file():
        pytest.skip("Download and import Olist data to run this integration check")
    monkeypatch.setattr(settings, "BUSINESS_DB_PATH", str(database))
    response = authorized_client.post(
        "/v1/business/query",
        headers=headers(),
        json={
            "tenant_id": SELLER_A,
            "sql": f"SELECT seller_id, price_cents FROM seller_items WHERE order_id = '{SHARED_ORDER}'",
        },
    )
    assert response.status_code == 200
    assert response.json()["rows"] == [{"seller_id": SELLER_A, "price_cents": 899}]


def test_chat_checks_signed_user_and_seller_before_agent(authorized_client, monkeypatch):
    run = AsyncMock(return_value="361 rows")
    monkeypatch.setattr(runner, "run", run)
    request = {"model": "NexRAG", "messages": [{"role": "user", "content": "Count my items"}]}

    unsigned = authorized_client.post(
        "/v1/chat/completions", json=request, headers={"X-NexRAG-Tenant-Id": SELLER_A}
    )
    forbidden = authorized_client.post(
        "/v1/chat/completions",
        json=request,
        headers={**headers(), "X-NexRAG-Tenant-Id": SELLER_B},
    )
    assert unsigned.status_code == 401
    assert forbidden.status_code == 403
    run.assert_not_awaited()

    allowed = authorized_client.post(
        "/v1/chat/completions",
        json=request,
        headers={**headers(), "X-NexRAG-Tenant-Id": SELLER_A},
    )
    assert allowed.status_code == 200
    assert allowed.json()["choices"][0]["message"]["content"] == "361 rows"
    assert run.await_args.args[1].tenant_id == SELLER_A
    assert run.await_args.args[1].capture_memory is True
    scheduled = authorized_client.post(
        "/v1/chat/completions",
        json=request,
        headers={**headers(), "X-NexRAG-Tenant-Id": SELLER_A, "X-NexRAG-Capture-Memory": "false"},
    )
    assert scheduled.status_code == 200
    assert run.await_args.args[1].capture_memory is False


def test_chat_stream_uses_openai_sse_frames(authorized_client, monkeypatch):
    async def mock_stream(prompt, context, history=None):
        """Provide mock stream test data or behavior for this scenario."""
        context.usage = {"prompt_tokens": 21, "completion_tokens": 13, "total_tokens": 34}
        yield "Verified answer"

    monkeypatch.setattr(runner, "run_stream", mock_stream)
    response = authorized_client.post(
        "/v1/chat/completions",
        json={
            "model": "NexRAG",
            "stream": True,
            "messages": [{"role": "user", "content": "Count my items"}],
        },
        headers={**headers(), "X-NexRAG-Tenant-Id": SELLER_A},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"content": "Verified answer"' in response.text
    assert (
        '"usage": {"prompt_tokens": 21, "completion_tokens": 13, "total_tokens": 34}'
        in response.text
    )
    assert response.text.endswith("data: [DONE]\n\n")
