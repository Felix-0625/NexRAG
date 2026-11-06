"""验证外部网络搜索工具的集成与降级机制。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from pydantic_ai.models.test import TestModel

from src.agent import core_tools
from src.agent.agent import AgentDeps, AgenticRunner, UserContext
from src.agent.tools import search_web


@pytest.fixture
def user_context():
    return UserContext(user_id="researcher", email="research@example.local", tenant_id="seller_001")


def test_search_web_formats_searxng_json_response():
    """模拟的 SearXNG JSON 响应会被解析为干净的网页搜索摘要。"""
    fake_payload = {
        "results": [
            {
                "title": "E-Commerce Market Trends 2026",
                "url": "https://example.com/trends-2026",
                "content": "Overview of online marketplace growth in Latin America.",
                "engine": "google",
            }
        ]
    }

    def respond(request: httpx.Request) -> httpx.Response:
        assert "format=json" in str(request.url)
        return httpx.Response(200, json=fake_payload)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await search_web(
                "market trends", searxng_url="http://mock-searxng:8080", client=client
            )

    results = asyncio.run(run())
    assert len(results) == 1
    assert results[0]["title"] == "E-Commerce Market Trends 2026"
    assert results[0]["url"] == "https://example.com/trends-2026"
    assert results[0]["engine"] == "google"


def test_search_web_offline_fallback():
    """外部搜索引擎离线时，工具返回友好的降级提示。"""

    def respond(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await search_web(
                "market trends", searxng_url="http://offline-searxng:8080", client=client
            )

    results = asyncio.run(run())
    assert len(results) == 1
    assert results[0]["engine"] == "fallback"
    assert "failed or was offline" in results[0]["snippet"]


def test_agent_search_web_tool_execution(monkeypatch, user_context):
    """获得真实结果后，调用 search_web_engine 工具会使证据计数增加。"""
    from src.config import settings
    from src.midplatform.data_policy import save_account_policy, save_resource_policy

    monkeypatch.setattr(settings, "SEARXNG_URL", "https://search.example")
    save_resource_policy("tenant", user_context.tenant_id, "public", False, True, False, "test")
    save_account_policy(user_context.email, {"external_search": True}, "test")

    async def fake_search(query, *, searxng_url=None, timeout=8.0, client=None):
        return [
            {
                "title": "Olist Platform Update",
                "url": "https://news.example.com/olist",
                "snippet": "New shipping rules for 2026.",
                "engine": "duckduckgo",
            }
        ]

    monkeypatch.setattr(core_tools, "search_web", fake_search)
    agent = AgenticRunner()._build_agent()
    deps = AgentDeps(context=user_context)

    async def run():
        with agent.override(model=TestModel(call_tools=["search_web_engine"])):
            return await agent.run("Find latest Olist platform news", deps=deps)

    result = asyncio.run(run())
    assert deps.evidence_calls >= 1
    assert "https://news.example.com/olist" in str(result.all_messages())


@pytest.mark.parametrize("state", ["secret", "revoked", "bridge_unavailable", "tenant_revoked"])
def test_search_rechecks_live_authorization_before_sending_query(monkeypatch, state):
    """模型在发起工具调用之前，可能已经读取过数据。"""
    from src.config import settings
    from src.midplatform.data_policy import DataAccessPolicy

    monkeypatch.setattr(settings, "ENABLE_WEB_SEARCH", True)
    probe = AsyncMock()
    monkeypatch.setattr(core_tools, "search_web", probe)
    # 先设置一个在缓存或复用时会允许外发的策略。
    monkeypatch.setattr(
        core_tools,
        "current_policy",
        lambda _: DataAccessPolicy(classification="public", external_search=True),
        raising=False,
    )
    refreshed = AsyncMock(return_value=DataAccessPolicy(classification="secret"))
    if state in {"revoked", "bridge_unavailable"}:
        refreshed.side_effect = ValueError(state)
    if state == "tenant_revoked":
        refreshed.return_value = DataAccessPolicy(classification="public", external_search=True)
        monkeypatch.setattr(core_tools, "require_business", Mock(side_effect=ValueError("revoked")))
    monkeypatch.setattr(core_tools, "refresh_policy", refreshed)
    context = UserContext(user_id="u", email="u@local", tenant_id="t")
    context.enforce_live_authorization = True
    deps = AgentDeps(context=context)
    tool = AgenticRunner()._build_agent()._function_toolset.tools["search_web_engine"]
    result = asyncio.run(tool.function(SimpleNamespace(deps=deps), "synthetic protected text"))
    assert "error" in result and "未发送" in result["error"]
    refreshed.assert_awaited_once_with(context)
    probe.assert_not_awaited()
    assert not deps.ledger.entries and deps.evidence_calls == 0
