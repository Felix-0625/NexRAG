"""Assemble process-wide memory dependencies.

Prefer Qdrant; warn and fall back to process-local vectors during outages, retaining
queued writes. Reuse the configured OpenAI-compatible completion endpoint.
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
    """Request one JSON-oriented completion from the configured endpoint."""
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
    """Constrain extraction to Pydantic fields and numeric types.

    Prompt-only JSON can produce confidence='high' and silently discard memories. Constrained
    decoding enforces the contract without guessing replacement values.
    """
    from src.memory.extractor import Extraction

    schema = Extraction.model_json_schema()
    return await llm_complete(system + "\nJSON Schema:\n" + json.dumps(schema), user, schema=schema)


def build_index(collection: str) -> VectorIndex:
    """Prefer Qdrant; otherwise return a flagged in-memory fallback with queued writes."""
    try:
        index = QdrantVectorIndex(collection)
        index.client.get_collections()
        return index
    except Exception:  # noqa: BLE001
        log.warning("Qdrant unavailable for %s; using in-memory index", collection)
        index = InMemoryVectorIndex()
        # Keep writes queued for replay when Qdrant recovers.
        index.fallback = True
        return index


def get_memory_service() -> MemoryService | None:
    """Return the shared memory service, or None when disabled."""
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
    """Replace the shared service; None resets it for lazy recreation."""
    global _service
    with _lock:
        _service = service
