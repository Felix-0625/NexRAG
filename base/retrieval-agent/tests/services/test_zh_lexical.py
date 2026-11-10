from types import SimpleNamespace
from unittest.mock import Mock

from app.services import zh_lexical


def test_cached_lexical_hit_requires_current_scope(monkeypatch):
    """A cached ID never returns a point that has since moved outside the allowed scope."""
    monkeypatch.setattr(zh_lexical, "_index", None)
    original = SimpleNamespace(
        id=1,
        payload={
            "content": "北京的博物馆",
            "meta": {
                "collection_name": "permitted",
                "file_id": "source",
                "parent_id": "p1",
            },
        },
    )
    moved = SimpleNamespace(
        id=1,
        payload={
            "content": "北京的博物馆",
            "meta": {
                "collection_name": "private",
                "file_id": "source",
                "parent_id": "p1",
            },
        },
    )
    client = Mock()
    client.scroll.side_effect = [([original], None), ([original], None), ([moved], None)]
    monkeypatch.setattr(zh_lexical.qdrant, "get_client", lambda: client)
    try:
        result = zh_lexical._search("北京博物馆", ("permitted",), 10)
        assert result is not None and result[0] == [original.payload["content"]]
        assert zh_lexical._search("北京博物馆", ("permitted",), 10) is None
        scope, identifiers = client.scroll.call_args.kwargs["scroll_filter"].must
        assert scope.must[0].match.any == ["permitted"]
        assert identifiers.has_id == [1]
    finally:
        if zh_lexical._index:
            zh_lexical._index.connection.close()
