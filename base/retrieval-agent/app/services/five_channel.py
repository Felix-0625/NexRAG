"""Scoped dense, sparse, body, title and document-structure candidate retrieval."""

import asyncio
import logging
import re
import time
import unicodedata
from collections.abc import Sequence
from itertools import islice

from qdrant_client import models
from qdrant_client.http.exceptions import UnexpectedResponse

from app import metrics
from app.config import settings
from app.models import RetrievalResult
from app.services import qdrant
from app.services.document_scope import consensus_document, document_identity

log = logging.getLogger(__name__)
_BODY = "content"
_TITLE = "meta.filename"
_HEADING = "meta.headers_breadcrumb"
_PARENT = "meta.parent_split_id"
_FIELD_TYPES = {
    _BODY: models.PayloadSchemaType.TEXT,
    _TITLE: models.PayloadSchemaType.TEXT,
    _HEADING: models.PayloadSchemaType.TEXT,
    _PARENT: models.PayloadSchemaType.INTEGER,
}
_WORDS = re.compile(r"[^\W_]+", re.UNICODE)
_HAN = re.compile(r"[\u3400-\u9fff]+")
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "with",
    }
)
_capability = (0.0, False)


def _indexes_ready() -> bool:
    global _capability
    now = time.monotonic()
    until, ready = _capability
    if now < until:
        return ready
    try:
        schema = qdrant.get_client().get_collection(settings.qdrant_index).payload_schema
    except UnexpectedResponse as exc:
        if exc.status_code != 404:
            raise
        return False  # First ingest creates both the collection and its indexes.
    ready = all(
        field in schema and schema[field].data_type == kind
        for field, kind in _FIELD_TYPES.items()
        if field != _BODY or settings.enable_body_lexical_retrieval
    )
    _capability = (now + (30 if ready else 2), ready)
    return ready


def _words(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return [word for word in _WORDS.findall(normalized) if word not in _STOPWORDS]


def _terms(words: list[str]) -> set[str]:
    terms = set(words)
    for word in words:
        for han in _HAN.findall(word):
            terms.update(han[i : i + 2] for i in range(len(han) - 1))
    return terms


def _meta(point: models.ScoredPoint) -> dict:
    return (point.payload or {}).get("meta", {})


def _body(point: models.ScoredPoint) -> str:
    return (point.payload or {}).get(_BODY, "")


def _scoped(points: list[models.ScoredPoint], scopes: set[str]) -> list[models.ScoredPoint]:
    return [
        point for point in points if _meta(point).get("collection_name") in scopes and _body(point)
    ]


def _rank_text(
    points: list[models.ScoredPoint], terms: set[str], field: str, limit: int
) -> list[models.ScoredPoint]:
    ranked = []
    for point in points:
        text = (
            _body(point) if field == _BODY else _meta(point).get(field.removeprefix("meta."), "")
        )
        if not isinstance(text, str):
            continue
        overlap = len(terms & _terms(_words(text)))
        if overlap:
            body_overlap = 0 if field == _BODY else len(terms & _terms(_words(_body(point))))
            ranked.append((-overlap, -body_overlap, str(point.id), point))
    ranked.sort(key=lambda row: row[:3])
    return [row[3] for row in ranked[:limit]]


def _text_request(scope: models.Filter, field: str, query: str, limit: int) -> models.QueryRequest:
    return models.QueryRequest(
        filter=models.Filter(
            must=[
                scope,
                models.FieldCondition(key=field, match=models.MatchTextAny(text_any=query)),
            ]
        ),
        limit=limit,
        with_payload=True,
    )


def _rrf(lanes: Sequence[tuple[str, float, Sequence[models.ScoredPoint | models.Record]]]):
    hits: dict = {}
    for name, weight, points in lanes:
        for rank, point in enumerate(points):
            hit, score, channels = hits.get(point.id, (point, 0.0, ()))
            if name == "entity_graph":
                # Graph candidates were hydrated after path verification. Keep
                # their provenance even when a vector lane found the same ID.
                hit = point
            hits[point.id] = (
                hit,
                score + weight / (settings.hybrid_rrf_k + rank),
                (*channels, name),
            )
    return sorted(hits.values(), key=lambda hit: (-hit[1], str(hit[0].id)))


def _reserve_document_candidates(hits, k: int):
    """Keep the broad head and reserve tail slots for sections of a resolved document."""
    broad = hits[:k]
    if len(hits) <= k or k < 6:
        return broad
    document = consensus_document([_meta(point) for point, _, _ in broad[:3]])
    if document is None:
        return broad

    head = broad[: k // 2]
    seen = set()
    for point, _, _ in head:
        meta = _meta(point)
        parent = meta.get("parent_id")
        if document_identity(meta) == document and isinstance(parent, str):
            seen.add(parent)
    reserve = []
    reserved_ids = set()
    for hit in hits[len(head) :]:
        point = hit[0]
        meta = _meta(point)
        parent = meta.get("parent_id")
        if (
            document_identity(meta) != document
            or not isinstance(parent, str)
            or not parent
            or parent in seen
        ):
            continue
        reserve.append(hit)
        reserved_ids.add(point.id)
        seen.add(parent)
        if len(head) + len(reserve) == k:
            break

    if reserved_ids.issubset(point.id for point, _, _ in broad):
        return broad
    rest = (hit for hit in broad[len(head) :] if hit[0].id not in reserved_ids)
    return [*head, *reserve, *islice(rest, k - len(head) - len(reserve))]


def _graph_requests(seeds, dense: list[float], limit: int):
    """Follow Document-to-section and adjacent-Parent edges in one file version."""
    requests = []
    seen = set()
    for point, _, _ in seeds:
        meta = _meta(point)
        identity = document_identity(meta)
        index = meta.get("parent_split_id")
        if identity is None or type(index) is not int:
            continue
        if identity in seen:
            continue
        seen.add(identity)
        exact: list[models.Condition] = [
            models.FieldCondition(key=key, match=models.MatchValue(value=value))
            for key, value in zip(
                ("meta.collection_name", "meta.file_id", "meta.ingest_version"),
                identity,
                strict=True,
            )
        ]
        requests.append(
            models.QueryRequest(
                query=dense,
                using=qdrant.DENSE_VECTOR_NAME,
                filter=models.Filter(must=exact),
                limit=limit,
                with_payload=True,
            )
        )
        requests.append(
            models.QueryRequest(
                filter=models.Filter(
                    must=[
                        *exact,
                        models.FieldCondition(
                            key=_PARENT, range=models.Range(gte=max(index - 1, 0), lte=index + 1)
                        ),
                    ],
                    must_not=[
                        models.FieldCondition(key=_PARENT, match=models.MatchValue(value=index))
                    ],
                ),
                limit=limit,
                with_payload=True,
            )
        )
        if len(seen) == 2:
            break
    return requests


def _query(
    query: str,
    collection_names: list[str],
    dense: list[float],
    sparse: models.SparseVector,
    k: int,
) -> RetrievalResult | None:
    if not _indexes_ready():
        return None
    client = qdrant.get_client()
    scope = qdrant._collection_name_filter(collection_names)
    scopes = set(collection_names)
    words = list(dict.fromkeys(_words(query)))[:24]
    terms = _terms(words)
    limit = min(k, 32)
    requests = [
        models.QueryRequest(
            query=dense,
            using=qdrant.DENSE_VECTOR_NAME,
            filter=scope,
            limit=k * 2,
            with_payload=True,
        ),
        models.QueryRequest(
            query=sparse,
            using=qdrant.SPARSE_VECTOR_NAME,
            filter=scope,
            limit=k * 2,
            with_payload=True,
        ),
    ]
    text_fields = (
        (_BODY, _TITLE, _HEADING) if settings.enable_body_lexical_retrieval else (_TITLE, _HEADING)
    )
    if words:
        requests.extend(
            _text_request(scope, field, " ".join(words), min(limit * 4, 128))
            for field in text_fields
        )
    raw = client.query_batch_points(collection_name=settings.qdrant_index, requests=requests)
    dense_hits, sparse_hits, *text_hits = (_scoped(row.points, scopes) for row in raw)
    ranked_text = {
        field: _rank_text(points, terms, field, limit)
        for field, points in zip(text_fields if words else (), text_hits, strict=True)
    }
    lanes: list[tuple[str, float, Sequence[models.ScoredPoint | models.Record]]] = [
        ("dense", settings.hybrid_dense_weight, dense_hits),
        ("sparse", settings.hybrid_sparse_weight, sparse_hits),
    ]
    if settings.enable_body_lexical_retrieval:
        lanes.append(("body_lexical", 0.7, ranked_text.get(_BODY, [])))
    lanes.append(("title_filename", 0.8, ranked_text.get(_TITLE, [])))
    seeds = _rrf(lanes[:2])
    graph_requests = _graph_requests(seeds, dense, min(limit, 16))
    neighbors = []
    if graph_requests:
        rows = client.query_batch_points(
            collection_name=settings.qdrant_index, requests=graph_requests
        )
        # A document-scoped vector query ranks its sections before the optional
        # adjacent context. Keep that query order through the structure lane.
        neighbors = [point for row in rows for point in _scoped(row.points, scopes)]
    # Keep a point only once within this lane even when reached by both graph seeds.
    structural = {point.id: point for point in [*ranked_text.get(_HEADING, []), *neighbors]}
    lanes.append(("document_structure", 0.45, list(islice(structural.values(), limit))))
    if settings.enable_graph_rag:
        from app.services.entity_graph import search as graph_search

        lanes.append(("entity_graph", 0.65, graph_search(query, collection_names, limit)))
    for name, _, points in lanes:
        metrics.retrieval_channel_candidates.labels(channel=name).observe(len(points))
    log.info("Retrieval channel candidates: %s", {name: len(points) for name, _, points in lanes})
    ranked = _rrf(lanes)
    hits = (
        _reserve_document_candidates(ranked, k)
        if settings.enable_document_candidate_reserve
        else ranked[:k]
    )
    max_score = sum(weight for _, weight, _ in lanes) / settings.hybrid_rrf_k
    return RetrievalResult(
        texts=[_body(point) for point, _, _ in hits],
        metadatas=[
            dict(_meta(point), retrieval_channels=list(channels)) for point, _, channels in hits
        ],
        distances=[score / max_score for _, score, _ in hits],
    )


async def search_five_channels(
    query: str,
    collection_names: list[str],
    dense: list[float],
    sparse: models.SparseVector,
    k: int,
) -> RetrievalResult | None:
    try:
        return await asyncio.to_thread(_query, query, collection_names, dense, sparse, k)
    except UnexpectedResponse as exc:
        if exc.status_code not in {400, 404, 422}:
            raise
        log.warning(
            "Five-channel query unavailable (Qdrant HTTP %d); using native hybrid", exc.status_code
        )
        return None
