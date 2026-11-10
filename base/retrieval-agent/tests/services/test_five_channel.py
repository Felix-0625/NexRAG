from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from qdrant_client import models

from app.services import five_channel


def _point(point_id: int, content: str, *, scope: str = "allowed", parent: int = 0):
    return models.ScoredPoint(
        id=point_id,
        version=1,
        score=1.0,
        payload={
            "content": content,
            "meta": {
                "collection_name": scope,
                "file_id": f"file-{point_id}",
                "ingest_version": "version-1",
                "parent_split_id": parent,
                "filename": "Safety procedures.md",
            },
        },
    )


@pytest.mark.parametrize("body_enabled", [True, False])
def test_channels_add_distinct_points_with_scope_and_version_filters(monkeypatch, body_enabled):
    dense = _point(1, "dense")
    sparse = _point(2, "sparse")
    body = _point(3, "Safety procedure")
    title = _point(4, "title")
    neighbor = _point(5, "Safety procedure nearby", parent=1)
    neighbor.payload["meta"]["file_id"] = dense.payload["meta"]["file_id"]
    denied = _point(9, "Safety procedure", scope="private")
    mock_client = Mock()
    lexical_rows = [[body, denied]] if body_enabled else []
    mock_client.query_batch_points.side_effect = [
        [SimpleNamespace(points=rows) for rows in ([dense], [sparse], *lexical_rows, [title], [])],
        [
            SimpleNamespace(points=[neighbor, denied]),
            SimpleNamespace(points=[]),
            SimpleNamespace(points=[]),
            SimpleNamespace(points=[]),
        ],
    ]
    monkeypatch.setattr(five_channel, "_indexes_ready", lambda: True)
    monkeypatch.setattr(five_channel.qdrant, "get_client", lambda: mock_client)
    monkeypatch.setattr(five_channel.settings, "enable_body_lexical_retrieval", body_enabled)

    result = five_channel._query(
        "Safety procedure", ["allowed"], [0.1], models.SparseVector(indices=[1], values=[1.0]), 5
    )
    assert result is not None
    expected = {
        "dense": ["dense"],
        "sparse": ["sparse"],
        "title": ["title_filename"],
        "Safety procedure nearby": ["document_structure"],
    }
    if body_enabled:
        expected["Safety procedure"] = ["body_lexical"]
    assert {
        text: meta["retrieval_channels"]
        for text, meta in zip(result.texts, result.metadatas, strict=True)
    } == expected
    first, second = mock_client.query_batch_points.call_args_list
    assert [request.using for request in first.kwargs["requests"][:2]] == [
        "text-dense",
        "text-sparse",
    ]
    expected_fields = ["meta.filename", "meta.headers_breadcrumb"]
    if body_enabled:
        expected_fields.insert(0, "content")
    actual_fields = [request.filter.must[1].key for request in first.kwargs["requests"][2:]]
    assert actual_fields == expected_fields
    assert all(
        {"meta.collection_name", "meta.file_id", "meta.ingest_version"}
        <= {condition.key for condition in request.filter.must}
        for request in second.kwargs["requests"]
    )
    assert [request.using for request in second.kwargs["requests"]] == [
        "text-dense",
        None,
        "text-dense",
        None,
    ]
    assert all(
        request.filter.must_not[0].key == "meta.parent_split_id"
        for request in second.kwargs["requests"][1::2]
    )


def test_five_channels_defer_to_legacy_search_until_upgrade(monkeypatch):
    monkeypatch.setattr(five_channel, "_capability", (0, False))
    mock_client = Mock()
    mock_client.get_collection.return_value = SimpleNamespace(payload_schema={})
    monkeypatch.setattr(five_channel.qdrant, "get_client", lambda: mock_client)

    assert (
        five_channel._query(
            "test", ["allowed"], [0.1], models.SparseVector(indices=[], values=[]), 5
        )
        is None
    )
    mock_client.query_batch_points.assert_not_called()


@pytest.mark.parametrize("body_enabled", [True, False])
def test_body_index_is_required_only_when_body_channel_is_enabled(monkeypatch, body_enabled):
    monkeypatch.setattr(five_channel, "_capability", (0, False))
    monkeypatch.setattr(five_channel.settings, "enable_body_lexical_retrieval", body_enabled)
    client = Mock()
    client.get_collection.return_value = SimpleNamespace(
        payload_schema={
            field: SimpleNamespace(data_type=kind)
            for field, kind in five_channel._FIELD_TYPES.items()
            if field != "content"
        }
    )
    monkeypatch.setattr(five_channel.qdrant, "get_client", lambda: client)
    assert five_channel._indexes_ready() is not body_enabled


def test_fusion_preserves_live_verified_graph_provenance_for_vector_duplicate():
    point = _point(1, "Original evidence")
    graph_payload = dict(point.payload)
    graph_payload["meta"] = dict(point.payload["meta"], graph_hops=2, graph_path=[{"edge_id": 17}])
    graph_point = point.model_copy(update={"payload": graph_payload})
    result = five_channel._rrf([("dense", 1.0, [point]), ("entity_graph", 0.65, [graph_point])])
    assert result[0][0].payload["meta"]["graph_path"] == [{"edge_id": 17}]
    assert result[0][2] == ("dense", "entity_graph")


@pytest.mark.parametrize("same_version", [True, False])
def test_document_reserve_preserves_broad_head_and_requires_consensus(same_version):
    def hit(point_id, file_id, parent_id, version="version-1"):
        point = _point(point_id, f"clause {point_id}")
        point.payload["meta"].update(file_id=file_id, parent_id=parent_id, ingest_version=version)
        return point, 1.0, ("dense",)

    hits = [
        hit(1, "contract", "first"),
        hit(2, "contract", "first"),
        hit(3, "contract", "second", "version-1" if same_version else "version-2"),
        hit(4, "different-contract", "second"),
        hit(5, "contract", "first"),
        hit(6, "different-contract", "third"),
        hit(7, "contract", "third", "version-2"),
        hit(8, "contract", "fourth"),
        hit(9, "contract", "fourth"),
    ]

    result = five_channel._reserve_document_candidates(hits, 6)

    assert [point.id for point, _, _ in result] == (
        [1, 2, 3, 8, 4, 5] if same_version else [1, 2, 3, 4, 5, 6]
    )
