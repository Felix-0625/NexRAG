import asyncio
import logging
import math
import threading

import httpx

from app import metrics
from app.config import settings
from app.http_client import bearer, get_client

log = logging.getLogger(__name__)
_local_model = None
_local_model_lock = threading.Lock()
# Attention tensors grow with the longest pair in each batch. Keep the default
# within the memory budget of a local workstation, even for many long chunks.
_LOCAL_BATCH_SIZE = 4


def _score_local_batches(model, query: str, documents: list[str]) -> list[float]:
    from onnxruntime.capi.onnxruntime_pybind11_state import Fail, RuntimeException

    scores: list[float] = []
    batch_size = _LOCAL_BATCH_SIZE
    while len(scores) < len(documents):
        batch = documents[len(scores) : len(scores) + batch_size]
        try:
            batch_scores = [float(score) for score in model.rerank(query, batch)]
        except (Fail, RuntimeException, RuntimeError, MemoryError) as exc:
            message = str(exc).lower()
            allocation_failed = (
                isinstance(exc, MemoryError)
                or "out of memory" in message
                or ("available memory" in message and "requested bytes" in message)
                or "failed to allocate memory" in message
            )
            if not allocation_failed or len(batch) == 1:
                raise RuntimeError("Local cross-encoder inference failed") from exc
            batch_size = max(1, len(batch) // 2)
            log.warning("Local reranker allocation failed; retrying in batches of %d", batch_size)
            continue
        if len(batch_scores) != len(batch):
            raise ValueError("Local cross-encoder returned an incomplete batch of scores")
        scores.extend(batch_scores)
    return scores


def _local_rerank_scores(query: str, documents: list[str]) -> list[float]:
    """Run the supported FastEmbed cross-encoder without blocking the event loop.

    A lock covers model loading and inference so concurrent local searches do
    not load multiple model copies or oversubscribe CPU threads.
    """
    global _local_model
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    with _local_model_lock:
        if _local_model is None:
            _local_model = TextCrossEncoder(
                model_name=settings.reranker_model,
                cache_dir=settings.fastembed_cache_dir or None,
                threads=settings.reranker_threads or None,
            )
        raw_scores = _score_local_batches(_local_model, query, documents)
        if settings.reranker_model.startswith("BAAI/bge-reranker"):
            # BGE emits unbounded logits. Sigmoid keeps their order while
            # making the returned relevance score interpretable on 0..1.
            return [
                1 / (1 + math.exp(-score))
                if score >= 0
                else math.exp(score) / (1 + math.exp(score))
                for score in raw_scores
            ]
        return raw_scores


async def rerank(
    query: str,
    documents: list[str],
    metadatas: list[dict],
    k: int,
) -> tuple[list[str], list[dict], list[float]]:
    """
    Rerank documents via the configured HTTP API or local FastEmbed model.

    ``documents`` must arrive in retrieval-score order (callers pass them straight
    from vector/hybrid search), so the input index *is* the retrieval rank. With
    ``RERANK_FUSION=rrf`` (default) the final order is a Reciprocal Rank Fusion of
    the retrieval ranking and the cross-encoder ranking — so a strong dense hit the
    cross-encoder underranks isn't buried. ``replace`` restores cross-encoder-only
    ordering. Either way the returned score is the cross-encoder relevance score
    (only the ordering changes), limited to k. Falls back to unranked results
    (truncated to k) on any HTTP/transport error (including timeouts) or a
    malformed response body.
    """
    if not documents:
        return [], [], []

    try:
        inputs = documents
        if settings.enable_filename_retrieval:
            # Add the upload title to the model input while keeping returned
            # evidence verbatim. Older vectors may lack the metadata.
            inputs = []
            for text, meta in zip(documents, metadatas, strict=True):
                title = meta.get("search_title")
                inputs.append(f"{title}\n{text}" if isinstance(title, str) and title else text)
        if settings.reranker_provider == "fastembed":
            scores = await asyncio.to_thread(_local_rerank_scores, query, inputs)
        else:
            url = f"{settings.reranker_api_base_url.rstrip('/')}/v1/rerank"
            payload = {
                "model": settings.reranker_model,
                "query": query,
                "documents": inputs,
                "top_n": len(documents),  # get scores for all, we sort ourselves
            }
            resp = await get_client().post(
                url, json=payload, headers=bearer(settings.reranker_api_key)
            )
            resp.raise_for_status()
            data = resp.json()
            # Response format: {"results": [{"index": 0, "relevance_score": 0.9}, ...]}
            results = sorted(data["results"], key=lambda x: x["index"])
            if [result["index"] for result in results] != list(range(len(documents))):
                raise ValueError(
                    "Reranker response must contain every candidate index exactly once"
                )
            scores = [r["relevance_score"] for r in results]
        if len(scores) != len(documents):
            raise ValueError(
                f"reranker returned {len(scores)} scores for {len(documents)} documents"
            )
        if any(
            not isinstance(score, (int, float)) or not math.isfinite(score) for score in scores
        ):
            raise ValueError("Reranker returned a non-finite or non-numeric score")
    except (httpx.HTTPError, KeyError, TypeError, ValueError, RuntimeError, OSError) as exc:
        # httpx.HTTPError covers status, connect, timeout and protocol errors;
        # the rest classify a malformed response body. Either way: fail open.
        metrics.reranker_failures_total.inc()
        if settings.rerank_required:
            raise RuntimeError(
                "Required document reranking is unavailable; no unverified fallback was returned"
            ) from exc
        log.warning("Reranker request failed (%s), returning unranked results", exc)
        return documents[:k], metadatas[:k], [0.0] * min(len(documents), k)

    n = len(documents)

    if settings.rerank_fusion == "rrf":
        # Reciprocal Rank Fusion: input index = retrieval rank, cross-encoder
        # order = rerank rank. Fusing the two stops the cross-encoder from
        # burying a strong dense/hybrid hit it happens to underrank. Stable sort
        # ⇒ retrieval order breaks fused-score ties.
        rf = settings.rerank_rrf_k
        rerank_order = sorted(range(n), key=lambda i: scores[i], reverse=True)
        rerank_rank = {idx: pos for pos, idx in enumerate(rerank_order)}
        order = sorted(
            range(n),
            key=lambda i: 1.0 / (rf + i) + 1.0 / (rf + rerank_rank[i]),
            reverse=True,
        )[:k]
    else:  # "replace" — cross-encoder score only (older behaviour)
        order = sorted(range(n), key=lambda i: scores[i], reverse=True)[:k]

    # Return the cross-encoder relevance score (meaning unchanged downstream);
    # only the ordering reflects the fusion.
    eligible = [i for i in order if scores[i] >= settings.rerank_min_score]
    texts = [documents[i] for i in eligible]
    metas = [metadatas[i] for i in eligible]
    dists = [scores[i] for i in eligible]
    return texts, metas, dists
