"""Hybrid query path: native Qdrant Query API with prefetch + RRF when sparse is present."""

from unittest.mock import MagicMock, patch

import pytest
from qdrant_client import QdrantClient, models

from app.services.qdrant import has_sparse_vectors, reset_sparse_capability, vector_search


def _make_point(id, content, meta, score):
    point = MagicMock()
    point.id = id
    point.payload = {"content": content, "meta": meta}
    point.score = score
    return point


async def test_hybrid_when_sparse_present():
    """When sparse_vector is provided AND collection has sparse vectors, use prefetch + RRF."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.points = [_make_point("1", "doc", {"collection_name": "file-abc"}, 0.42)]
    mock_client.query_points.return_value = mock_response

    sparse = models.SparseVector(indices=[1, 5, 7], values=[0.5, 0.3, 0.2])

    with (
        patch("app.services.qdrant.get_client", return_value=mock_client),
        patch("app.services.qdrant.has_sparse_vectors", return_value=True),
    ):
        result = await vector_search(["file-abc"], [0.1, 0.2], sparse, k=5)

    call_kwargs = mock_client.query_points.call_args.kwargs
    assert "prefetch" in call_kwargs and call_kwargs["prefetch"] is not None
    prefetch = call_kwargs["prefetch"]
    assert len(prefetch) == 2

    dense_leg = next(p for p in prefetch if p.using == "text-dense")
    assert dense_leg.query == [0.1, 0.2]

    sparse_leg = next(p for p in prefetch if p.using == "text-sparse")
    assert sparse_leg.query == sparse

    assert isinstance(call_kwargs["query"], models.RrfQuery)
    assert call_kwargs["query"].rrf.k == 2
    assert call_kwargs["query"].rrf.weights == [1.0, 1.0]
    # Hybrid path returns RRF score directly (no cosine normalization)
    assert result.distances == [0.42]


@pytest.mark.parametrize("weights,expected", [((1.0, 0.1), "dense"), ((0.1, 1.0), "sparse")])
async def test_native_weights_change_ranking_without_crossing_scope(
    monkeypatch, weights, expected
):
    """Exercise the real Query API; an unauthorized strongest match stays excluded."""
    from app.config import settings

    client = QdrantClient(":memory:")
    client.create_collection(
        "ingestion_files",
        vectors_config={
            "text-dense": models.VectorParams(size=2, distance=models.Distance.COSINE)
        },
        sparse_vectors_config={"text-sparse": models.SparseVectorParams()},
    )
    points = []
    for index, (name, dense, sparse, scope) in enumerate(
        [
            ("dense", [1.0, 0.0], 1.0, "allowed"),
            ("sparse", [0.0, 1.0], 2.0, "allowed"),
            ("private", [1.0, 0.0], 100.0, "denied"),
        ]
    ):
        points.append(
            models.PointStruct(
                id=index,
                vector={
                    "text-dense": dense,
                    "text-sparse": models.SparseVector(indices=[1], values=[sparse]),
                },
                payload={"content": name, "meta": {"collection_name": scope}},
            )
        )
    client.upsert("ingestion_files", points)
    monkeypatch.setattr("app.services.qdrant.get_client", lambda: client)
    monkeypatch.setattr("app.services.qdrant.has_sparse_vectors", lambda: True)
    monkeypatch.setattr(settings, "hybrid_dense_weight", weights[0])
    monkeypatch.setattr(settings, "hybrid_sparse_weight", weights[1])
    result = await vector_search(
        ["allowed"], [1.0, 0.0], models.SparseVector(indices=[1], values=[1.0]), 2
    )
    assert result.texts[0] == expected
    assert set(result.texts) == {"dense", "sparse"}
    client.close()


async def test_dbsf_uses_native_distribution_fusion(monkeypatch):
    from app.config import settings

    client = MagicMock()
    client.query_points.return_value.points = []
    monkeypatch.setattr("app.services.qdrant.get_client", lambda: client)
    monkeypatch.setattr("app.services.qdrant.has_sparse_vectors", lambda: True)
    monkeypatch.setattr(settings, "hybrid_fusion", "dbsf")
    await vector_search(["allowed"], [1.0], models.SparseVector(indices=[1], values=[1.0]), 2)
    assert client.query_points.call_args.kwargs["query"].fusion == models.Fusion.DBSF


async def test_hybrid_falls_back_to_dense_when_collection_has_no_sparse():
    """Even with a sparse_vector, dense-only path runs if the collection lacks sparse vectors."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.points = []
    mock_client.query_points.return_value = mock_response
    mock_client.get_collection.return_value.config.params.vectors = {
        "text-dense": models.VectorParams(size=1024, distance=models.Distance.COSINE)
    }

    sparse = models.SparseVector(indices=[1], values=[0.5])

    with (
        patch("app.services.qdrant.get_client", return_value=mock_client),
        patch("app.services.qdrant.has_sparse_vectors", return_value=False),
    ):
        await vector_search(["file-abc"], [0.1], sparse, k=5)

    call_kwargs = mock_client.query_points.call_args.kwargs
    # Dense-only call shape: no prefetch; bare ``query`` + ``using``
    assert call_kwargs.get("prefetch") in (None, [])
    assert call_kwargs["using"] == "text-dense"
    assert call_kwargs["query"] == [0.1]


async def test_has_sparse_vectors_caches_result():
    """has_sparse_vectors caches; reset_sparse_capability clears the cache."""
    reset_sparse_capability()

    mock_info = MagicMock()
    mock_info.config.params.sparse_vectors = {"text-sparse": MagicMock()}
    mock_client = MagicMock()
    mock_client.get_collection.return_value = mock_info

    with patch("app.services.qdrant.get_client", return_value=mock_client):
        # First call queries Qdrant
        assert has_sparse_vectors() is True
        # Second call uses cache (no second get_collection call)
        assert has_sparse_vectors() is True
        assert mock_client.get_collection.call_count == 1

        reset_sparse_capability()
        # After reset, queries Qdrant again
        assert has_sparse_vectors() is True
        assert mock_client.get_collection.call_count == 2


async def test_has_sparse_vectors_false_when_collection_missing():
    """Collection-doesn't-exist returns False without caching, so we re-detect later."""
    from qdrant_client.http.exceptions import UnexpectedResponse

    reset_sparse_capability()

    mock_client = MagicMock()
    mock_client.get_collection.side_effect = UnexpectedResponse(
        status_code=404, content=b"Not found", reason_phrase="Not Found", headers={}
    )

    with patch("app.services.qdrant.get_client", return_value=mock_client):
        assert has_sparse_vectors() is False
        # First ingest will create the collection — re-detect on next call
        assert has_sparse_vectors() is False
        assert mock_client.get_collection.call_count == 2
