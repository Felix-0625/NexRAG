"""Multi-step planning and query decomposition in the single agent."""

from __future__ import annotations

import asyncio

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from src.agent import core_tools
from src.agent.agent import AgentDeps, AgenticRunner, UserContext
from src.agent.grounding import EvidenceLedger
from src.agent.task_intent import plan_prohibition_conflicts
from src.agent.tools import register_plan


@pytest.fixture
def user_context():
    """Provide the authorized user context for this scenario."""
    return UserContext(user_id="analyst_1", email="analyst@example.local", tenant_id="seller_001")


def test_register_plan_logic():
    """Planning cleans subtasks and rejects empty input."""
    empty_steps, err = register_plan(["  ", ""])
    assert empty_steps == []
    assert "error" in err

    valid_steps, res = register_plan([" 1. Step A ", "2. Step B "])
    assert valid_steps == ["1. Step A", "2. Step B"]
    assert res["status"] == "plan_registered"
    assert res["step_count"] == 2


def test_plan_analysis_registers_sub_tasks_in_agent_run(monkeypatch, user_context):
    """Planning and evidence tools must store ordered steps in AgentDeps."""

    async def fake_search(query, *, base_url, api_key, collection_name, client=None, top_k=3):
        """Provide fake search test data or behavior for this scenario."""
        return [{"source": "policy.md", "content": "delay rules", "score": 0.88}]

    monkeypatch.setattr(core_tools, "search_knowledge", fake_search)
    agent = AgenticRunner()._build_agent()
    deps = AgentDeps(context=user_context)

    async def run():
        """Run this scenario in an isolated asynchronous context."""
        with agent.override(
            model=TestModel(
                call_tools=["plan_analysis", "search_knowledge_documents"],
                custom_output_text="The policy describes delay rules.",
            )
        ):
            return await agent.run(
                "Compare delay delivery rate and platform penalty rules",
                deps=deps,
            )

    asyncio.run(run())
    # TestModel supplies placeholder subtasks; verify the actual registered steps
    # reach AgentDeps.plan_steps, which was previously write-only
    # even in this test.
    assert len(deps.plan_steps) > 0
    assert deps.evidence_calls >= 1


@pytest.mark.parametrize(
    "prompt, proposed, conflicts",
    [
        ("查知识库，不查询业务数据库", {"kb", "db", "db_count"}, {"db", "db_count"}),
        ("联网搜索，不读取知识库", {"web", "kb"}, {"kb"}),
        ("查知识库，不联网，不导出", {"kb", "web", "export"}, {"web", "export"}),
        ("查询数据库和知识库并导出", {"db", "kb", "export"}, set()),
    ],
)
def test_plan_conflicts_respect_original_prohibitions(prompt, proposed, conflicts):
    assert plan_prohibition_conflicts(prompt, proposed) == conflicts


def test_rejected_plan_does_not_mutate_requirements_and_allowed_retry_can_finish(
    monkeypatch, user_context
):
    """Exercise real tool dispatch and retries, not just intent matching."""
    prompt = "查知识库，不查询业务数据库。"
    deps = AgentDeps(context=user_context)
    deps.ledger = EvidenceLedger(required={"kb"}, prompt=prompt)
    calls = 0

    async def fake_search(query, *, base_url, api_key, collection_name, client=None, top_k=3):
        """Provide fake search test data or behavior for this scenario."""
        return [{"source": "policy.md", "content": "delay rules", "score": 0.88}]

    def provider(messages, info):
        """Provide provider test data or behavior for this scenario."""
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(
                parts=[ToolCallPart("plan_analysis", {"sub_tasks": ["查询业务数据库订单数"]})]
            )
        if calls == 2:
            assert deps.ledger.required == {"kb"}
            assert not deps.ledger.entries
            assert not deps.plan_steps
            assert any(
                "explicit tool prohibitions" in str(getattr(part, "content", ""))
                for message in messages
                for part in message.parts
            )
            return ModelResponse(
                parts=[ToolCallPart("plan_analysis", {"sub_tasks": ["检索知识库制度"]})]
            )
        if calls == 3:
            assert deps.plan_steps == ["检索知识库制度"]
            assert deps.ledger.required == {"kb"}
            return ModelResponse(
                parts=[ToolCallPart("search_knowledge_documents", {"query": "delay rules"})]
            )
        return ModelResponse(
            parts=[TextPart("The policy describes delay rules. Source: policy.md.")]
        )

    monkeypatch.setattr(core_tools, "search_knowledge", fake_search)
    agent = AgenticRunner()._build_agent()

    async def run():
        """Run this scenario in an isolated asynchronous context."""
        with agent.override(model=FunctionModel(provider)):
            return await agent.run(prompt, deps=deps)

    asyncio.run(run())
    assert deps.ledger.required == {"kb"}
    assert deps.evidence_calls == 1
