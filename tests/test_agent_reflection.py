"""验证智能体的自我反思、迭代改进与查询改写提示。"""

from __future__ import annotations

import asyncio

import pytest
from pydantic_ai.models.test import TestModel

from src.agent import core_tools
from src.agent.agent import AgentDeps, AgenticRunner, UserContext


@pytest.fixture
def user_context(isolated_ops):
    from src.midplatform.tenant_access import create_tenant, set_grant

    create_tenant("seller_001", "Synthetic seller")
    set_grant("user@example.local", "seller_001", True, "test")
    return UserContext(user_id="test_user", email="user@example.local", tenant_id="seller_001")


def test_knowledge_search_returns_reflection_hint_when_empty(monkeypatch, user_context):
    """未检索到文档时，智能体工具给出反思提示以触发改写。"""

    async def fake_search(query, *, base_url, api_key, collection_name, client=None, top_k=3):
        return []

    monkeypatch.setattr(core_tools, "search_knowledge", fake_search)
    agent = AgenticRunner()._build_agent()
    deps = AgentDeps(context=user_context)

    async def run():
        with agent.override(
            model=TestModel(
                call_tools=["search_knowledge_documents"],
                custom_output_text="No reliable document was found.",
            )
        ):
            return await agent.run("Tell me about an unknown topic", deps=deps)

    result = asyncio.run(run())
    messages_str = str(result.all_messages())
    assert "reflection_hint" in messages_str
    assert deps.reflections == 1


def test_knowledge_search_warns_on_low_relevance_score(monkeypatch, user_context):
    """最高得分低于置信阈值时，工具返回 low_relevance_warning。"""

    async def fake_search(query, *, base_url, api_key, collection_name, client=None, top_k=3):
        return [{"source": "doc.md", "content": "vague hit", "score": 0.15}]

    monkeypatch.setattr(core_tools, "search_knowledge", fake_search)
    agent = AgenticRunner()._build_agent()
    deps = AgentDeps(context=user_context)

    async def run():
        with agent.override(
            model=TestModel(
                call_tools=["search_knowledge_documents"],
                custom_output_text="No reliable document was found.",
            )
        ):
            return await agent.run("Check low score topic", deps=deps)

    result = asyncio.run(run())
    messages_str = str(result.all_messages())
    assert "low_relevance_warning" in messages_str
    assert deps.reflections == 1


def test_database_query_returns_reflection_hint_on_zero_rows(monkeypatch, user_context):
    """SQL 返回 0 行时，工具建议检查过滤条件或大小写。"""

    def fake_query(sql, tenant_id, db_path):
        return {"row_count": 0, "rows": []}

    monkeypatch.setattr(core_tools, "query_business", fake_query)
    agent = AgenticRunner()._build_agent()
    deps = AgentDeps(context=user_context)

    async def run():
        with agent.override(
            model=TestModel(
                call_tools=["query_database"], custom_output_text="No rows matched the query."
            )
        ):
            return await agent.run("Query my zero rows table", deps=deps)

    result = asyncio.run(run())
    messages_str = str(result.all_messages())
    assert "reflection_hint" in messages_str
    assert deps.reflections == 1


def test_database_query_returns_valid_evidence_when_rows_exist(monkeypatch, user_context):
    """SQL 返回数据行时，evidence_calls 计数增加且不带反思提示。"""

    def fake_query(sql, tenant_id, db_path):
        return {"row_count": 2, "rows": [{"order_id": "1"}, {"order_id": "2"}]}

    monkeypatch.setattr(core_tools, "query_business", fake_query)
    agent = AgenticRunner()._build_agent()
    deps = AgentDeps(context=user_context)

    async def run():
        with agent.override(model=TestModel(call_tools=["query_database"])):
            return await agent.run("Count orders", deps=deps)

    asyncio.run(run())
    assert deps.evidence_calls == 1
    assert deps.reflections == 0
