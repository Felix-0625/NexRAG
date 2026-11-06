"""对照 AarhusAI 的 /search 通信约定检查 NexRAG 适配器。"""

import asyncio
import json

import httpx
from pydantic_ai.models.test import TestModel

from src.agent import core_tools
from src.agent.agent import AgentDeps, AgenticRunner, UserContext
from src.agent.tools import search_knowledge


def test_knowledge_search_preserves_source_and_scores():
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search"
        assert request.headers["Authorization"] == "Bearer retrieval-test-key"
        assert json.loads(request.read()) == {
            "queries": ["What is the warranty?"],
            "collection_names": ["nexrag_docs"],
            "k": 3,
        }
        return httpx.Response(
            200,
            json={
                "documents": [["Warranty text"]],
                "metadatas": [[{"source": "warranty.md", "section": "Returns"}]],
                "distances": [[0.91]],
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await search_knowledge(
                "What is the warranty?",
                base_url="http://retrieval:8002",
                api_key="retrieval-test-key",
                collection_name="nexrag_docs",
                client=client,
            )

    result = asyncio.run(run())
    assert result == [
        {
            "source": "warranty.md",
            "content": "Warranty text",
            "score": 0.91,
            "metadata": {"source": "warranty.md", "section": "Returns"},
        }
    ]


def test_agent_document_tool_uses_authorized_knowledge_without_seller_filter(monkeypatch):
    """已注册的智能体工具必须返回来源，并计入真实的检索证据。"""
    seen = {}

    async def fake_search(query, *, base_url, api_key, collection_name, top_k=3):
        seen.update(query=query, collection_name=collection_name)
        return [{"source": "warranty.md", "content": "Warranty text", "score": 0.91}]

    monkeypatch.setattr(core_tools, "search_knowledge", fake_search)
    agent = AgenticRunner()._build_agent()
    context = UserContext(
        user_id="viewer",
        email="viewer@example.local",
        tenant_id="seller-a",
        knowledge_scopes=["personal-library", "admin-granted-library"],
    )

    async def run():
        with agent.override(
            model=TestModel(
                call_tools=["search_knowledge_documents"],
                custom_output_text="The warranty is described in warranty.md.",
            )
        ):
            return await agent.run("What does the warranty say?", deps=AgentDeps(context=context))

    result = asyncio.run(run())
    assert seen["collection_name"] == ["personal-library", "admin-granted-library"]
    assert "warranty.md" in str(result.all_messages())
