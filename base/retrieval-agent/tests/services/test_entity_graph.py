"""Graph transaction, multi-hop scope and live-path regressions using explicit fixtures."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from qdrant_client import models

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from src.rag import entity_graph as store

from app.services import entity_graph as retrieval


def entity(name, label, text):
    start = text.index(name)
    return {
        "key": store.text_hash(label + "\0" + store.normalize(name)),
        "name": name,
        "normalized": store.normalize(name),
        "label": label,
        "start": start,
        "end": start + len(name),
    }


def graph_fixture(tmp_path, monkeypatch):
    first = "Acme acquired Northstar."
    second = "Northstar is based in Boston."
    facts = {}
    for text, left, right, label, predicate in (
        (first, "Acme", "Northstar", "ORG", "acquire"),
        (second, "Northstar", "Boston", "GPE", "base_in"),
    ):
        source, target = entity(left, "ORG", text), entity(right, label, text)
        facts[text] = {
            "entities": [source, target],
            "relations": [
                {
                    "source": source["key"],
                    "target": target["key"],
                    "predicate": predicate,
                    "start": 0,
                    "end": len(text),
                    "quote": text,
                }
            ],
        }
    monkeypatch.setattr(store, "extract", facts.__getitem__)
    monkeypatch.setattr(store, "extractor_version", lambda: "fixture-extractor")
    monkeypatch.setattr(
        store,
        "_model",
        lambda _lang: (
            lambda query, **kwargs: SimpleNamespace(
                ents=[SimpleNamespace(text=query, label_="ORG")]
            )
        ),
    )
    graph = store.EntityGraph(tmp_path / "graph.sqlite")
    points = [
        models.Record(
            id=1,
            payload={
                "content": first,
                "meta": {"collection_name": "private", "file_id": "one", "ingest_version": "v1"},
            },
        ),
        models.Record(
            id=2,
            payload={
                "content": second,
                "meta": {"collection_name": "public", "file_id": "two", "ingest_version": "v1"},
            },
        ),
    ]
    graph.index("corpus", points)
    return graph, points


def test_two_hop_traversal_requires_authorization_for_every_edge(tmp_path, monkeypatch):
    graph, _ = graph_fixture(tmp_path, monkeypatch)
    assert graph.search("corpus", ["public"], "Acme", 10) == []
    result = graph.search("corpus", ["private", "public"], "Acme", 10)
    second = next(hit for hit in result if hit["point_id"] == "2")
    assert second["hops"] == 2
    assert [edge["point_id"] for edge in second["path"]] == ["1", "2"]
    assert graph.search("another-corpus", ["private", "public"], "Acme", 10) == []


def test_library_shards_isolate_entities_and_walks_even_for_combined_search(tmp_path, monkeypatch):
    _, points = graph_fixture(tmp_path, monkeypatch)
    graph = store.ScopedEntityGraph(tmp_path / "sharded.sqlite")
    graph.index("corpus", points)

    private = graph.for_scope("corpus", "private")
    public = graph.for_scope("corpus", "public")
    assert private.path != public.path
    with private.connect(read_only=True) as connection:
        assert {row[0] for row in connection.execute("SELECT scope FROM chunks")} == {"private"}
    with public.connect(read_only=True) as connection:
        assert {row[0] for row in connection.execute("SELECT scope FROM chunks")} == {"public"}

    assert graph.search("corpus", ["unknown"], "Acme", 10) == []
    assert graph.for_scope("corpus", "unknown") is None
    assert [
        hit["point_id"] for hit in graph.search("corpus", ["private", "public"], "Acme", 10)
    ] == ["1"]
    graph.remove_versions("corpus", "one")
    assert graph.search("corpus", ["private", "public"], "Acme", 10) == []
    assert graph.stats("corpus")["chunks"] == 1
    assert graph.for_scope("corpus", "public").stats("corpus")["relations"] == 1


def test_reindex_updates_moved_scope_even_when_content_is_unchanged(tmp_path, monkeypatch):
    graph, points = graph_fixture(tmp_path, monkeypatch)
    points[0].payload["meta"]["collection_name"] = "moved"
    graph.index("corpus", points[:1])
    assert graph.search("corpus", ["private", "public"], "Acme", 10) == []
    assert any(hit["hops"] == 2 for hit in graph.search("corpus", ["moved", "public"], "Acme", 10))


def test_failed_extraction_keeps_the_previous_graph_transaction(tmp_path, monkeypatch):
    graph, points = graph_fixture(tmp_path, monkeypatch)
    before = graph.stats("corpus")
    points[0].payload["content"] = "A replacement that cannot be extracted."
    monkeypatch.setattr(store, "extract", Mock(side_effect=RuntimeError("extractor failed")))
    import pytest

    with pytest.raises(RuntimeError, match="extractor failed"):
        graph.index("corpus", points[:1], replace_file=True)
    assert graph.stats("corpus") == before


def test_delete_prunes_only_orphan_cache_and_entities(tmp_path, monkeypatch):
    graph, _ = graph_fixture(tmp_path, monkeypatch)
    graph.remove_versions("corpus", "one")
    assert graph.stats("corpus") == {"chunks": 1, "mentions": 2, "relations": 1, "entities": 2}
    assert graph.search("corpus", ["private", "public"], "Acme", 10) == []
    with graph.connect() as database:
        assert database.execute("SELECT count(*) FROM extraction_cache").fetchone()[0] == 1


def test_live_path_rejects_deleted_support_and_hides_unselected_quotes(tmp_path, monkeypatch):
    graph, points = graph_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(retrieval, "get_graph", lambda: graph)
    monkeypatch.setattr(retrieval.settings, "qdrant_index", "corpus")
    client = Mock()
    monkeypatch.setattr(retrieval.qdrant, "get_client", lambda: client)
    client.scroll.return_value = ([points[1]], None)
    assert retrieval.search("Acme", ["private", "public"], 10) == []
    client.scroll.return_value = (points, None)
    result = retrieval.search("Acme", ["private", "public"], 10)
    target = next(point for point in result if point.id == 2)
    assert target.payload["meta"]["graph_hops"] == 2
    assert all("quote" not in edge for edge in target.payload["meta"]["graph_path"])
    points[0].payload["meta"]["ingest_version"] = "v2"
    assert retrieval.search("Acme", ["private", "public"], 10) == []


def test_two_hop_path_cannot_mix_versions_of_one_file(tmp_path, monkeypatch):
    graph, points = graph_fixture(tmp_path, monkeypatch)
    points[1].payload["meta"].update(collection_name="private", file_id="one", ingest_version="v2")
    graph.index("corpus", points[1:])
    monkeypatch.setattr(retrieval, "get_graph", lambda: graph)
    monkeypatch.setattr(retrieval.settings, "qdrant_index", "corpus")
    client = Mock()
    client.scroll.return_value = (points, None)
    monkeypatch.setattr(retrieval.qdrant, "get_client", lambda: client)
    assert all(point.id != 2 for point in retrieval.search("Acme", ["private"], 10))
