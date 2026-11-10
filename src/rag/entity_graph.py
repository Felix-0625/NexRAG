"""Persistent entity/relation graph with original-chunk provenance."""

from __future__ import annotations

import json
import sqlite3
from collections import OrderedDict
from contextlib import ExitStack, closing, contextmanager, nullcontext
from pathlib import Path
from threading import local

from src.rag.entity_extraction import (
    _LABELS,
    _LOCK,
    _language,
    _model,
    extract,
    extractor_version,
    normalize,
    text_hash,
    warm_up,
)
from src.rag.graph_verification import verify_collection

__all__ = [
    "EntityGraph",
    "ScopedEntityGraph",
    "backfill",
    "extract",
    "extractor_version",
    "normalize",
    "text_hash",
    "verify_collection",
    "warm_up",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks(
    id TEXT PRIMARY KEY, corpus TEXT NOT NULL, point_id TEXT NOT NULL,
    scope TEXT NOT NULL, file_id TEXT NOT NULL, version TEXT NOT NULL,
    content_hash TEXT NOT NULL, extractor TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunk_scope ON chunks(corpus,scope,file_id,version);
CREATE INDEX IF NOT EXISTS chunk_hash ON chunks(content_hash,extractor);
CREATE TABLE IF NOT EXISTS entities(
    id TEXT PRIMARY KEY, name TEXT NOT NULL, normalized TEXT NOT NULL, label TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS entity_name ON entities(normalized);
CREATE TABLE IF NOT EXISTS mentions(
    chunk_id TEXT REFERENCES chunks(id) ON DELETE CASCADE,
    entity_id TEXT REFERENCES entities(id), start INTEGER NOT NULL, end INTEGER NOT NULL,
    PRIMARY KEY(chunk_id,entity_id,start)
);
CREATE INDEX IF NOT EXISTS mention_entity ON mentions(entity_id,chunk_id);
CREATE TABLE IF NOT EXISTS relations(
    id INTEGER PRIMARY KEY, source TEXT REFERENCES entities(id),
    target TEXT REFERENCES entities(id), predicate TEXT NOT NULL,
    chunk_id TEXT REFERENCES chunks(id) ON DELETE CASCADE,
    start INTEGER NOT NULL, end INTEGER NOT NULL, quote TEXT NOT NULL,
    UNIQUE(source,target,predicate,chunk_id,start)
);
CREATE INDEX IF NOT EXISTS relation_source ON relations(source,chunk_id);
CREATE INDEX IF NOT EXISTS relation_target ON relations(target,chunk_id);
CREATE INDEX IF NOT EXISTS relation_chunk ON relations(chunk_id);
CREATE TABLE IF NOT EXISTS extraction_cache(
    hash TEXT NOT NULL, extractor TEXT NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY(hash,extractor)
);
"""


class EntityGraph:
    def __init__(self, path: str | Path, *, create: bool = True):
        self.path = Path(path).absolute()
        if not create:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1}:
                raise ValueError("Unsupported entity graph schema")
            if version == 0:
                connection.executescript(_SCHEMA)
                connection.execute("PRAGMA user_version=1")

    def open_connection(self, *, read_only: bool = False):
        """Open connection."""
        database = self.path.as_uri() + "?mode=ro" if read_only else self.path
        connection = sqlite3.connect(database, timeout=30, uri=read_only)
        if not read_only:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def connect(self, *, read_only: bool = False):
        """Connect."""
        connection = self.open_connection(read_only=read_only)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def index(self, corpus: str, points: list, *, replace_file: bool = False, prior=None) -> dict:
        """Publish one complete file version atomically, or append backfill pages."""
        version = extractor_version()
        prepared = []
        cache_updates = []
        cache_misses = 0
        unchanged = 0
        reused = 0
        with self.connect() as connection:
            for point in points:
                payload = point.payload or {}
                text = payload.get("content", "")
                meta = payload.get("meta", {})
                if not text or not all(
                    meta.get(key) for key in ("collection_name", "file_id", "ingest_version")
                ):
                    continue
                digest = text_hash(text)
                key = text_hash(corpus + "\0" + str(point.id))
                existing = connection.execute(
                    "SELECT content_hash,extractor,scope,file_id,version FROM chunks WHERE id=?",
                    (key,),
                ).fetchone()
                if (
                    existing
                    and existing["content_hash"] == digest
                    and existing["extractor"] == version
                    and existing["scope"] == meta["collection_name"]
                    and existing["file_id"] == meta["file_id"]
                    and existing["version"] == meta["ingest_version"]
                ):
                    unchanged += 1
                    continue
                cached = connection.execute(
                    "SELECT data FROM extraction_cache WHERE hash=? AND extractor=?",
                    (digest, version),
                ).fetchone()
                if not cached and prior is not None:
                    cached = prior.execute(
                        "SELECT data FROM extraction_cache WHERE hash=? AND extractor=?",
                        (digest, version),
                    ).fetchone()
                    if cached:
                        cache_updates.append((digest, version, cached[0]))
                        reused += 1
                if cached:
                    facts = json.loads(cached[0])
                else:
                    facts = extract(text)
                    cache_misses += 1
                    cache_updates.append((digest, version, json.dumps(facts, ensure_ascii=False)))
                prepared.append((point, meta, digest, facts))
            # NLP runs before taking the write lock so large uploads do not
            # block unrelated graph readers or other files' short commits.
            connection.executemany(
                "INSERT OR IGNORE INTO extraction_cache VALUES (?,?,?)", cache_updates
            )
            files = {(meta["collection_name"], meta["file_id"]) for _, meta, _, _ in prepared}
            if replace_file:
                for scope, file_id in files:
                    connection.execute(
                        "DELETE FROM chunks WHERE corpus=? AND scope=? AND file_id=?",
                        (corpus, scope, file_id),
                    )
            for point, meta, digest, facts in prepared:
                self._write_point(connection, corpus, point, meta, digest, facts, version)
        return {
            "chunks": len(prepared),
            "unchanged": unchanged,
            "real_extractions": cache_misses,
            "legacy_extractions_reused": reused,
            "mentions": sum(len(facts["entities"]) for _, _, _, facts in prepared),
            "relations": sum(len(facts["relations"]) for _, _, _, facts in prepared),
        }

    def _write_point(self, connection, corpus, point, meta, digest, facts, version):
        key = text_hash(corpus + "\0" + str(point.id))
        connection.execute("DELETE FROM chunks WHERE id=?", (key,))
        connection.execute(
            "INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?)",
            (
                key,
                corpus,
                str(point.id),
                meta["collection_name"],
                meta["file_id"],
                meta["ingest_version"],
                digest,
                version,
            ),
        )
        for entity in facts["entities"]:
            connection.execute(
                "INSERT OR IGNORE INTO entities VALUES (?,?,?,?)",
                (entity["key"], entity["name"], entity["normalized"], entity["label"]),
            )
            connection.execute(
                "INSERT OR IGNORE INTO mentions VALUES (?,?,?,?)",
                (key, entity["key"], entity["start"], entity["end"]),
            )
        for relation in facts["relations"]:
            connection.execute(
                "INSERT OR IGNORE INTO relations(source,target,predicate,chunk_id,start,end,quote)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    relation["source"],
                    relation["target"],
                    relation["predicate"],
                    key,
                    relation["start"],
                    relation["end"],
                    relation["quote"],
                ),
            )

    def search(self, corpus: str, scopes: list[str], query: str, k: int) -> list[dict]:
        """Search."""
        names = _query_entities(query) if scopes else []
        return self.search_entities(corpus, scopes, names, set(normalize(query).split()), k)

    def search_entities(self, corpus, scopes, names, terms, k, *, reader=None):
        """Search entities."""
        if not scopes or not names:
            return []
        scope_sql = ",".join("?" for _ in scopes)
        name_sql = ",".join("?" for _ in names)
        with (
            nullcontext(reader) if reader is not None else self.connect(read_only=True)
        ) as connection:
            if connection.execute("PRAGMA user_version").fetchone()[0] == 0:
                return []
            seeds = connection.execute(
                f"SELECT DISTINCT e.id FROM entities e JOIN mentions m ON m.entity_id=e.id "
                f"JOIN chunks c ON c.id=m.chunk_id WHERE c.corpus=? AND c.scope IN ({scope_sql}) "
                f"AND e.normalized IN ({name_sql}) AND c.extractor=? LIMIT 16",
                (corpus, *scopes, *names, extractor_version()),
            ).fetchall()
            frontier = [(row["id"], [], frozenset({row["id"]})) for row in seeds]
            hits = {}
            for hop in range(1, 3):
                next_frontier = []
                for entity_id, path, visited in frontier[:16]:
                    rows = connection.execute(
                        f"SELECT r.*,c.point_id,c.version,c.content_hash,c.scope,c.file_id,"
                        f"e1.name AS source_name,e2.name AS target_name FROM relations r "
                        f"JOIN chunks c ON c.id=r.chunk_id JOIN entities e1 ON e1.id=r.source "
                        f"JOIN entities e2 ON e2.id=r.target "
                        f"WHERE c.corpus=? AND c.scope IN ({scope_sql}) "
                        f"AND (r.source=? OR r.target=?) AND c.extractor=? ORDER BY r.id DESC LIMIT 64",
                        (corpus, *scopes, entity_id, entity_id, extractor_version()),
                    ).fetchall()
                    ordered = sorted(
                        rows, key=lambda row: -len(terms & set(normalize(row["quote"]).split()))
                    )
                    for row in ordered[:16]:
                        target = row["target"] if row["source"] == entity_id else row["source"]
                        if target in visited:
                            continue
                        edge = {
                            key: row[key]
                            for key in (
                                "point_id",
                                "version",
                                "content_hash",
                                "scope",
                                "file_id",
                                "predicate",
                                "start",
                                "end",
                                "quote",
                                "source_name",
                                "target_name",
                            )
                        }
                        edge["edge_id"] = row["id"]
                        next_path = [*path, edge]
                        score = len(terms & set(normalize(row["quote"]).split())) + 1 / hop
                        hit = {
                            "point_id": row["point_id"],
                            "path": next_path,
                            "score": score,
                            "hops": hop,
                        }
                        previous = hits.get(row["point_id"])
                        if previous is None or previous["score"] < score:
                            hits[row["point_id"]] = hit
                        next_frontier.append((target, next_path, visited | {target}))
                frontier = next_frontier
                if not frontier:
                    break
        return sorted(hits.values(), key=lambda hit: (-hit["score"], hit["hops"], hit["point_id"]))[
            :k
        ]

    def stats(self, corpus: str) -> dict:
        """Stats."""
        with self.connect(read_only=True) as connection:
            if connection.execute("PRAGMA user_version").fetchone()[0] == 0:
                return dict.fromkeys(("chunks", "mentions", "relations", "entities"), 0)
            counts = {
                "chunks": connection.execute(
                    "SELECT count(*) FROM chunks WHERE corpus=?", (corpus,)
                ).fetchone()[0]
            }
            for table in ("mentions", "relations"):
                counts[table] = connection.execute(
                    f"SELECT count(*) FROM {table} t JOIN chunks c ON c.id=t.chunk_id WHERE c.corpus=?",
                    (corpus,),
                ).fetchone()[0]
            counts["entities"] = connection.execute(
                "SELECT count(DISTINCT m.entity_id) FROM mentions m JOIN chunks c ON c.id=m.chunk_id WHERE c.corpus=?",
                (corpus,),
            ).fetchone()[0]
        return counts

    def remove_versions(
        self,
        corpus: str,
        file_id: str,
        scope: str | None = None,
        *,
        keep: str | None = None,
        remove: str | None = None,
    ):
        """Remove versions."""
        with self.connect() as connection:
            clause = "corpus=? AND file_id=?"
            params = [corpus, file_id]
            if scope is not None:
                clause += " AND scope=?"
                params.append(scope)
            if keep is not None:
                clause += " AND version<>?"
                params.append(keep)
            elif remove is not None:
                clause += " AND version=?"
                params.append(remove)
            cached = connection.execute(
                "SELECT DISTINCT content_hash,extractor FROM chunks WHERE " + clause, params
            ).fetchall()
            nodes = connection.execute(
                "SELECT DISTINCT m.entity_id FROM mentions m JOIN chunks c ON c.id=m.chunk_id "
                "WHERE c.id IN (SELECT id FROM chunks WHERE " + clause + ")",
                params,
            ).fetchall()
            connection.execute("DELETE FROM chunks WHERE " + clause, params)
            connection.executemany(
                "DELETE FROM extraction_cache WHERE hash=? AND extractor=? "
                "AND NOT EXISTS (SELECT 1 FROM chunks WHERE content_hash=? AND extractor=?)",
                [
                    (row["content_hash"], row["extractor"], row["content_hash"], row["extractor"])
                    for row in cached
                ],
            )
            connection.executemany(
                "DELETE FROM entities WHERE id=? "
                "AND NOT EXISTS (SELECT 1 FROM mentions WHERE entity_id=?) "
                "AND NOT EXISTS (SELECT 1 FROM relations WHERE source=? OR target=?)",
                [(row["entity_id"],) * 4 for row in nodes],
            )


class ScopedEntityGraph:
    """One persistent graph per physical collection and authorized library."""

    def __init__(self, path: str | Path):
        self.legacy_path = Path(path).resolve()
        self.root = self.legacy_path.with_name(self.legacy_path.name + ".scopes")
        self._thread = local()

    def _reader(self, graph: EntityGraph):
        readers = getattr(self._thread, "readers", None)
        if readers is None:
            readers = self._thread.readers = OrderedDict()
        reader = readers.pop(graph.path, None)
        if reader is None:
            reader = graph.open_connection(read_only=True)
        readers[graph.path] = reader
        if len(readers) > 8:
            _, oldest = readers.popitem(last=False)
            oldest.close()
        return reader

    def for_scope(self, corpus: str, scope: str, *, create: bool = False) -> EntityGraph | None:
        """For scope."""
        path = self.root / text_hash(corpus) / (text_hash(scope) + ".sqlite")
        if not create and not path.is_file():
            return None
        return EntityGraph(path, create=create)

    def _shards(self, corpus: str):
        return (
            EntityGraph(path, create=False)
            for path in (self.root / text_hash(corpus)).glob("*.sqlite")
        )

    def index(self, corpus: str, points: list, *, prior=None) -> dict:
        """Index."""
        by_scope = {}
        for point in points:
            scope = ((point.payload or {}).get("meta") or {}).get("collection_name")
            if isinstance(scope, str) and scope:
                by_scope.setdefault(scope, []).append(point)
        counts = dict.fromkeys(
            (
                "chunks",
                "unchanged",
                "real_extractions",
                "legacy_extractions_reused",
                "mentions",
                "relations",
            ),
            0,
        )
        for scope, scoped_points in by_scope.items():
            graph = self.for_scope(corpus, scope, create=True)
            for metric, value in graph.index(corpus, scoped_points, prior=prior).items():
                counts[metric] += value
        return counts

    def search(self, corpus: str, scopes: list[str], query: str, k: int) -> list[dict]:
        """Search."""
        names = _query_entities(query) if scopes else []
        if not names:
            return []
        terms = set(normalize(query).split())
        hits = []
        for scope in dict.fromkeys(scopes):
            graph = self.for_scope(corpus, scope)
            if graph is not None:
                hits.extend(
                    graph.search_entities(
                        corpus, [scope], names, terms, k, reader=self._reader(graph)
                    )
                )
        return sorted(hits, key=lambda hit: (-hit["score"], hit["hops"], hit["point_id"]))[:k]

    def stats(self, corpus: str) -> dict:
        """Stats."""
        counts = dict.fromkeys(("chunks", "mentions", "relations", "entities", "shards"), 0)
        for graph in self._shards(corpus):
            for metric, value in graph.stats(corpus).items():
                counts[metric] += value
            counts["shards"] += 1
        return counts

    def remove_versions(self, corpus: str, file_id: str, scope: str | None = None, **versions):
        """Remove versions."""
        if scope is None:
            shards = self._shards(corpus)
        else:
            graph = self.for_scope(corpus, scope)
            shards = [graph] if graph is not None else []
        for graph in shards:
            graph.remove_versions(corpus, file_id, scope, **versions)


def backfill(client, corpus: str, graph: ScopedEntityGraph, *, report=None) -> dict:
    """Backfill."""
    from qdrant_client import models
    from qdrant_client.http.exceptions import UnexpectedResponse

    try:
        expected = client.count(corpus, exact=True).count
    except UnexpectedResponse as exc:
        if exc.status_code != 404:
            raise
        return {"scanned": 0, "expected": 0, "new_chunks": 0}
    receipt = {
        "expected": expected,
        "scanned": 0,
        "new_chunks": 0,
        "unchanged": 0,
        "real_extractions": 0,
        "legacy_extractions_reused": 0,
    }
    offset = None
    with ExitStack() as stack:
        prior = None
        if graph.legacy_path.is_file():
            prior = stack.enter_context(
                closing(
                    sqlite3.connect(
                        graph.legacy_path.as_uri() + "?mode=ro",
                        uri=True,
                        timeout=30,
                    )
                )
            )
        while True:
            points, offset = client.scroll(
                collection_name=corpus,
                limit=128,
                offset=offset,
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
            indexed = graph.index(corpus, points, prior=prior)
            receipt["scanned"] += len(points)
            receipt["new_chunks"] += indexed["chunks"]
            receipt["unchanged"] += indexed["unchanged"]
            receipt["real_extractions"] += indexed["real_extractions"]
            receipt["legacy_extractions_reused"] += indexed["legacy_extractions_reused"]
            if report:
                report(dict(receipt))
            if offset is None:
                break
    receipt["unindexed"] = receipt["scanned"] - receipt["new_chunks"] - receipt["unchanged"]
    return receipt


def _query_entities(query: str) -> list[str]:
    if not query.strip():
        return []
    with _LOCK:
        parsed = _model(_language(query))(query, disable=["parser"])
    return list(
        dict.fromkeys(normalize(span.text) for span in parsed.ents if span.label_ in _LABELS)
    )[:16]
