"""Publish derived graph facts only from the actual written chunk version."""

from functools import lru_cache

from qdrant_client.http.models import FieldCondition, Filter, MatchValue

from app.config import settings
from app.pipelines.indexing import _active_settings, _raw_qdrant_client


@lru_cache(maxsize=1)
def get_graph():
    from src.rag.entity_graph import ScopedEntityGraph

    return ScopedEntityGraph(settings.graph_store_path)


def index_version(file_id: str, scope: str, version: str) -> dict:
    active = _active_settings()
    points = []
    offset = None
    while True:
        rows, offset = _raw_qdrant_client().scroll(
            collection_name=active.qdrant_index,
            scroll_filter=Filter(
                must=[
                    FieldCondition(key="meta.file_id", match=MatchValue(value=file_id)),
                    FieldCondition(key="meta.collection_name", match=MatchValue(value=scope)),
                    FieldCondition(key="meta.ingest_version", match=MatchValue(value=version)),
                ]
            ),
            offset=offset,
            limit=128,
            with_payload=True,
            with_vectors=False,
        )
        points.extend(rows)
        if offset is None:
            break
    if not points:
        raise RuntimeError("Written chunk version was unavailable for entity-graph indexing")
    return get_graph().index(active.qdrant_index, points)
