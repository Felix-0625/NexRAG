"""管理员维护的共享知识库：通过摄取服务上传、列出和删除。"""

from __future__ import annotations

import asyncio
import io
import time

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from src.config import settings
from src.main import app
from src.midplatform import knowledge_library

KEY = "test-service-key-with-32-characters"
SECRET = "a" * 32


def headers(role="admin"):
    claims = {
        "sub": "admin-1",
        "email": "admin@example.local",
        "role": role,
        "iss": "open-webui",
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
    }
    return {
        "Authorization": f"Bearer {KEY}",
        "X-OpenWebUI-User-Jwt": jwt.encode(claims, SECRET, algorithm="HS256"),
    }


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "NEXRAG_SHARED_API_KEY", KEY)
    monkeypatch.setattr(settings, "NEXRAG_USER_JWT_SECRET", SECRET)
    monkeypatch.setattr(settings, "INGESTION_API_KEY", "ingestion-key-with-32-characters")
    return TestClient(app)


_REAL_ASYNC_CLIENT = httpx.AsyncClient


def mock_transport(upload_response):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(200, json=upload_response)
        return httpx.Response(200, json={"chunks_deleted": 3})

    return httpx.MockTransport(handler)


def patch_ingestion(monkeypatch, upload_response):
    monkeypatch.setattr(
        knowledge_library.httpx,
        "AsyncClient",
        lambda **kw: _REAL_ASYNC_CLIENT(transport=mock_transport(upload_response)),
    )


def test_upload_records_inventory_and_delete_removes_it(isolated_ops, monkeypatch):
    monkeypatch.setattr(settings, "INGESTION_API_KEY", "k" * 32)
    patch_ingestion(
        monkeypatch, {"status": True, "collection_name": "nexrag_docs", "chunks_count": 5}
    )

    result = asyncio.run(
        knowledge_library.upload_document(
            filename="policy.md", content=b"hello", uploaded_by="admin@example.local"
        )
    )
    assert result["chunks_count"] == 5
    listed = knowledge_library.list_documents()
    assert [doc["filename"] for doc in listed] == ["policy.md"]

    asyncio.run(knowledge_library.delete_document(result["file_id"]))
    assert knowledge_library.list_documents() == []


def test_reupload_same_filename_overwrites_inventory(isolated_ops, monkeypatch):
    monkeypatch.setattr(settings, "INGESTION_API_KEY", "k" * 32)
    patch_ingestion(
        monkeypatch, {"status": True, "collection_name": "nexrag_docs", "chunks_count": 2}
    )
    first = asyncio.run(
        knowledge_library.upload_document(filename="a.md", content=b"v1", uploaded_by="x")
    )
    second = asyncio.run(
        knowledge_library.upload_document(filename="a.md", content=b"v2", uploaded_by="x")
    )
    assert first["file_id"] == second["file_id"]
    assert len(knowledge_library.list_documents()) == 1


def test_knowledge_endpoints_require_admin(client, monkeypatch):
    patch_ingestion(
        monkeypatch, {"status": True, "collection_name": "nexrag_docs", "chunks_count": 1}
    )
    denied = client.post(
        "/v1/ops/knowledge",
        files={"file": ("x.md", io.BytesIO(b"x"))},
        headers=headers(role="user"),
    )
    assert denied.status_code == 403

    uploaded = client.post(
        "/v1/ops/knowledge", files={"file": ("x.md", io.BytesIO(b"x"))}, headers=headers()
    )
    assert uploaded.status_code == 200
    listed = client.get("/v1/ops/knowledge", headers=headers())
    assert listed.json()["data"][0]["filename"] == "x.md"

    file_id = uploaded.json()["file_id"]
    assert (
        client.delete(f"/v1/ops/knowledge/{file_id}", headers=headers(role="user")).status_code
        == 403
    )
    assert client.delete(f"/v1/ops/knowledge/{file_id}", headers=headers()).status_code == 200
