"""Verify graph provenance against live original chunks."""

from __future__ import annotations

from contextlib import ExitStack
from typing import TYPE_CHECKING

from src.rag.entity_extraction import extractor_version, normalize, text_hash

if TYPE_CHECKING:
    from src.rag.entity_graph import ScopedEntityGraph


def verify_collection(client, corpus: str, graph: ScopedEntityGraph) -> dict:
    """Check all stored mentions and relation quotes against live original chunks."""
    from qdrant_client import models

    counts = {"chunks": 0, "mentions": 0, "relations": 0, "errors": 0, "examples": []}
    offset = None

    def fail(point_id, reason):
        """Fail."""
        counts["errors"] += 1
        if len(counts["examples"]) < 50:
            counts["examples"].append({"point_id": str(point_id), "reason": reason})

    version = extractor_version()
    while True:
        with ExitStack() as connections:
            by_scope = {}
            points, offset = client.scroll(
                collection_name=corpus,
                offset=offset,
                limit=256,
                with_vectors=False,
                with_payload=models.PayloadSelectorInclude(
                    include=[
                        "content",
                        "meta.collection_name",
                        "meta.file_id",
                        "meta.ingest_version",
                    ]
                ),
            )
            for point in points:
                payload = point.payload or {}
                text, meta = payload.get("content", ""), payload.get("meta", {})
                scope = meta.get("collection_name")
                if scope not in by_scope:
                    shard = graph.for_scope(corpus, scope) if isinstance(scope, str) else None
                    by_scope[scope] = (
                        connections.enter_context(shard.connect(read_only=True)) if shard else None
                    )
                connection = by_scope[scope]
                if connection is None:
                    fail(point.id, "missing graph scope")
                    continue
                key = text_hash(corpus + "\0" + str(point.id))
                row = connection.execute("SELECT * FROM chunks WHERE id=?", (key,)).fetchone()
                if not row:
                    fail(point.id, "missing graph chunk")
                    continue
                counts["chunks"] += 1
                if (
                    row["content_hash"] != text_hash(text)
                    or row["scope"] != meta.get("collection_name")
                    or row["file_id"] != meta.get("file_id")
                    or row["version"] != meta.get("ingest_version")
                    or row["extractor"] != version
                ):
                    fail(point.id, "chunk provenance mismatch")
                mentions = connection.execute(
                    "SELECT m.*,e.normalized FROM mentions m JOIN entities e ON e.id=m.entity_id "
                    "WHERE m.chunk_id=?",
                    (key,),
                ).fetchall()
                entity_ids = {mention["entity_id"] for mention in mentions}
                for mention in mentions:
                    counts["mentions"] += 1
                    if (
                        not 0 <= mention["start"] < mention["end"] <= len(text)
                        or normalize(text[mention["start"] : mention["end"]])
                        != mention["normalized"]
                    ):
                        fail(point.id, "entity span mismatch")
                for relation in connection.execute(
                    "SELECT * FROM relations WHERE chunk_id=?", (key,)
                ):
                    counts["relations"] += 1
                    if (
                        relation["source"] not in entity_ids
                        or relation["target"] not in entity_ids
                        or not 0 <= relation["start"] < relation["end"] <= len(text)
                        or text[relation["start"] : relation["end"]] != relation["quote"]
                    ):
                        fail(point.id, "relation evidence mismatch")
        if offset is None:
            break
    counts["stored_chunks"] = graph.stats(corpus)["chunks"]
    counts["passed"] = counts["errors"] == 0 and counts["chunks"] == counts["stored_chunks"]
    return counts
