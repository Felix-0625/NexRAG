"""Linear search pipeline + the entrypoint that routes to linear/agentic.

Phase 3 simplification: ``vector_search`` now hits a single physical Qdrant
collection (filtered by ``meta.collection_name IN (collection_names)``), so
the per-collection ``asyncio.gather`` is gone — one query, one result set.

When ``ENABLE_HYBRID_SEARCH`` is on:

  * If the target collection has sparse vectors, the sparse query embedder
    runs and Qdrant fuses dense + sparse server-side via RRF — no client-side
    BM25 path executes.
  * If the collection has no sparse vectors, BM25 RRF runs client-side over
    the same collection scope.
"""

import asyncio
import logging
import time
from collections.abc import Iterator
from itertools import chain, islice

from qdrant_client.http.models import SparseVector

from app import metrics
from app.config import settings
from app.log_utils import sanitize_for_log
from app.models import ChatMessage, RetrievalResult, SearchRequest, SearchResponse
from app.services import embedding, qdrant, sparse_embedding
from app.services.bm25 import bm25_search, reciprocal_rank_fusion
from app.services.document_scope import scoped_evidence_query
from app.services.five_channel import search_five_channels
from app.services.reranker import rerank
from app.services.title_routing import explicit_title_order
from app.services.zh_lexical import is_chinese_query, search_chinese_parents

log = logging.getLogger(__name__)


def extract_queries_from_messages(messages: list[ChatMessage]) -> list[str]:
    """Use the last non-empty user message as the query."""
    for message in reversed(messages):
        if message.role == "user" and message.content.strip():
            return [message.content.strip()]
    return []


def restore_parent_context(
    texts: list[str],
    metadatas: list[dict],
    distances: list[float],
    *,
    expand_parent: bool = True,
) -> tuple[list[str], list[dict], list[float]]:
    """Return the parent text after ranking its matching child fragments.

    The child score remains attached to the result. Remove the duplicate
    ``parent_content`` field from returned metadata while retaining
    ``parent_id`` and source details for citations. Legacy points without a
    parent payload keep their original chunk content. Matched-child mode keeps
    the indexed fragment verbatim and removes parent text from its metadata.
    """
    restored_texts: list[str] = []
    restored_meta: list[dict] = []
    for text, metadata in zip(texts, metadatas, strict=True):
        meta = dict(metadata)
        legacy_parent = meta.pop("parent_content", None)
        parent = meta.pop("parent_text", None) or legacy_parent
        window = meta.pop("context_window", None)
        if settings.retrieval_context_mode == "window":
            start, end = meta.get("context_window_start"), meta.get("context_window_end")
            valid_window = (
                isinstance(window, str)
                and bool(text)
                and text in window
                and isinstance(start, int)
                and isinstance(end, int)
                and 0 <= start < end
                and end - start == len(window)
                and bool(meta.get("source_id"))
            )
            restored_texts.append(window if valid_window else text)
            if not valid_window:
                # Old indexes remain readable without inventing a surrounding
                # passage. Reingestion supplies verified source-bound windows.
                meta.pop("context_window_start", None)
                meta.pop("context_window_end", None)
                meta["context_window_missing"] = True
        else:
            restored_texts.append(
                parent if expand_parent and isinstance(parent, str) and parent.strip() else text
            )
        restored_meta.append(meta)
    return restored_texts, restored_meta, distances


async def embed_dense_and_sparse(
    queries: list[str],
) -> tuple[list[list[float]], list[SparseVector | None], bool]:
    """Embed queries for retrieval. Returns ``(dense, sparse, use_native_hybrid)``.

    Probes :func:`qdrant.has_sparse_vectors` once. Sparse embedding only runs
    when hybrid is enabled *and* the configured collection actually carries a
    ``text-sparse`` named vector; otherwise the sparse list is all-``None``
    and downstream callers fall back to dense-only retrieval (with optional
    client-side BM25 RRF if hybrid is on).
    """
    if not queries:
        return [], [], False

    async with metrics.time_stage("embed_dense"):
        vectors = await embedding.embed_queries(queries)
    # has_sparse_vectors hits Qdrant (sync client) until its answer is cached —
    # probe in a worker thread to keep the event loop free.
    use_native_hybrid = settings.enable_hybrid_search and await asyncio.to_thread(
        qdrant.has_sparse_vectors
    )
    if use_native_hybrid:
        async with metrics.time_stage("embed_sparse"):
            sparse_vectors: list[SparseVector | None] = await sparse_embedding.embed_queries(
                queries
            )
    else:
        sparse_vectors = [None] * len(queries)

    # Record which fusion path this search will take so operators can see
    # whether native server-side RRF, the client-side BM25 fallback, or
    # dense-only retrieval is actually running.
    if use_native_hybrid:
        path = "native_sparse"
    elif settings.enable_hybrid_search:
        path = "bm25_fallback"
    else:
        path = "dense_only"
    metrics.hybrid_path_total.labels(path=path).inc()

    return vectors, sparse_vectors, use_native_hybrid


async def retrieve_one_query(
    query_text: str,
    query_vector: list[float],
    sparse_vec: SparseVector | None,
    collection_names: list[str],
    fetch_k: int,
    use_native_hybrid: bool,
    *,
    rerank_k: int | None,
    dedup_before_limit: bool = False,
    expand_parent: bool = True,
) -> tuple[list[str], list[dict], list[float]]:
    """Retrieve, optionally fuse/rerank, then restore context without double fusion.

    ``rerank_k=None`` keeps raw vector results for the agent fallback path.
    """
    async with metrics.time_stage("qdrant"):
        result = None
        if (
            (settings.enable_five_channel_retrieval or settings.enable_graph_rag)
            and use_native_hybrid
            and sparse_vec is not None
            and settings.hybrid_fusion == "rrf"
        ):
            result = await search_five_channels(
                query_text, collection_names, query_vector, sparse_vec, fetch_k
            )
        if result is None:
            result = await qdrant.vector_search(
                collection_names, query_vector, sparse_vec, fetch_k
            )
    texts, metadatas, distances = result.texts, result.metadatas, result.distances
    metrics.candidates_fetched.observe(len(texts))
    log.debug(
        "retrieve_one_query q=%s candidates=%d top_scores=%s",
        sanitize_for_log(query_text),
        len(texts),
        [round(d, 4) for d in distances[:3]],
    )

    if settings.enable_hybrid_search and not use_native_hybrid and texts:
        texts, metadatas, distances = await _fuse_bm25(
            query_text, collection_names, fetch_k, texts, metadatas, distances
        )

    if settings.enable_reranking and texts and rerank_k is not None:
        # Keep the full eligible ordering when siblings can share a result slot.
        keep_full_order = dedup_before_limit or settings.enable_title_routing
        ranked = None
        scoped = None
        if use_native_hybrid and settings.enable_document_scoped_retrieval:
            scoped = scoped_evidence_query(query_text, metadatas)
            if scoped is not None and scoped[0][0] not in collection_names:
                scoped = None
        if scoped is not None:
            document, question = scoped
            async with metrics.time_stage("document_scoped_retrieval"):
                dense, sparse, _ = await embed_dense_and_sparse([question])
                evidence = await qdrant.vector_search(
                    collection_names, dense[0], sparse[0], fetch_k, document=document
                )
            if evidence.texts:
                evidence_meta = [
                    dict(meta, retrieval_channels=["document_scoped_hybrid"])
                    for meta in evidence.metadatas
                ]
                async with metrics.time_stage("rerank"):
                    ranked = await rerank(
                        question,
                        evidence.texts,
                        evidence_meta,
                        len(evidence.texts) if keep_full_order else rerank_k,
                    )
        if ranked is None or not ranked[0]:
            async with metrics.time_stage("rerank"):
                ranked = await rerank(
                    query_text, texts, metadatas, len(texts) if keep_full_order else rerank_k
                )
        texts, metadatas, distances = ranked
        if settings.enable_title_routing:
            order = explicit_title_order(query_text, metadatas)
            texts = [texts[index] for index in order]
            metadatas = [metadatas[index] for index in order]
            distances = [distances[index] for index in order]
        if dedup_before_limit:
            ranked = RetrievalResult(texts=texts, metadatas=metadatas, distances=distances)
            texts, metadatas, distances = dedup_topk([ranked], rerank_k)
        elif settings.enable_title_routing:
            texts, metadatas, distances = (
                texts[:rerank_k],
                metadatas[:rerank_k],
                distances[:rerank_k],
            )

    return restore_parent_context(texts, metadatas, distances, expand_parent=expand_parent)


def interleave_dedup(results: list[RetrievalResult]) -> Iterator[tuple[str, dict, float]]:
    """Yield ``(text, meta, dist)`` round-robin across per-query result sets.

    Each result set is already rerank-sorted, so position 0 is each query's
    *best* hit. Interleaving by position gives every query fair representation
    instead of letting the first query monopolise a small ``k`` — the bug where
    one query's results filled the budget and a relevant chunk from a later
    query was dropped before it was ever considered. Within a position, the
    higher-scored candidate is yielded first (score is the tiebreak). Duplicate
    parent identities (or legacy texts) are emitted once, keeping the first
    (highest-position, highest-score) occurrence. A single result set is
    simply deduped in order.

    Order is intentionally *not* re-sorted globally by score: a relevant chunk
    that the reranker under-scores must stay near the front so a downstream
    top-k / threshold can't silently re-drop it.
    """
    seen: set[tuple] = set()
    max_len = max((len(r.texts) for r in results), default=0)
    for pos in range(max_len):
        # Every query's candidate at this rank, best score first.
        row = [
            (r.distances[pos], r.texts[pos], r.metadatas[pos])
            for r in results
            if pos < len(r.texts)
        ]
        row.sort(key=lambda x: x[0], reverse=True)
        for dist, text, meta in row:
            # Prefer a persisted parent identity over text equality. Several
            # leaf hits from one parent should return one context; two source
            # files may legitimately contain the same sentence.
            parent_id = meta.get("parent_id")
            if settings.retrieval_context_mode == "window":
                start, end = meta.get("context_window_start"), meta.get("context_window_end")
                window_id = (
                    (start, end)
                    if isinstance(start, int) and isinstance(end, int) and 0 <= start < end
                    else ("child", text)
                )
                key = (
                    "window",
                    meta.get("collection_name"),
                    meta.get("file_id"),
                    meta.get("ingest_version"),
                    window_id,
                )
            else:
                key = (
                    ("parent", meta.get("collection_name"), meta.get("file_id"), parent_id)
                    if parent_id
                    else ("text", text)
                )
            if key not in seen:
                seen.add(key)
                yield text, meta, dist


def dedup_topk(
    results: list[RetrievalResult], k: int
) -> tuple[list[str], list[dict], list[float]]:
    """First ``k`` of :func:`interleave_dedup`, as parallel lists."""
    rows = list(islice(interleave_dedup(results), k))
    return [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows]


async def linear_search(request: SearchRequest) -> SearchResponse:
    """Traditional linear pipeline:

    1. Resolve queries (LLM generation → explicit ``queries`` → last user message)
    2. Embed (dense, plus sparse when hybrid+sparse-capable)
    3. Single Qdrant query per dense query, scoped to all collection_names
    4. Optional client-side BM25 RRF (only when hybrid is on AND no sparse)
    5. Optional cross-encoder reranking
    6. Restore parent context, deduplicate parent identities, limit to k

    Queries run concurrently — each query's retrieve/fuse/rerank chain is
    independent, so per-query wall-clock doesn't stack up.
    """
    k = request.k
    multiplier = max(
        settings.initial_retrieval_multiplier if settings.enable_reranking else 1,
        settings.parent_candidate_multiplier,
    )
    fetch_k = settings.retrieval_candidate_limit or k * multiplier

    queries = await _resolve_queries(request)
    if not queries:
        return SearchResponse(documents=[], metadatas=[], distances=[])

    retrieved: list[tuple[list[str], list[dict], list[float]] | None] = [None] * len(queries)
    if (
        settings.enable_zh_lexical_retrieval
        and settings.retrieval_context_mode == "parent"
        and not settings.enable_graph_rag
    ):
        chinese = [index for index, query in enumerate(queries) if is_chinese_query(query)]
        async with metrics.time_stage("zh_lexical"):
            results = await asyncio.gather(
                *(
                    search_chinese_parents(queries[index], request.collection_names, k)
                    for index in chinese
                )
            )
        for index, result in zip(chinese, results, strict=True):
            retrieved[index] = result

    remaining = [index for index, result in enumerate(retrieved) if result is None]
    native_queries = [queries[index] for index in remaining]
    vectors, sparse_vectors, use_native_hybrid = await embed_dense_and_sparse(native_queries)

    native_results = await asyncio.gather(
        *(
            retrieve_one_query(
                query_text,
                query_vector,
                sparse_vec,
                request.collection_names,
                fetch_k,
                use_native_hybrid,
                rerank_k=min(k, settings.rerank_top_k),
                dedup_before_limit=settings.rerank_dedup_before_limit,
                expand_parent=settings.retrieval_context_mode == "parent",
            )
            for query_text, query_vector, sparse_vec in zip(
                native_queries, vectors, sparse_vectors, strict=True
            )
        )
    )
    for index, result in zip(remaining, native_results, strict=True):
        retrieved[index] = result
    per_query = []
    for result in retrieved:
        assert result is not None
        texts, metadatas, distances = result
        per_query.append(
            dedup_topk([RetrievalResult(texts=texts, metadatas=metadatas, distances=distances)], k)
        )

    return SearchResponse(
        documents=[texts for texts, _, _ in per_query],
        metadatas=[metas for _, metas, _ in per_query],
        distances=[dists for _, _, dists in per_query],
    )


async def _resolve_queries(request: SearchRequest) -> list[str]:
    """Pick query strings from request, preferring LLM generation when available."""
    queries: list[str] | None = None

    if request.messages and settings.enable_query_generation:
        from app.services.query_generation import generate_queries_from_messages

        async with metrics.time_stage("query_generation"):
            queries = await generate_queries_from_messages(
                request.messages,
                template_override=request.retrieval_query_generation_prompt_template,
            )

    if not queries:
        queries = request.queries or []

    if not queries and request.messages:
        queries = extract_queries_from_messages(request.messages)
        log.info("Extracted %d queries from %d messages", len(queries), len(request.messages))
        log.debug("Extracted queries: %s", sanitize_for_log(queries))

    return queries


async def search(request: SearchRequest) -> SearchResponse:
    """Main entry point: routes to agentic or linear pipeline based on config.

    Single owner of request-level metrics: ``search_requests_total`` (success
    and error), ``search_duration_seconds`` and ``results_returned`` are all
    recorded here so there's no double-counting and the ``pipeline`` label is
    consistent across both.
    """
    pipeline = "agentic" if settings.enable_agentic_rag else "linear"
    start = time.perf_counter()
    try:
        if settings.enable_agentic_rag:
            from app.services.agent import agentic_search

            log.info("Using agentic search pipeline")
            response = await agentic_search(request)
        else:
            log.info("Using linear search pipeline")
            response = await linear_search(request)
    except Exception as exc:
        metrics.search_requests_total.labels(
            pipeline=pipeline, outcome="error", code=type(exc).__name__
        ).inc()
        raise
    else:
        metrics.search_requests_total.labels(
            pipeline=pipeline, outcome="success", code="none"
        ).inc()
        metrics.results_returned.observe(sum(len(d) for d in response.documents))
        return response
    finally:
        metrics.search_duration_seconds.labels(pipeline=pipeline).observe(
            time.perf_counter() - start
        )


async def _fuse_bm25(query_text, collection_names, fetch_k, texts, metadatas, distances):
    async with metrics.time_stage("bm25"):
        bm25_results = await bm25_search(collection_names, query_text, fetch_k)
        fused = reciprocal_rank_fusion(
            texts, [text for text, _, _ in bm25_results], settings.hybrid_bm25_weight
        )
        # Vector metadata wins; BM25 triples fill in provenance for docs
        # only BM25 surfaced (they were never in the vector result set).
        text_to_meta: dict[str, dict] = {}
        for text, meta in chain(
            zip(texts, metadatas, strict=True), ((t, m) for t, _, m in bm25_results)
        ):
            text_to_meta.setdefault(text, meta)
        texts = [text for text, _ in fused]
        distances = [score for _, score in fused]
        metadatas = [text_to_meta.get(t, {}) for t in texts]
    return texts, metadatas, distances
