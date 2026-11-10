from unittest.mock import AsyncMock, patch

import httpx

from app.config import settings
from app.services import reranker
from app.services.reranker import rerank

_FAKE_REQUEST = httpx.Request("POST", "http://fake/v1/rerank")


def test_local_rerank_retries_all_pairs_in_smaller_batches_after_allocator_failure(monkeypatch):
    from onnxruntime.capi.onnxruntime_pybind11_state import Fail

    class MemoryLimitedCrossEncoder:
        def rerank(self, _query, documents):
            if len(documents) > 2:
                yield float(documents[0])
                raise Fail("Available memory is smaller than requested bytes")
            yield from map(float, documents)

    monkeypatch.setattr(reranker, "_local_model", MemoryLimitedCrossEncoder())
    monkeypatch.setattr(settings, "reranker_model", "cross-encoder/test")
    assert reranker._local_rerank_scores("question", ["1", "2", "3", "4", "5"]) == [1, 2, 3, 4, 5]


async def test_rerank_basic_replace_mode():
    """RERANK_FUSION=replace ⇒ pure cross-encoder ordering (the older behaviour)."""
    mock_response = httpx.Response(
        200,
        json={
            "results": [
                {"index": 0, "relevance_score": 0.3},
                {"index": 1, "relevance_score": 0.9},
            ]
        },
        request=_FAKE_REQUEST,
    )
    with (
        patch.object(settings, "rerank_fusion", "replace"),
        patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_response
        ),
    ):
        texts, metas, scores = await rerank(
            "query",
            ["doc_low", "doc_high"],
            [{"id": 1}, {"id": 2}],
            k=2,
        )

    # Should be sorted by relevance descending
    assert texts == ["doc_high", "doc_low"]
    assert metas == [{"id": 2}, {"id": 1}]
    assert scores == [0.9, 0.3]


async def test_local_cross_encoder_scores_candidates_before_parent_restore():
    """The local provider follows the same ordering and score contract as HTTP."""
    with (
        patch.object(settings, "reranker_provider", "fastembed"),
        patch.object(settings, "rerank_fusion", "replace"),
        patch("app.services.reranker._local_rerank_scores", return_value=[0.2, 0.8]) as local,
        patch("app.services.reranker.get_client") as http_client,
    ):
        texts, metas, scores = await rerank(
            "卖家收入",
            ["unrelated child", "seller revenue child"],
            [{"parent_id": "a"}, {"parent_id": "b"}],
            k=2,
        )

    local.assert_called_once_with("卖家收入", ["unrelated child", "seller revenue child"])
    http_client.assert_not_called()
    assert texts == ["seller revenue child", "unrelated child"]
    assert metas == [{"parent_id": "b"}, {"parent_id": "a"}]
    assert scores == [0.8, 0.2]


async def test_title_is_only_in_model_input_and_older_metadata_still_works():
    documents = ["first chunk", "legacy chunk"]
    metadatas = [{"search_title": "Privacy Guide"}, {"filename": "legacy.txt"}]
    with (
        patch.object(settings, "reranker_provider", "fastembed"),
        patch.object(settings, "enable_filename_retrieval", True),
        patch.object(settings, "rerank_fusion", "replace"),
        patch("app.services.reranker._local_rerank_scores", return_value=[0.9, 0.2]) as local,
    ):
        texts, metas, scores = await rerank("query", documents, metadatas, k=2)

    local.assert_called_once_with("query", ["Privacy Guide\nfirst chunk", "legacy chunk"])
    assert texts == documents
    assert metas == metadatas
    assert scores == [0.9, 0.2]


def _diagnostic_response():
    """Reranker response mirroring the real 'Opkrævningens bankoplysninger' ranks.

    Documents are passed in retrieval (dense) order: index 0 is the answer chunk
    (dense #0) which the cross-encoder buries (rerank #3 @ 0.0776); two workflow
    chunks get the top cross-encoder scores.
    """
    return httpx.Response(
        200,
        json={
            "results": [
                {"index": 0, "relevance_score": 0.0776},  # bankopl  — dense#0
                {"index": 1, "relevance_score": 0.2434},  # C        — dense#1
                {"index": 2, "relevance_score": 0.8825},  # A        — dense#2
                {"index": 3, "relevance_score": 0.8018},  # B        — dense#3
            ]
        },
        request=_FAKE_REQUEST,
    )


_DIAG_DOCS = ["bankopl", "C", "A", "B"]
_DIAG_METAS = [{"id": 0}, {"id": 1}, {"id": 2}, {"id": 3}]


async def test_rerank_rrf_surfaces_buried_dense_hit():
    """Fix 4: RRF lifts the dense-#0 / rerank-#3 answer chunk into the top-3."""
    with (
        patch.object(settings, "rerank_fusion", "rrf"),
        patch.object(settings, "rerank_rrf_k", 60),
        patch.object(
            httpx.AsyncClient,
            "post",
            new_callable=AsyncMock,
            return_value=_diagnostic_response(),
        ),
    ):
        texts, _metas, _scores = await rerank("query", _DIAG_DOCS, _DIAG_METAS, k=3)

    # Fused order (K=60): A (#0), bankopl (#2), C — bankopl is inside top-3.
    assert "bankopl" in texts
    assert texts == ["A", "bankopl", "C"]


async def test_rerank_replace_mode_buries_dense_hit():
    """Without fusion the cross-encoder drops the answer chunk out of top-3."""
    with (
        patch.object(settings, "rerank_fusion", "replace"),
        patch.object(
            httpx.AsyncClient,
            "post",
            new_callable=AsyncMock,
            return_value=_diagnostic_response(),
        ),
    ):
        texts, _metas, _scores = await rerank("query", _DIAG_DOCS, _DIAG_METAS, k=3)

    assert "bankopl" not in texts
    assert texts == ["A", "B", "C"]


async def test_rerank_limits_to_k():
    mock_response = httpx.Response(
        200,
        json={
            "results": [
                {"index": 0, "relevance_score": 0.9},
                {"index": 1, "relevance_score": 0.8},
                {"index": 2, "relevance_score": 0.7},
            ]
        },
        request=_FAKE_REQUEST,
    )
    with patch.object(
        httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_response
    ):
        texts, _metas, _scores = await rerank(
            "query",
            ["a", "b", "c"],
            [{}, {}, {}],
            k=2,
        )

    assert len(texts) == 2
    assert texts == ["a", "b"]


async def test_rerank_empty_documents():
    texts, metas, scores = await rerank("query", [], [], k=5)
    assert texts == []
    assert metas == []
    assert scores == []


async def test_required_rerank_failure_does_not_claim_fallback_is_validated():
    with (
        patch.object(settings, "rerank_required", True),
        patch.object(
            httpx.AsyncClient,
            "post",
            new_callable=AsyncMock,
            side_effect=httpx.ConnectError("unavailable"),
        ),
    ):
        import pytest

        with pytest.raises(RuntimeError, match="Required document reranking is unavailable"):
            await rerank("query", ["private document"], [{}], 3)


async def test_cross_encoder_filters_low_relevance_before_parent_expansion():
    with (
        patch.object(settings, "reranker_provider", "fastembed"),
        patch.object(settings, "rerank_fusion", "replace"),
        patch.object(settings, "rerank_min_score", 0.05),
        patch("app.services.reranker._local_rerank_scores", return_value=[0.01, 0.9, 0.6, 0.02]),
    ):
        texts, _metas, scores = await rerank(
            "query", ["noise", "a", "b", "noise2"], [{}, {}, {}, {}], 3
        )
    assert texts == ["a", "b"]
    assert scores == [0.9, 0.6]


async def test_rerank_http_error_falls_back_to_unranked():
    """When reranker API returns 500, fall back to unranked results truncated to k."""
    mock_response = httpx.Response(
        500,
        json={"error": "server error"},
        request=_FAKE_REQUEST,
    )
    with patch.object(
        httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_response
    ):
        texts, metas, scores = await rerank(
            "query",
            ["doc1", "doc2", "doc3"],
            [{"id": 1}, {"id": 2}, {"id": 3}],
            k=2,
        )

    # Should return first k documents unranked
    assert texts == ["doc1", "doc2"]
    assert metas == [{"id": 1}, {"id": 2}]
    assert scores == [0.0, 0.0]


async def test_rerank_connect_error_falls_back_to_unranked():
    """When reranker API is unreachable, fall back to unranked results."""
    with patch.object(
        httpx.AsyncClient,
        "post",
        new_callable=AsyncMock,
        side_effect=httpx.ConnectError("Connection refused"),
    ):
        texts, metas, scores = await rerank(
            "query",
            ["doc1", "doc2"],
            [{"id": 1}, {"id": 2}],
            k=5,
        )

    assert texts == ["doc1", "doc2"]
    assert metas == [{"id": 1}, {"id": 2}]
    assert scores == [0.0, 0.0]


async def test_rerank_timeout_falls_back_to_unranked():
    """A hanging reranker (ReadTimeout) must fail open, not 500 the search."""
    with patch.object(
        httpx.AsyncClient,
        "post",
        new_callable=AsyncMock,
        side_effect=httpx.ReadTimeout("timed out"),
    ):
        texts, _metas, scores = await rerank(
            "query",
            ["doc1", "doc2"],
            [{"id": 1}, {"id": 2}],
            k=5,
        )

    assert texts == ["doc1", "doc2"]
    assert scores == [0.0, 0.0]


async def test_rerank_malformed_response_falls_back_to_unranked():
    """A 200 with an unexpected body (no 'results') must fail open."""
    mock_response = httpx.Response(
        200,
        json={"unexpected": "shape"},
        request=_FAKE_REQUEST,
    )
    with patch.object(
        httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_response
    ):
        texts, _metas, scores = await rerank(
            "query",
            ["doc1", "doc2"],
            [{"id": 1}, {"id": 2}],
            k=5,
        )

    assert texts == ["doc1", "doc2"]
    assert scores == [0.0, 0.0]


async def test_rerank_score_count_mismatch_falls_back_to_unranked():
    """Fewer scores than documents (reranker capped the list) must fail open."""
    mock_response = httpx.Response(
        200,
        json={"results": [{"index": 0, "relevance_score": 0.9}]},
        request=_FAKE_REQUEST,
    )
    with patch.object(
        httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_response
    ):
        texts, _metas, scores = await rerank(
            "query",
            ["doc1", "doc2", "doc3"],
            [{}, {}, {}],
            k=3,
        )

    assert texts == ["doc1", "doc2", "doc3"]
    assert scores == [0.0, 0.0, 0.0]


async def test_rerank_failure_increments_failure_counter():
    """The fail-open path must bump reranker_failures_total."""
    from prometheus_client import REGISTRY

    before = REGISTRY.get_sample_value("reranker_failures_total") or 0.0
    with patch.object(
        httpx.AsyncClient,
        "post",
        new_callable=AsyncMock,
        side_effect=httpx.ConnectError("Connection refused"),
    ):
        await rerank("query", ["doc1"], [{"id": 1}], k=5)
    after = REGISTRY.get_sample_value("reranker_failures_total")
    assert after == before + 1
