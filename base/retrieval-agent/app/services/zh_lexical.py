"""Chinese character BM25 over authorized Parent chunks, with live evidence hydration."""

import asyncio
import logging
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass
from threading import Lock

from qdrant_client import models
from qdrant_client.http.exceptions import UnexpectedResponse

from app.config import settings
from app.services import qdrant

log = logging.getLogger(__name__)
_HAN = re.compile(r"[\u3400-\u9fff]")
_WORDS = re.compile(r"[a-z0-9]+|[\u3400-\u9fff]+")
_MAX_INDEX_BYTES = 64 * 1024 * 1024


def is_chinese_query(query: str) -> bool:
    """Use the route for questions with a substantial Chinese component."""
    han = len(_HAN.findall(query))
    return han >= 2 and han * 3 >= sum(char.isalpha() for char in query)


def tokens(text: str):
    for word in _WORDS.findall(unicodedata.normalize("NFKC", text).casefold()):
        if word.isascii():
            yield word
        else:
            for width in (1, 2, 3):
                for start in range(len(word) - width + 1):
                    yield word[start : start + width]


def _parent_text(payload: dict) -> str:
    meta = payload.get("meta", {})
    return meta.get("parent_text") or meta.get("parent_content") or payload.get("content", "")


@dataclass
class _Index:
    scopes: tuple[str, ...]
    connection: sqlite3.Connection
    point_ids: list[str | int]
    expires: float


# One bounded snapshot. A lock protects both publication and reads of its SQLite connection.
_index: _Index | None = None
_lock = Lock()


def _build_index(scopes: tuple[str, ...]) -> _Index | None:
    client = qdrant.get_client()
    scope_filter = qdrant._collection_name_filter(list(scopes))
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    point_ids = []
    seen = set()
    indexed_bytes = 0
    offset = None
    try:
        connection.execute("CREATE VIRTUAL TABLE parents USING fts5(body)")
        while True:
            points, offset = client.scroll(
                collection_name=settings.qdrant_index,
                scroll_filter=scope_filter,
                limit=128,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for point in points:
                payload = point.payload or {}
                meta = payload.get("meta", {})
                text = _parent_text(payload)
                if not text or meta.get("collection_name") not in scopes:
                    continue
                key = (meta["collection_name"], meta.get("file_id"), meta.get("parent_id") or text)
                if key in seen:
                    continue
                tokenized = " ".join(tokens(text))
                if not tokenized:
                    continue
                indexed_bytes += len(tokenized.encode())
                if len(point_ids) >= settings.bm25_max_docs or indexed_bytes > _MAX_INDEX_BYTES:
                    log.warning("Chinese lexical scope exceeds the index budget")
                    connection.close()
                    return None
                seen.add(key)
                point_ids.append(point.id)
                connection.execute(
                    "INSERT INTO parents(rowid, body) VALUES (?, ?)", (len(point_ids), tokenized)
                )
            if offset is None:
                break
        connection.commit()
        expires = time.monotonic() + settings.bm25_cache_ttl_seconds
        return _Index(scopes, connection, point_ids, expires)
    except BaseException:
        connection.close()
        raise


def _search(query: str, scopes: tuple[str, ...], k: int):
    global _index
    terms = list(dict.fromkeys(tokens(query)))
    if not terms:
        return None
    match = " OR ".join(f'"{term}"' for term in terms)
    with _lock:
        if _index is None or _index.scopes != scopes or _index.expires <= time.monotonic():
            if _index is not None:
                _index.connection.close()
                _index = None
            try:
                _index = _build_index(scopes)
            except UnexpectedResponse as error:
                if error.status_code != 404:
                    raise
                return None
            except sqlite3.OperationalError:
                log.exception("Could not build Chinese lexical index; using normal retrieval")
                return None
        if _index is None:
            return None
        rows = _index.connection.execute(
            "SELECT rowid, bm25(parents) AS score FROM parents WHERE parents MATCH ? "
            "ORDER BY score, rowid LIMIT ?",
            (match, k * 2),
        )
        candidates = [(_index.point_ids[rowid - 1], -score) for rowid, score in rows]
    if not candidates:
        return None

    # The SQLite snapshot selects IDs only. Read evidence again under the current
    # Qdrant scope so a deleted or revoked point cannot be served from the index.
    scope_filter = qdrant._collection_name_filter(list(scopes))
    query_filter = models.Filter(
        must=[scope_filter, models.HasIdCondition(has_id=[point_id for point_id, _ in candidates])]
    )
    points, _ = qdrant.get_client().scroll(
        collection_name=settings.qdrant_index,
        scroll_filter=query_filter,
        limit=len(candidates),
        with_payload=True,
        with_vectors=False,
    )
    payloads = {point.id: point.payload or {} for point in points}
    hits = []
    for point_id, score in candidates:
        payload = payloads.get(point_id, {})
        text = _parent_text(payload)
        meta = payload.get("meta", {})
        if not text or meta.get("collection_name") not in scopes:
            continue
        metadata = {
            key: value
            for key, value in meta.items()
            if key not in {"parent_text", "parent_content", "context_window"}
        }
        metadata["retrieval_strategy"] = "zh_parent_bm25"
        hits.append((text, metadata, score / (1 + score)))
        if len(hits) == k:
            break
    if not hits:
        return None
    texts, metadatas, scores = zip(*hits, strict=True)
    return list(texts), list(metadatas), list(scores)


async def search_chinese_parents(query: str, collection_names: list[str], k: int):
    return await asyncio.to_thread(_search, query, tuple(sorted(set(collection_names))), k)
