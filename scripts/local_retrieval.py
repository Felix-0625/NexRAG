"""原生检索入口，首次运行时以非阻塞方式预热重排序模型。

可选的 cross-encoder 下载期间，混合检索仍然可用；
模型加载完成后，现有流水线会自动启用重排序。
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
    """包装上游 lifespan：使用 fastembed 重排序且未强制要求时，先关闭重排序，后台预热模型成功后再启用；退出时取消预热并恢复原设置。"""
    requested = settings.enable_reranking
    background = (
        requested and not settings.rerank_required and settings.reranker_provider == "fastembed"
    )
    task = None
    if background:
        settings.enable_reranking = False

    async def warmup():
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
