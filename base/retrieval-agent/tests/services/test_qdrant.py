"""vector_search tests against the Phase 3 schema (content/meta + collection_name filter)."""

from unittest.mock import MagicMock, patch

import pytest
from qdrant_client import models
from qdrant_client.http.exceptions import UnexpectedResponse

from app.models import RetrievalResult
from app.services import qdrant
from app.services.qdrant import dense_vector_name, reset_sparse_capability, vector_search


@pytest.mark.parametrize(
    ("uri", "trust_env"),
    [
        ("http://127.0.0.1:6333", False),
        ("http://[::1]:6333", False),
        ("localhost:6333", False),
        ("https://localhost:6333", True),
        ("https://db.example.com:6333", True),
    ],
)
def test_qdrant_bypasses_proxy_for_local_http_only(monkeypatch, uri, trust_env):
    monkeypatch.setattr(qdrant, "_client", None)
    monkeypatch.setattr(qdrant.settings, "qdrant_uri", uri)
    with patch.object(qdrant, "QdrantClient") as constructor:
        assert qdrant.get_client() is constructor.return_value
    assert constructor.call_args.kwargs["trust_env"] is trust_env


def _make_point(id, content, meta, score):
    point = MagicMock()
    point.id = id
    point.payload = {"content": content, "meta": meta}
    point.score = score
    return point


async def test_vector_search_basic():
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.points = [
        _make_point("1", "hello world", {"source": "a", "collection_name": "file-abc"}, 0.8),
        _make_point("2", "foo bar", {"source": "b", "collection_name": "file-abc"}, 0.6),
    ]
    mock_client.query_points.return_value = mock_response

    with (
        patch("app.services.qdrant.get_client", return_value=mock_client),
        patch("app.services.qdrant.has_sparse_vectors", return_value=False),
    ):
        result = await vector_search(["file-abc"], [0.1, 0.2], None, k=5)

    assert isinstance(result, RetrievalResult)
    assert result.texts == ["hello world", "foo bar"]
    assert result.metadatas == [
        {"source": "a", "collection_name": "file-abc"},
        {"source": "b", "collection_name": "file-abc"},
    ]
    # Cosine normalization (dense path): (score + 1) / 2
    assert result.distances[0] == (0.8 + 1.0) / 2.0
    assert result.distances[1] == (0.6 + 1.0) / 2.0


async def test_vector_search_empty_payload():
    mock_client = MagicMock()
    mock_response = MagicMock()
    point = MagicMock()
    point.payload = None
    point.score = 0.5
    mock_response.points = [point]
    mock_client.query_points.return_value = mock_response

    with (
        patch("app.services.qdrant.get_client", return_value=mock_client),
        patch("app.services.qdrant.has_sparse_vectors", return_value=False),
    ):
        result = await vector_search(["file-abc"], [0.1], None, k=5)

    assert result.texts == [""]
    assert result.metadatas == [{}]


async def test_vector_search_unexpected_response_returns_empty():
    """UnexpectedResponse (e.g. collection not found) returns empty results."""
    mock_client = MagicMock()
    mock_client.query_points.side_effect = UnexpectedResponse(
        status_code=404, content=b"Not found", reason_phrase="Not Found", headers={}
    )

    with (
        patch("app.services.qdrant.get_client", return_value=mock_client),
        patch("app.services.qdrant.has_sparse_vectors", return_value=False),
    ):
        result = await vector_search(["file-abc"], [0.1], None, k=5)

    assert result.texts == []
    assert result.metadatas == []
    assert result.distances == []


async def test_vector_search_connection_error_propagates():
    """Connection errors (not UnexpectedResponse) should propagate."""
    mock_client = MagicMock()
    mock_client.query_points.side_effect = ConnectionError("connection refused")

    with (
        patch("app.services.qdrant.get_client", return_value=mock_client),
        patch("app.services.qdrant.has_sparse_vectors", return_value=False),
        pytest.raises(ConnectionError),
    ):
        await vector_search(["file-abc"], [0.1], None, k=5)


async def test_vector_search_filter_targets_meta_collection_name():
    """Filter scopes to meta.collection_name IN (collection_names) — the new schema."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.points = []
    mock_client.query_points.return_value = mock_response

    with (
        patch("app.services.qdrant.get_client", return_value=mock_client),
        patch("app.services.qdrant.has_sparse_vectors", return_value=False),
    ):
        await vector_search(["file-abc", "kb-xyz"], [0.1], None, k=5)

    call_kwargs = mock_client.query_points.call_args.kwargs
    qfilter = call_kwargs["query_filter"]
    assert isinstance(qfilter, models.Filter)
    assert len(qfilter.must) == 1
    cond = qfilter.must[0]
    assert cond.key == "meta.collection_name"
    assert cond.match.any == ["file-abc", "kb-xyz"]


async def test_document_filter_intersects_acl_and_checks_qdrant_result():
    client = MagicMock()
    meta = {"collection_name": "allowed", "file_id": "manual", "ingest_version": "v1"}
    client.query_points.return_value.points = [
        _make_point("1", "current", meta, 0.8),
        _make_point("2", "old", dict(meta, ingest_version="v0"), 0.7),
        _make_point("3", "private", dict(meta, collection_name="private"), 0.6),
    ]
    sparse = models.SparseVector(indices=[1], values=[1.0])
    with (
        patch("app.services.qdrant.get_client", return_value=client),
        patch("app.services.qdrant.has_sparse_vectors", return_value=True),
    ):
        denied = await vector_search(
            ["allowed"], [0.1], sparse, 10, document=("private", "manual", "v1")
        )
        client.query_points.assert_not_called()
        allowed = await vector_search(
            ["allowed"], [0.1], sparse, 10, document=("allowed", "manual", "v1")
        )
    assert denied.texts == []
    assert allowed.texts == ["current"]
    for prefetch in client.query_points.call_args.kwargs["prefetch"]:
        acl, *document = prefetch.filter.must
        assert acl.must[0].match.any == ["allowed"]
        assert [(item.key, item.match.value) for item in document] == [
            (f"meta.{field}", value) for field, value in meta.items()
        ]


async def test_vector_search_uses_named_dense_vector():
    """A named dense collection must retain its configured vector name."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.points = []
    mock_client.query_points.return_value = mock_response

    with (
        patch("app.services.qdrant.get_client", return_value=mock_client),
        patch("app.services.qdrant.has_sparse_vectors", return_value=False),
        patch("app.services.qdrant.dense_vector_name", return_value="text-dense"),
    ):
        await vector_search(["file-abc"], [0.1, 0.2], None, k=5)

    call_kwargs = mock_client.query_points.call_args.kwargs
    assert call_kwargs["using"] == "text-dense"
    assert call_kwargs["query"] == [0.1, 0.2]
    # Pure dense path — no prefetch
    assert "prefetch" not in call_kwargs or call_kwargs.get("prefetch") is None


async def test_vector_search_uses_unnamed_dense_vector():
    """The ingestion service's dense-only index has no named vector."""
    mock_client = MagicMock()
    mock_client.get_collection.return_value.config.params.vectors = models.VectorParams(
        size=1024, distance=models.Distance.COSINE
    )
    mock_client.query_points.return_value.points = []

    reset_sparse_capability()
    with (
        patch("app.services.qdrant.get_client", return_value=mock_client),
        patch("app.services.qdrant.has_sparse_vectors", return_value=False),
    ):
        assert dense_vector_name() is None
        await vector_search(["nexrag_docs"], [0.1, 0.2], None, k=5)

    assert mock_client.query_points.call_args.kwargs["using"] is None
