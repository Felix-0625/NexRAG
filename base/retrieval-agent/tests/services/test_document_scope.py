from unittest.mock import AsyncMock

import pytest
from qdrant_client import models

from app.config import settings
from app.models import RetrievalResult
from app.services import pipeline
from app.services.document_scope import scoped_evidence_query

DOCUMENT = {"collection_name": "allowed", "file_id": "manual", "ingest_version": "v1"}
QUERY = "Under the maintenance manual; When should the pump be replaced?"


@pytest.mark.parametrize("other", [{"collection_name": "different"}, {"ingest_version": "v2"}])
def test_scope_preserves_ambiguity_across_collections_and_versions(other):
    assert scoped_evidence_query(QUERY, [DOCUMENT, DOCUMENT, {**DOCUMENT, **other}]) is None
    assert scoped_evidence_query("When should the pump be replaced?", [DOCUMENT] * 3) is None


@pytest.mark.parametrize("scoped_outcome", ["eligible", "no_hits", "below_threshold"])
async def test_pipeline_uses_scoped_evidence_only_when_eligible(monkeypatch, scoped_outcome):
    sparse = models.SparseVector(indices=[1], values=[1.0])
    monkeypatch.setattr(settings, "enable_document_scoped_retrieval", True)
    monkeypatch.setattr(settings, "enable_reranking", True)
    monkeypatch.setattr(settings, "enable_five_channel_retrieval", True)
    monkeypatch.setattr(settings, "hybrid_fusion", "rrf")
    monkeypatch.setattr(
        pipeline,
        "search_five_channels",
        AsyncMock(
            return_value=RetrievalResult(
                texts=["original", "second", "third"],
                metadatas=[DOCUMENT] * 3,
                distances=[1, 0.7, 0.6],
            )
        ),
    )
    embed = AsyncMock(return_value=([[0.5]], [sparse], True))
    monkeypatch.setattr(pipeline, "embed_dense_and_sparse", embed)
    texts = [] if scoped_outcome == "no_hits" else ["matching evidence"]
    scoped = AsyncMock(
        return_value=RetrievalResult(
            texts=texts,
            metadatas=[DOCUMENT] * len(texts),
            distances=[0.9] * len(texts),
        )
    )
    monkeypatch.setattr(pipeline.qdrant, "vector_search", scoped)

    async def rank(query, documents, metas, k):
        if query != QUERY and scoped_outcome == "below_threshold":
            return [], [], []
        return documents[:k], metas[:k], [0.9] * min(k, len(documents))

    rerank = AsyncMock(side_effect=rank)
    monkeypatch.setattr(pipeline, "rerank", rerank)
    results, metadata, _ = await pipeline.retrieve_one_query(
        QUERY,
        [0.1],
        sparse,
        ["allowed"],
        50,
        True,
        rerank_k=10,
    )
    focus = "When should the pump be replaced?"
    embed.assert_awaited_once_with([focus])
    scoped.assert_awaited_once_with(
        ["allowed"], [0.5], sparse, 50, document=("allowed", "manual", "v1")
    )
    if scoped_outcome == "eligible":
        assert [call.args[0] for call in rerank.await_args_list] == [focus]
        assert results == ["matching evidence"]
        assert metadata[0]["retrieval_channels"] == ["document_scoped_hybrid"]
    else:
        expected_queries = [focus, QUERY] if texts else [QUERY]
        assert [call.args[0] for call in rerank.await_args_list] == expected_queries
        assert results == ["original", "second", "third"]
        assert metadata == [DOCUMENT] * 3
