"""用一次性的合成 Qdrant collection 检查正在运行的原生 RAG。

在 start.cmd 就绪后，用 .local-run/venvs/retrieval/Scripts/python.exe 运行。
检查过程绝不打开已有的知识库集合或业务数据库。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values
from qdrant_client import QdrantClient, models

ROOT = Path(__file__).resolve().parents[1]


async def verify() -> dict:
    """确认受管 Ollama 为回环且已禁用云功能后，在一次性合成 Qdrant collection 上检验混合检索、父文档展开和 collection 授权过滤，结束时删除该 collection。"""
    runtime = json.loads((ROOT / ".local-run/runtime.json").read_text("utf8"))
    trust = json.loads((ROOT / ".local-run/ollama-trust.json").read_text("utf8"))
    endpoint = trust["endpoint"].rstrip("/")
    if urlsplit(endpoint).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("Verification requires the managed loopback Ollama")
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        status = await client.get(endpoint + "/api/status")
        status.raise_for_status()
        if status.json().get("cloud", {}).get("disabled") is not True:
            raise RuntimeError("Live Ollama cloud-disabled status was not verified")

    environment = dotenv_values(ROOT / ".env")
    collection = "nexrag_acceptance_rag_" + uuid.uuid4().hex
    os.environ.update(
        API_KEY="synthetic-rag-acceptance-" + "x" * 32,
        EMBEDDING_API_BASE_URL=endpoint + "/v1",
        EMBEDDING_API_KEY="ollama",
        EMBEDDING_MODEL="bge-m3",
        EMBEDDING_PREFIX_QUERY="",
        QDRANT_URI=f"http://127.0.0.1:{runtime['ports']['qdrant']}",
        QDRANT_INDEX=collection,
        ENABLE_HYBRID_SEARCH="true",
        SPARSE_QUERY_PROVIDER="fastembed",
        SPARSE_QUERY_MODEL="Qdrant/bm42-all-minilm-l6-v2-attentions",
        FASTEMBED_CACHE_DIR=str(ROOT / ".local-run/cache/fastembed"),
        ENABLE_RERANKING="true",
        RERANKER_PROVIDER="fastembed",
        RERANKER_MODEL="BAAI/bge-reranker-base",
        RERANK_FUSION="replace",
        RERANK_MIN_SCORE="0.05",
        RERANK_TOP_K="3",
        RERANK_REQUIRED="true",
        RERANKER_THREADS="2",
        ENABLE_QUERY_GENERATION="false",
        ENABLE_AGENTIC_RAG="false",
    )
    if environment.get("QDRANT_API_KEY"):
        os.environ["QDRANT_API_KEY"] = environment["QDRANT_API_KEY"]
    sys.path.insert(0, str(ROOT / "base/retrieval-agent"))
    # settings 必须在分配好唯一测试 collection 之后再加载。
    from app.config import settings
    from app.models import SearchRequest
    from app.services import qdrant, sparse_embedding
    from app.services.pipeline import linear_search

    children = [
        "销售折扣上限为6%，审批负责人陈经理。",
        "企业销售审批由陈经理负责，客户折扣不能超过6%。",
        "员工A项目预算32000元，私有编号ALPHA_TEST_ONLY。",
        "Private employee B project budget is 67000 with code BETA_TEST_ONLY.",
        "London weather is sunny today, with light winds.",
    ]
    policy = "销售制度全文：" + children[0] + " 生效版本V2。"
    parents = [policy, policy, *children[2:]]
    database = QdrantClient(url=settings.qdrant_uri, api_key=settings.qdrant_api_key)
    created = False
    try:
        async with httpx.AsyncClient(timeout=90, trust_env=False) as client:
            embedded = await client.post(
                endpoint + "/v1/embeddings", json={"model": "bge-m3", "input": children}
            )
            embedded.raise_for_status()
            vectors = [item["embedding"] for item in embedded.json()["data"]]
        # 摄取使用文档注意力权重；BM42 的 query_embed 有意与之不同，
        # 不能用它来生成已存储的文档向量。
        sparse_model = sparse_embedding.get_model()
        assert sparse_model is not None
        raw_sparse = await asyncio.to_thread(lambda: list(sparse_model.embed(children)))
        sparse = [
            models.SparseVector(indices=item.indices.tolist(), values=item.values.tolist())
            for item in raw_sparse
        ]
        database.create_collection(
            collection,
            vectors_config={
                "text-dense": models.VectorParams(
                    size=len(vectors[0]), distance=models.Distance.COSINE
                )
            },
            sparse_vectors_config={
                "text-sparse": models.SparseVectorParams(modifier=models.Modifier.IDF)
            },
        )
        created = True
        points = [
            models.PointStruct(
                id=str(uuid.uuid4()),
                vector={"text-dense": dense, "text-sparse": lexical},
                payload={
                    "content": child,
                    "meta": {
                        "collection_name": "private-b" if index == 3 else "private-a",
                        "file_id": "policy" if index < 2 else str(index),
                        "parent_id": "policy-v2" if index < 2 else str(index),
                        "parent_text": parent,
                    },
                },
            )
            for index, (child, parent, dense, lexical) in enumerate(
                zip(children, parents, vectors, sparse, strict=True)
            )
        ]
        database.upsert(collection, points, wait=True)
        info = database.get_collection(collection)
        assert info.config.params.sparse_vectors["text-sparse"].modifier == models.Modifier.IDF
        stored, _ = database.scroll(
            collection, limit=len(children) + 1, with_vectors=True, with_payload=True
        )
        assert len(stored) == len(children)
        assert all(set(point.vector) == {"text-dense", "text-sparse"} for point in stored)
        assert all(
            point.payload["meta"]["parent_text"] and point.payload["meta"]["parent_id"]
            for point in stored
        )
        result = await linear_search(
            SearchRequest(queries=["销售折扣上限和审批负责人"], collection_names=["private-a"], k=5)
        )
        assert qdrant.has_sparse_vectors(), "Native dense+sparse layout was not recognized"
        assert result.documents and result.documents[0][0] == policy
        documents = result.documents[0]
        assert len(documents) <= 3 and len(set(documents)) == len(documents)
        assert all("parent_text" not in meta for meta in result.metadatas[0])
        # 查询 B 的确切内容但只授权 A，而不是指望 B 与策略查询本来就不相关。
        denied = await linear_search(
            SearchRequest(queries=[children[3]], collection_names=["private-a"], k=5)
        )
        assert all("BETA_TEST_ONLY" not in text for group in denied.documents for text in group)
        assert all(
            meta["collection_name"] == "private-a" for group in denied.metadatas for meta in group
        )
        # 拒绝结果不能是空洞的：同一个问题在显式授权 B 后，
        # 必须能检索到 B 的合成私有内容。
        allowed = await linear_search(
            SearchRequest(queries=[children[3]], collection_names=["private-b"], k=5)
        )
        assert any("BETA_TEST_ONLY" in text for group in allowed.documents for text in group)
        return {
            "status": "passed",
            "cloud_disabled": True,
            "native_hybrid": True,
            "physical_layout": "child dense+sparse with parent payload",
            "selected_parents": len(documents),
            "parent_dedup": True,
            "private_b_exact_query_filtered": True,
            "private_b_owner_positive_control": True,
            "bm42_idf": True,
            "bm42_asymmetric_query_encoder": True,
            "first_relevance": result.distances[0][0],
            "models": ["bge-m3", settings.sparse_query_model, settings.reranker_model],
        }
    finally:
        try:
            if created:
                database.delete_collection(collection)
        finally:
            database.close()
            qdrant.close_client()
            sparse_embedding.close()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(verify()), ensure_ascii=False))
