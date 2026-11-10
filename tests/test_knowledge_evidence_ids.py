"""Every recorded retrieval result must expose its actual ledger ID."""

import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from src.agent import agent as module
from src.agent import core_tools
from src.agent.agent_instructions import build_dynamic_instructions
from src.agent.dependencies import AgentDeps, UserContext
from src.agent.execution import AgentExecution
from src.agent.grounding import EvidenceLedger


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
        """Provide search test data or behavior for this scenario."""
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


@pytest.mark.parametrize("scopes", [["policies"], []])
def test_local_citation_guidance_uses_current_tool_evidence_ids(scopes):
    deps = AgentDeps(
        context=UserContext(user_id="u", email="u@local", tenant_id="", knowledge_scopes=scopes)
    )
    instructions = build_dynamic_instructions(deps, schema_builder=lambda: "")
    assert ("[KB3] only if this turn's tool returned it" in instructions) is bool(scopes)


@pytest.mark.parametrize(
    ("enabled", "scopes", "tenant", "prompt", "expected"),
    [
        (True, ["library"], "", "合同什么时候到期？", {"kb"}),
        (False, ["library"], "", "合同什么时候到期？", set()),
        (True, [], "", "合同什么时候到期？", set()),
        (True, ["library"], "business", "合同什么时候到期？", set()),
        (True, ["library"], "", "合同什么时候到期？不要查询知识库", set()),
        (True, ["library"], "", "请上网搜索这项标准", {"web"}),
    ],
)
def test_optional_knowledge_mode_requires_evidence_only_in_its_scope(
    monkeypatch, enabled, scopes, tenant, prompt, expected
):
    monkeypatch.setattr(module.settings, "REQUIRE_KNOWLEDGE_EVIDENCE", enabled)
    context = UserContext(user_id="u", email="u@local", tenant_id=tenant, knowledge_scopes=scopes)
    deps = AgentDeps(context=context)
    policy = SimpleNamespace(allowed_tool_ids=[])
    AgentExecution._bind_requirements(deps, prompt, context, policy, None, {})
    assert deps.ledger.required == expected


def test_knowledge_answer_rejects_unknown_or_empty_turn_citations():
    ledger = EvidenceLedger(required={"kb"}, prompt="What does the contract say?")
    assert ledger.add("kb", [{"content": "The contract may be renewed."}]) == "KB1"
    assert ledger.add("kb", []) == "KB2"
    assert not ledger.issues("The contract may be renewed. [KB1]")
    issues = ledger.issues("The contract may be renewed. [KB1] [KB2] [kb3]")
    assert "Unknown or empty current-turn knowledge evidence IDs: KB2, KB3" in issues
