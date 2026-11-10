"""Warm native reranking asynchronously while hybrid retrieval remains available.

Enable the existing pipeline's reranker once its optional cross-encoder finishes loading.
"""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from app.config import settings
from app.main import app

log = logging.getLogger(__name__)
_upstream_lifespan = app.router.lifespan_context


@asynccontextmanager
async def local_lifespan(application):
    """Wrap upstream lifespan with optional reranker warmup, cancellation, and settings
    restoration.
    """
    requested = settings.enable_reranking
    background = (
        requested and not settings.rerank_required and settings.reranker_provider == "fastembed"
    )
    task = None
    if background:
        settings.enable_reranking = False

    async def warmup():
        """Load the optional reranker without blocking initial retrieval availability."""
        from app.services.reranker import _local_rerank_scores

        log.info("Warming local reranker in background; hybrid retrieval is already available.")
        try:
            await asyncio.to_thread(_local_rerank_scores, "warmup", ["warmup"])
        except Exception:
            log.exception(
                "Local reranker warmup failed; retrieval remains available without reranking."
            )
        else:
            settings.enable_reranking = True
            log.info("Local reranker ready; reranking enabled.")

    try:
        async with _upstream_lifespan(application):
            if background:
                task = asyncio.create_task(warmup())
            yield
    finally:
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        settings.enable_reranking = requested


app.router.lifespan_context = local_lifespan
