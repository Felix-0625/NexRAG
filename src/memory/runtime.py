"""进程级的长期记忆装配。

Qdrant 可达时使用 Qdrant，否则退回内存索引以保证 Agent 可用（此时记忆仅在
进程存活期间有效，并记录警告）。LLM completer 复用已配置的 OpenAI 兼容端点。
"""

from __future__ import annotations

import json
import logging
import threading

import httpx

from src.config import settings
from src.memory.local_policy import cloud_model_name, local_memory_configuration
from src.memory.service import MemoryService
from src.memory.vectors import HttpEmbedder, InMemoryVectorIndex, QdrantVectorIndex, VectorIndex
from src.midplatform.ops_store import get_ops_store

log = logging.getLogger(__name__)

_lock = threading.Lock()
_service: MemoryService | None = None


async def llm_complete(
    system: str, user: str, *, model: str | None = None, schema: dict | None = None
) -> str:
    """向 OpenAI 兼容的 LLM 端点发起一次面向 JSON 输出的补全。"""
    if not local_memory_configuration() or cloud_model_name(model or settings.LLM_MODEL_NAME):
        raise ValueError(
            "Personal memory and improvement helpers require local models without cloud routing"
        )
    async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
        response = await client.post(
            f"{settings.LLM_API_BASE.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.LLM_API_KEY}"},
            json={
                "model": model or settings.LLM_MODEL_NAME,
                "temperature": 0,
                "response_format": (
                    {
                        "type": "json_schema",
                        "json_schema": {"name": "memory_extraction", "schema": schema},
                    }
                    if schema is not None
                    else {"type": "json_object"}
                ),
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"] or ""


async def memory_complete(system: str, user: str) -> str:
    """将提取约束在经校验的 Pydantic 数值/字段 schema 内。

    仅靠提示词要求 JSON 时，真实本地模型会输出 confidence="high"、
    importance="medium"，导致所有记忆被静默丢弃。约束解码直接强制该契约，
    而不是去猜测数值替代。
    """
    from src.memory.extractor import Extraction

    schema = Extraction.model_json_schema()
    return await llm_complete(system + "\nJSON Schema:\n" + json.dumps(schema), user, schema=schema)


def build_index(collection: str) -> VectorIndex:
    """优先返回连通的 Qdrant 索引；不可达时退回进程内索引（标记 fallback，写入进入 outbox 待恢复后同步）。"""
    try:
        index = QdrantVectorIndex(collection)
        index.client.get_collections()
        return index
    except Exception:  # noqa: BLE001
        log.warning("Qdrant unavailable for %s; using in-memory index", collection)
        index = InMemoryVectorIndex()
        # 写入仍会进入 outbox，待 Qdrant 恢复后同步过去。
        index.fallback = True
        return index


def get_memory_service() -> MemoryService | None:
    """返回共享的记忆服务；记忆被禁用时返回 None。"""
    global _service
    if not settings.ENABLE_MEMORY:
        return None
    if not local_memory_configuration():
        log.warning(
            "Personal memory disabled: helper model, embeddings and vector store must all be local"
        )
        return None
    with _lock:
        if _service is None:
            _service = MemoryService(
                get_ops_store(),
                build_index(settings.MEMORY_COLLECTION),
                HttpEmbedder(),
                completer=memory_complete,
            )
        return _service


def set_memory_service(service: MemoryService | None) -> None:
    """替换进程级共享的记忆服务；传入 None 会清空，下次获取时重新创建。"""
    global _service
    with _lock:
        _service = service
