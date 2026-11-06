"""长期记忆使用的 embedding 客户端和向量索引。

生产环境使用 Ollama/OpenAI 兼容的 embedding 端点和 Qdrant。``InMemoryVectorIndex``
提供相同接口，用于测试和无 Qdrant 的运行；ops store 中的元数据始终是事实来源。
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
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class HttpEmbedder:
    """OpenAI 兼容的 /embeddings 客户端（默认 Ollama bge-m3）。"""

    def __init__(
        self, base_url: str | None = None, api_key: str | None = None, model: str | None = None
    ):
        self.base_url = (base_url or settings.EMBEDDING_API_BASE_URL).rstrip("/")
        self.api_key = api_key or settings.EMBEDDING_API_KEY
        self.model = model or settings.EMBEDDING_MODEL

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """为文本批量生成向量，按输入顺序返回；端点非本机回环或模型为云端模型时拒绝（ValueError）。"""
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
    """Qdrant 要求 UUID 或整数 ID，因此由自有 hex ID 派生稳定的 UUID。"""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"nexrag:{record_id}"))


def cosine(a: list[float], b: list[float]) -> float:
    """两个向量的余弦相似度；任一向量为零向量时返回 0.0。"""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class VectorIndex(Protocol):
    def upsert(self, record_id: str, vector: list[float], payload: dict[str, Any]) -> None: ...

    def delete(self, record_ids: list[str]) -> None: ...

    def search(
        self, vector: list[float], must: dict[str, Any], limit: int
    ) -> list[tuple[str, float]]: ...


def _matches(payload: dict[str, Any], must: dict[str, Any]) -> bool:
    """内存索引使用的相等/成员关系过滤。"""
    for key, expected in must.items():
        value = payload.get(key)
        if isinstance(expected, (list, tuple, set)):
            if value not in expected:
                return False
        elif value != expected:
            return False
    return True


class InMemoryVectorIndex:
    """进程内向量索引，使用精确余弦搜索。"""

    def __init__(self):
        self._points: dict[str, tuple[list[float], dict[str, Any]]] = {}
        self._lock = threading.Lock()
        # 作为不可达 Qdrant 的替身时为 True（见 memory.runtime）。
        self.fallback = False

    def upsert(self, record_id: str, vector: list[float], payload: dict[str, Any]) -> None:
        """写入或覆盖一条记录的向量与过滤字段。"""
        with self._lock:
            self._points[record_id] = (vector, dict(payload))

    def delete(self, record_ids: list[str]) -> None:
        """删除指定记录的向量；记录不存在时忽略。"""
        with self._lock:
            for record_id in record_ids:
                self._points.pop(record_id, None)

    def search(
        self, vector: list[float], must: dict[str, Any], limit: int
    ) -> list[tuple[str, float]]:
        """在满足 must 过滤条件的记录中按余弦相似度检索，返回相似度最高的 limit 个 (记录 ID, 相似度)。"""
        with self._lock:
            scored = [
                (record_id, cosine(vector, stored))
                for record_id, (stored, payload) in self._points.items()
                if _matches(payload, must)
            ]
        return sorted(scored, key=lambda item: item[1], reverse=True)[:limit]


class QdrantVectorIndex:
    """带 payload 过滤的 Qdrant collection，首次 upsert 时惰性创建。"""

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
        """写入或覆盖一条记录的向量；collection 不存在时按该向量维度先创建。"""
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
        """删除指定记录的向量；列表为空或 collection 尚不存在时不做任何事。"""
        from qdrant_client.models import PointIdsList

        if record_ids and self.client.collection_exists(self.collection):
            self.client.delete(
                self.collection,
                points_selector=PointIdsList(points=[point_id(r) for r in record_ids]),
            )

    def search(
        self, vector: list[float], must: dict[str, Any], limit: int
    ) -> list[tuple[str, float]]:
        """在满足 must 过滤条件的记录中检索最相似的 limit 个 (记录 ID, 得分)；collection 尚不存在时返回空列表。"""
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
