"""Embedding clients and vector indexes for memory.

Production uses compatible embedding endpoints and Qdrant. In-memory indexing supports
tests and outages; operations metadata remains authoritative.
"""

from __future__ import annotations

import logging
import math
import threading
import uuid
from typing import Any, Protocol

import httpx

from src.config import settings

log = logging.getLogger(__name__)


class Embedder(Protocol):
    """Batch text-embedding interface shared by local and HTTP implementations."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding per input text in input order."""
        ...


class HttpEmbedder:
    """OpenAI-compatible embeddings client, defaulting to Ollama bge-m3."""

    def __init__(
        self, base_url: str | None = None, api_key: str | None = None, model: str | None = None
    ):
        self.base_url = (base_url or settings.EMBEDDING_API_BASE_URL).rstrip("/")
        self.api_key = api_key or settings.EMBEDDING_API_KEY
        self.model = model or settings.EMBEDDING_MODEL

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed in input order; reject nonloopback endpoints and cloud models."""
        if not texts:
            return []
        from src.memory.local_policy import cloud_model_name
        from src.midplatform.data_policy import is_loopback_endpoint

        if not is_loopback_endpoint(self.base_url) or cloud_model_name(self.model):
            raise ValueError(
                "Personal-memory embeddings must use a local model without cloud routing"
            )
        async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
            response = await client.post(
                f"{self.base_url}/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
                json={"model": self.model, "input": texts},
            )
            response.raise_for_status()
            data = sorted(response.json()["data"], key=lambda item: item["index"])
            return [item["embedding"] for item in data]


def point_id(record_id: str) -> str:
    """Derive stable UUIDs from owned hex IDs for Qdrant compatibility."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"nexrag:{record_id}"))


def cosine(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity, returning zero for zero vectors."""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class VectorIndex(Protocol):
    """Vector storage and filtered nearest-neighbor search contract."""

    def upsert(self, record_id: str, vector: list[float], payload: dict[str, Any]) -> None:
        """Insert or replace a record vector and filterable metadata."""
        ...

    def delete(self, record_ids: list[str]) -> None:
        """Remove vectors for the specified record IDs."""
        ...

    def search(
        self, vector: list[float], must: dict[str, Any], limit: int
    ) -> list[tuple[str, float]]:
        """Return the highest-scoring vectors matching the visibility filters."""
        ...


def _matches(payload: dict[str, Any], must: dict[str, Any]) -> bool:
    """Match equality and membership filters in the in-memory index."""
    for key, expected in must.items():
        value = payload.get(key)
        if isinstance(expected, (list, tuple, set)):
            if value not in expected:
                return False
        elif value != expected:
            return False
    return True


class InMemoryVectorIndex:
    """Process-local index using exact cosine search."""

    def __init__(self):
        self._points: dict[str, tuple[list[float], dict[str, Any]]] = {}
        self._lock = threading.Lock()
        # Flag unavailable-Qdrant stand-ins so writes remain queued.
        self.fallback = False

    def upsert(self, record_id: str, vector: list[float], payload: dict[str, Any]) -> None:
        """Insert or replace a vector and filter metadata."""
        with self._lock:
            self._points[record_id] = (vector, dict(payload))

    def delete(self, record_ids: list[str]) -> None:
        """Delete a vector, ignoring missing records."""
        with self._lock:
            for record_id in record_ids:
                self._points.pop(record_id, None)

    def search(
        self, vector: list[float], must: dict[str, Any], limit: int
    ) -> list[tuple[str, float]]:
        """Return top matching record IDs and cosine scores within the limit."""
        with self._lock:
            scored = [
                (record_id, cosine(vector, stored))
                for record_id, (stored, payload) in self._points.items()
                if _matches(payload, must)
            ]
        return sorted(scored, key=lambda item: item[1], reverse=True)[:limit]


class QdrantVectorIndex:
    """Payload-filtered Qdrant collection created lazily at first upsert."""

    def __init__(self, collection: str, url: str | None = None):
        from qdrant_client import QdrantClient

        self.collection = collection
        self.client = QdrantClient(
            url=url or settings.QDRANT_URI, timeout=10, check_compatibility=False
        )
        self._ready = False

    def _ensure(self, dim: int) -> None:
        if self._ready:
            return
        from qdrant_client.models import Distance, PayloadSchemaType, VectorParams

        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                self.collection, vectors_config=VectorParams(size=dim, distance=Distance.COSINE)
            )
            for field_name in ("user_id", "scope", "tenant_id", "chat_id", "status", "kind"):
                self.client.create_payload_index(
                    self.collection, field_name=field_name, field_schema=PayloadSchemaType.KEYWORD
                )
        self._ready = True

    @staticmethod
    def _filter(must: dict[str, Any]):
        from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

        conditions = []
        for key, expected in must.items():
            if isinstance(expected, (list, tuple, set)):
                conditions.append(FieldCondition(key=key, match=MatchAny(any=list(expected))))
            else:
                conditions.append(FieldCondition(key=key, match=MatchValue(value=expected)))
        return Filter(must=conditions)

    def upsert(self, record_id: str, vector: list[float], payload: dict[str, Any]) -> None:
        """Upsert a vector, creating a matching-dimension collection if needed."""
        from qdrant_client.models import PointStruct

        self._ensure(len(vector))
        self.client.upsert(
            self.collection,
            points=[
                PointStruct(
                    id=point_id(record_id),
                    vector=vector,
                    payload={**payload, "record_id": record_id},
                )
            ],
        )

    def delete(self, record_ids: list[str]) -> None:
        """Delete vectors; ignore empty IDs and collections not yet created."""
        from qdrant_client.models import PointIdsList

        if record_ids and self.client.collection_exists(self.collection):
            self.client.delete(
                self.collection,
                points_selector=PointIdsList(points=[point_id(r) for r in record_ids]),
            )

    def search(
        self, vector: list[float], must: dict[str, Any], limit: int
    ) -> list[tuple[str, float]]:
        """Return top matching IDs and scores, or no results before collection creation."""
        if not self.client.collection_exists(self.collection):
            return []
        response = self.client.query_points(
            self.collection,
            query=vector,
            query_filter=self._filter(must),
            limit=limit,
            with_payload=["record_id"],
        )
        return [(point.payload["record_id"], float(point.score)) for point in response.points]
