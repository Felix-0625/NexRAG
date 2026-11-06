"""管理员维护的共享知识库：通过 ingestion 服务上传/列出/删除文件。

NexRAG 把文件转发给现有的 AarhusAI ``ingestion-service``，而不是重新实现
分块/向量化。这里只会操作 ``settings.KNOWLEDGE_COLLECTION_NAME`` 这一个
collection（Agent 的共享文档范围）；它有别于用户私有的 Open WebUI 知识库，
后者由 Open WebUI 自行管理。
"""

from __future__ import annotations

import hashlib
from typing import Any

import httpx

from src.config import settings
from src.midplatform.ops_store import OpsStore, get_ops_store, now


def _file_id(filename: str, collection: str) -> str:
    """稳定的 ID，使重复上传同名文件时覆盖而不是产生重复。"""
    return hashlib.sha256(f"{collection}\0{filename}".encode()).hexdigest()[:32]


async def upload_document(
    *,
    filename: str,
    content: bytes,
    uploaded_by: str,
    store: OpsStore | None = None,
) -> dict[str, Any]:
    """把一个文件发送给 ingestion 服务，并登记到清单中。"""
    if not settings.INGESTION_API_KEY:
        raise RuntimeError("INGESTION_API_KEY is not configured")
    collection = settings.KNOWLEDGE_COLLECTION_NAME
    file_id = _file_id(filename, collection)
    fields = {
        "file_id": file_id,
        "filename": filename,
        "collection_name": collection,
        "collection_type": "knowledge",
        "user_id": uploaded_by,
        "overwrite": "true",
    }
    async with httpx.AsyncClient(timeout=settings.INGESTION_TIMEOUT_SECONDS) as client:
        response = await client.put(
            f"{settings.INGESTION_API_BASE_URL.rstrip('/')}/api/v1/ingest",
            headers={"Authorization": f"Bearer {settings.INGESTION_API_KEY}"},
            data=fields,
            files={"file": (filename, content)},
        )
        response.raise_for_status()
        payload = response.json()

    store = store or get_ops_store()
    store.execute(
        "INSERT OR REPLACE INTO knowledge_documents VALUES (?,?,?,?,?,?)",
        (
            file_id,
            filename,
            payload["collection_name"],
            payload["chunks_count"],
            uploaded_by,
            now(),
        ),
    )
    return {"file_id": file_id, **payload}


async def delete_document(file_id: str, store: OpsStore | None = None) -> dict[str, Any]:
    """从 ingestion 服务和清单中移除某个文件的分块。"""
    if not settings.INGESTION_API_KEY:
        raise RuntimeError("INGESTION_API_KEY is not configured")
    async with httpx.AsyncClient(timeout=settings.INGESTION_TIMEOUT_SECONDS) as client:
        response = await client.delete(
            f"{settings.INGESTION_API_BASE_URL.rstrip('/')}/api/v1/documents/{file_id}",
            headers={"Authorization": f"Bearer {settings.INGESTION_API_KEY}"},
        )
        response.raise_for_status()
        payload = response.json()
    store = store or get_ops_store()
    store.execute("DELETE FROM knowledge_documents WHERE file_id = ?", (file_id,))
    return payload


def list_documents(store: OpsStore | None = None) -> list[dict[str, Any]]:
    """共享知识库当前包含的文件清单。"""
    store = store or get_ops_store()
    return store.query("SELECT * FROM knowledge_documents ORDER BY created_at DESC")
