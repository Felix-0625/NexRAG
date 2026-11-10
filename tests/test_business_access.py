"""Truthful denials must not count as completion after fabricated data."""

from src.agent.business_access import check_requested_scope
from src.agent.dependencies import AgentDeps, UserContext
from src.agent.grounding import EvidenceLedger
from src.agent.run_options import EvaluationRun


def deps(prompt):
    """Provide deps test data or behavior for this scenario."""
    result = AgentDeps(context=UserContext(user_id="b", email="b@local", tenant_id="company-b"))
    result.evaluation = EvaluationRun(guidance="", params={}, authorized_tenants=("company-b",))
    result.ledger = EvidenceLedger(required={"db", "db_details"}, prompt=prompt)
    return result


def test_real_denial_satisfies_only_business_obligations():
    context = deps("请读取公司 company-a 的发票明细")
    context.ledger.required.add("kb")
    assert not check_requested_scope(context, "company-a")["allowed"]
    assert context.evaluation.denied
    assert context.ledger.missing() == {"kb"}
    assert "已拒绝" in context.ledger.safe_answer()


def test_model_cannot_invent_a_denied_target_or_negate_current_request():
    context = deps("查询 company-b，不要读取 company-a")
    assert "error" in check_requested_scope(context, "company-a")
    assert not context.ledger.entries
    assert check_requested_scope(context, "company-b")["allowed"]
    assert not context.ledger.entries
    assert "error" in check_requested_scope(context, "company")


def test_allowed_but_unselected_scope_requires_ui_switch():
    context = deps("查询 company-a")
    context.evaluation.authorized_tenants = ("company-a", "company-b")
    assert "切换" in check_requested_scope(context, "company-a")["message"]


def test_known_explicit_foreign_scope_is_refused_before_model(monkeypatch):
    from src.agent import business_access

    monkeypatch.setattr(
        business_access, "list_tenants", lambda: {"company-a": "A", "company-b": "B"}
    )
    monkeypatch.setattr(business_access, "allowed_tenants", lambda principal: {"company-b": "B"})
    context = deps("读取 company-a 发票明细")
    context.evaluation = None
    assert "已拒绝" in business_access.explicit_scope_refusal(context)["message"]
    context = deps("读取 company-b，不要读取 company-a")
    context.evaluation = None
    assert business_access.explicit_scope_refusal(context) is None
