"""Entity GraphRAG candidates with live authorization and full-path verification."""

import logging
from functools import lru_cache

from qdrant_client import models

from app.config import settings
from app.services import qdrant

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_graph():
    from src.rag.entity_graph import ScopedEntityGraph

    return ScopedEntityGraph(settings.graph_store_path)


def warm_up():
    from src.rag.entity_graph import warm_up as warm_models

    get_graph()
    warm_models()


def search(query: str, scopes: list[str], k: int) -> list[models.Record]:
    from src.rag.entity_graph import text_hash

    hits = get_graph().search(settings.qdrant_index, scopes, query, k)
    if not hits:
        return []
    ids = {
        int(edge["point_id"]) if edge["point_id"].isdecimal() else edge["point_id"]
        for hit in hits
        for edge in hit["path"]
    }
    points, _ = qdrant.get_client().scroll(
        collection_name=settings.qdrant_index,
        scroll_filter=models.Filter(
            must=[
                qdrant._collection_name_filter(scopes),
                models.HasIdCondition(has_id=sorted(ids, key=str)),
            ]
        ),
        limit=len(ids),
        with_payload=True,
        with_vectors=False,
    )
    live = {str(point.id): point for point in points}
    result = []
    for hit in hits:
        valid = True
        versions = {}
        for edge in hit["path"]:
            identity = (edge["scope"], edge["file_id"])
            if identity in versions and versions[identity] != edge["version"]:
                valid = False
                break
            versions[identity] = edge["version"]
            point = live.get(edge["point_id"])
            payload = point.payload if point is not None else {}
            meta = (payload or {}).get("meta", {})
            text = (payload or {}).get("content", "")
            if (
                meta.get("collection_name") != edge["scope"]
                or meta.get("collection_name") not in scopes
                or meta.get("file_id") != edge["file_id"]
                or meta.get("ingest_version") != edge["version"]
                or not isinstance(text, str)
                or text_hash(text) != edge["content_hash"]
                or text[edge["start"] : edge["end"]] != edge["quote"]
            ):
                valid = False
                break
        if not valid:
            continue
        point = live[hit["point_id"]]
        payload = dict(point.payload)
        # Keep path evidence in the graph store. Only opaque provenance reaches
        # the caller so unselected chunks cannot expand the effective context K.
        provenance = [
            {key: edge[key] for key in ("edge_id", "point_id", "scope", "version", "content_hash")}
            for edge in hit["path"]
        ]
        payload["meta"] = dict(
            payload.get("meta", {}), graph_path=provenance, graph_hops=hit["hops"]
        )
        result.append(point.model_copy(update={"payload": payload}))
    log.info(
        "Entity graph candidates=%d verified=%d max_hops=%d",
        len(hits),
        len(result),
        max((point.payload["meta"]["graph_hops"] for point in result), default=0),
    )
    return result
