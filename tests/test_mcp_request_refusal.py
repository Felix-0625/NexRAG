"""不可用的显式 MCP 授权，不得重放旧的工具结果。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from src.agent.agent import AgenticRunner
from src.agent.dependencies import UserContext
from src.agent.grounding import EvidenceLedger, GroundingFailure, requested_capabilities
from src.agent.mcp_tools import explicit_tool_refusal, requested_tool_ids
from src.agent.run_options import EvaluationRun
from src.midplatform import mcp_registry
from src.midplatform.data_policy import save_account_policy

TOOL_ID = "mcp:fixture:acceptance_sum"
PROMPT = "再次实际调用 acceptance_sum，将公开合成整数 19 和 27 相加。若工具已撤销审批或无权调用，明确拒绝执行，不要心算、不要引用历史返回值，不要调用其他工具。"


@pytest.mark.parametrize(
    "prompt",
    [
        "调用已授权的 acceptance_sum 工具，返回实际结果。",
        "请执行当前审批的 `acceptance_sum` 工具。",
        "请使用 MCP 工具“acceptance_sum”。",
        'Please invoke the currently approved MCP tool "acceptance_sum".',
        "Use the authorized acceptance_sum tool now.",
    ],
)
def test_revoked_granted_names_with_descriptors_are_detected(monkeypatch, prompt):
    policy = SimpleNamespace(
        allowed_tool_ids=[TOOL_ID], external_tools=True, classification="internal"
    )
    monkeypatch.setattr(mcp_registry, "approved_catalog", lambda: [])
    assert requested_tool_ids(prompt, policy) == {TOOL_ID}
    assert explicit_tool_refusal(prompt, policy)


@pytest.mark.parametrize(
    "prompt",
    [
        "不要调用已授权的 acceptance_sum 工具。",
        "Do not use the approved acceptance_sum tool.",
        "Never invoke acceptance_sum.",
        "Explain how to use acceptance_sum.",
        "解释如何使用 acceptance_sum 工具。",
        "请调用 acceptance_summary。",
    ],
)
def test_negative_descriptive_and_different_names_are_not_invocations(monkeypatch, prompt):
    policy = SimpleNamespace(
        allowed_tool_ids=[TOOL_ID], external_tools=True, classification="internal"
    )
    monkeypatch.setattr(mcp_registry, "approved_catalog", lambda: [])
    assert not requested_tool_ids(prompt, policy)
    assert explicit_tool_refusal(prompt, policy) is None


def test_explicit_mcp_name_with_removed_grant_is_denied_without_disclosing_registry(monkeypatch):
    policy = SimpleNamespace(allowed_tool_ids=[], external_tools=True, classification="internal")
    monkeypatch.setattr(mcp_registry, "approved_catalog", lambda: [])
    assert explicit_tool_refusal("调用 MCP 工具 acceptance_sum。", policy)


@pytest.mark.parametrize(
    "tool_id,prompt",
    [
        (TOOL_ID, PROMPT),
        (
            "mcp:fixture:read_text_file",
            "调用已授权的 read_text_file 工具，path 参数为 "
            "C:/Users/FAST/Desktop/agentic_rag/.local-run/f31-junction-20261101/"
            "sandbox/acceptance-public.txt。原样返回这个合成文件的实际内容；"
            "不要查询数据库、知识库、联网或导出。",
        ),
    ],
)
def test_revoked_mcp_is_denied_before_model_or_history_replay(monkeypatch, tool_id, prompt):
    context = UserContext(user_id="mcp-test", email="mcp-test@local", tenant_id="")
    save_account_policy(
        context.email, {"external_tools": True, "allowed_tool_ids": [tool_id]}, "test"
    )
    monkeypatch.setattr(mcp_registry, "approved_catalog", lambda: [])
    runner = AgenticRunner()
    runner._agent = Mock()
    evaluation = EvaluationRun(guidance="", params={})
    answer = asyncio.run(
        runner.run(prompt, context, [("assistant", "旧工具返回 total=42")], evaluation=evaluation)
    )
    assert "已拒绝" in answer and "未调用工具、未发送参数" in answer
    assert "42" not in answer
    assert evaluation.status == "denied"
    runner._agent.run.assert_not_called()


@pytest.mark.parametrize("external_tools,classification", [(False, "internal"), (True, "secret")])
def test_current_data_policy_denies_even_an_approved_grant(
    monkeypatch, external_tools, classification
):
    monkeypatch.setattr(mcp_registry, "approved_catalog", lambda: [{"tool_id": TOOL_ID}])
    policy = SimpleNamespace(
        allowed_tool_ids=[TOOL_ID], external_tools=external_tools, classification=classification
    )
    assert explicit_tool_refusal(PROMPT, policy)


def test_approved_grant_and_unrequested_names_are_not_preempted(monkeypatch):
    monkeypatch.setattr(mcp_registry, "approved_catalog", lambda: [{"tool_id": TOOL_ID}])
    policy = SimpleNamespace(
        allowed_tool_ids=[TOOL_ID], external_tools=True, classification="internal"
    )
    assert explicit_tool_refusal(PROMPT, policy) is None
    monkeypatch.setattr(mcp_registry, "approved_catalog", lambda: [])
    assert explicit_tool_refusal("不要调用 acceptance_sum；请解释工具授权。", policy) is None
    assert explicit_tool_refusal("请调用 acceptance_summary", policy) is None
    assert explicit_tool_refusal("acceptance_sum 是什么？", policy) is None


def test_tool_approval_does_not_require_business_kb_but_approval_policy_does():
    assert requested_capabilities(PROMPT, False) == set()
    assert requested_capabilities("订单审批负责人是谁？", False) == {"kb"}
    assert requested_capabilities("项目撤销审批由谁处理？", False) == {"kb"}
    assert requested_capabilities("工具撤销审批后，检索制度中的审批负责人。", False) == {"kb"}


def test_current_turn_evidence_must_match_requested_tool_identity():
    ledger = EvidenceLedger(required={"tool:" + TOOL_ID})
    ledger.add("tool", {"content": "another result"}, tool_id="mcp:other:another_tool")
    assert ledger.missing() == {"tool:" + TOOL_ID}
    assert ledger.issues("An earlier result is synthetic-read-result.")
    ledger.add("tool", {"content": "synthetic-read-result"}, tool_id=TOOL_ID)
    assert not ledger.missing()
    assert not ledger.issues("synthetic-read-result")


@pytest.mark.parametrize("invoke_current_tool", [False, True])
def test_approved_request_cannot_replay_history_without_current_call(
    monkeypatch, invoke_current_tool
):
    """生产运行器会先绑定完成状态，再引用旧对话。"""
    from src.midplatform import agent_models

    context = UserContext(user_id="mcp-test", email="mcp-test@local", tenant_id="")
    save_account_policy(
        context.email, {"external_tools": True, "allowed_tool_ids": [TOOL_ID]}, "test"
    )
    spec = dict(
        tool_id=TOOL_ID,
        name="acceptance_sum",
        server_name="Synthetic",
        description="Return a synthetic test marker",
        parameters={"type": "object", "properties": {}},
        revision=1,
        schema_hash="a" * 64,
    )
    monkeypatch.setattr(mcp_registry, "approved_catalog", lambda: [spec])
    actual_result = {"content": [{"type": "text", "text": "synthetic-read-result"}]}
    invoke = AsyncMock(return_value=actual_result)
    monkeypatch.setattr(mcp_registry, "invoke_tool", invoke)
    calls = 0

    def provider(messages, info):
        nonlocal calls
        calls += 1
        if invoke_current_tool and calls == 1:
            name = next(tool.name for tool in info.function_tools if tool.name.startswith("mcp_"))
            return ModelResponse(parts=[ToolCallPart(name, {})])
        return ModelResponse(parts=[TextPart("synthetic-read-result")])

    monkeypatch.setattr(agent_models, "build_model", lambda _: FunctionModel(provider))
    evaluation = EvaluationRun(guidance="", params={})
    runner = AgenticRunner()
    pending = runner.run(
        "调用已授权的 acceptance_sum 工具，返回实际结果。",
        context,
        [("assistant", "synthetic-read-result")],
        evaluation=evaluation,
    )
    if invoke_current_tool:
        assert asyncio.run(pending) == "synthetic-read-result"
        invoke.assert_awaited_once()
        assert evaluation.status == "ok"
    else:
        with pytest.raises(GroundingFailure):
            asyncio.run(pending)
        invoke.assert_not_awaited()
        assert evaluation.status == "error"
