"""Restore must not publish between indexing and background file association."""

import ast
import asyncio
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route


def test_writer_covers_background_completion_and_allows_parallel_readers(tmp_path, monkeypatch):
    tree = ast.parse(
        Path("webui/backend/open_webui/utils/nexrag_knowledge_writes.py").read_text("utf8")
    )
    tree.body = [
        node
        for node in tree.body
        if not isinstance(node, ast.ImportFrom) or node.module != "open_webui.env"
    ]
    ns = {"DATA_DIR": tmp_path}
    exec(compile(tree, "<shipped-writer-coordinator>", "exec"), ns)
    monkeypatch.setenv("NEXRAG_WORKSPACE", "true")

    async def run():
        """Run this scenario in an isolated asynchronous context."""
        indexing, finish_link, restored = asyncio.Event(), asyncio.Event(), asyncio.Event()
        events = []

        async def background():
            """Provide background test data or behavior for this scenario."""
            events.append("index-ready")
            indexing.set()
            await finish_link.wait()
            events.append("file-linked")

        async def upload(request):
            """Provide upload test data or behavior for this scenario."""
            return JSONResponse({"status": True}, background=BackgroundTask(background))

        async def restore(request):
            """Provide restore test data or behavior for this scenario."""
            events.append("restore-published")
            restored.set()
            return JSONResponse({"status": True})

        async def read(request):
            """Provide read test data or behavior for this scenario."""
            return JSONResponse({"visible": "old-version"})

        async def passthrough(request, call_next):
            """Provide passthrough test data or behavior for this scenario."""
            return await call_next(request)

        app = Starlette(
            routes=[
                Route("/api/v1/files/", upload, methods=["POST"]),
                Route("/api/v1/nexrag/knowledge/kb/versions/v1/restore", restore, methods=["POST"]),
                Route("/api/v1/knowledge/kb", read),
            ]
        )
        app = ns["KnowledgeWriteCoordinator"](BaseHTTPMiddleware(app, dispatch=passthrough))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            first = asyncio.create_task(client.post("/api/v1/files/"))
            await asyncio.wait_for(indexing.wait(), 2)
            second = asyncio.create_task(
                client.post("/api/v1/nexrag/knowledge/kb/versions/v1/restore")
            )
            try:
                assert (await client.get("/api/v1/knowledge/kb")).json()["visible"] == "old-version"
                await asyncio.sleep(0.1)
                assert not second.done() and not restored.is_set()
            finally:
                finish_link.set()
                responses = await asyncio.wait_for(asyncio.gather(first, second), 3)
            assert [item.status_code for item in responses] == [200, 200]
            assert events == ["index-ready", "file-linked", "restore-published"]

    asyncio.run(run())
