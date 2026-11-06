"""每条已记录的检索结果都要告知智能体其真实的台账 ID。"""

import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from src.agent import agent as module
from src.agent import core_tools
from src.agent.dependencies import AgentDeps, UserContext


@pytest.mark.parametrize("branch", ["normal", "low_relevance", "empty", "omitted"])
def test_kb_result_returns_ledger_id_for_all_recorded_branches(monkeypatch, branch):
    documents = (
        []
        if branch == "empty"
        else [
            {
                "source": "policy.md",
                "content": "折扣上限6%。",
                "score": 0.1 if branch == "low_relevance" else 1.0,
            }
        ]
    )

    async def search(*a, **kw):
        return [dict(item) for item in documents]

    monkeypatch.setattr(core_tools, "search_knowledge", search)
    monkeypatch.setattr(module.settings, "RETRIEVAL_API_BASE_URL", "http://127.0.0.1:8002")
    runner = module.AgenticRunner()
    agent = runner._build_agent()
    deps = AgentDeps(context=UserContext(user_id="u", email="u@local", tenant_id=""))
    if branch == "omitted":
        deps.document_context_keys.add(hashlib.sha256(documents[0]["content"].encode()).hexdigest())
    callback = agent._function_toolset.tools["search_knowledge_documents"].function
    result = asyncio.run(callback(SimpleNamespace(deps=deps), "折扣政策"))
    assert result["evidence_id"] == "KB1"
    assert deps.ledger.entries[-1]["id"] == result["evidence_id"]
    assert deps.ledger.entries[-1]["kind"] == "kb"
    if branch == "low_relevance":
        assert result.get("low_relevance_warning")
    if branch == "omitted":
        assert result["context_budget_omitted"] == 1
