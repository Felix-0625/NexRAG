"""默认拒绝披露与显式的组织授权相互独立。"""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from src.midplatform import data_policy as policy
from src.midplatform import directory
from src.midplatform.tenant_access import Principal, allowed_tenants, create_tenant


def model(identifier="local", provider="ollama", url="http://127.0.0.1:11434/v1", name="qwen3:4b"):
    return dict(id=identifier, provider=provider, base_url=url, model_name=name)


def assignment(user_id, email, **changes):
    return dict(
        user_id=user_id,
        email=email,
        name=user_id,
        department_id="",
        job_level="P3",
        manager_id="",
        tenant_ids=["sales"],
        egress={},
        **changes,
    )


def test_internal_and_unknown_data_default_to_local():
    decision = policy.effective_policy("employee@local", "sales", ["private"])
    decision.check_model(model())
    assert not decision.external_search and not decision.external_tools
    with pytest.raises(ValueError, match="本机"):
        decision.check_model(model("cloud", "ollama", "https://untrusted.example/v1"))
    assert not policy.model_is_local(model(name="qwen3:cloud"))


def test_remote_needs_account_exact_model_and_every_resource_approval():
    policy.save_account_policy(
        "employee@local", {"external_models": True, "allowed_model_ids": ["approved"]}, "admin"
    )
    policy.save_resource_policy("tenant", "sales", "public", True, False, False, "admin")
    policy.save_resource_policy("knowledge", "public-kb", "public", True, False, False, "admin")
    approved = model("approved", "openai", "https://api.example/v1")
    policy.effective_policy("employee@local", "sales", ["public-kb"]).check_model(approved)
    with pytest.raises(ValueError):
        policy.effective_policy("employee@local", "sales", ["public-kb", "private"]).check_model(
            approved
        )
    with pytest.raises(ValueError):
        policy.effective_policy("employee@local", "sales", ["public-kb"]).check_model(
            model("other", "openai", "https://api.example/v1")
        )


def test_signed_secret_label_overrides_approved_external_account():
    policy.save_account_policy(
        "employee@local",
        {
            "external_models": True,
            "external_tools": True,
            "external_search": True,
            "allowed_model_ids": ["approved"],
        },
        "admin",
    )
    label = dict(
        classification="secret", external_models=False, external_tools=False, external_search=False
    )
    decision = policy.effective_policy("employee@local", "", ["private"], {"private": label})
    assert decision.classification == "secret"
    assert not any((decision.external_models, decision.external_tools, decision.external_search))
    with pytest.raises(ValueError):
        decision.check_model(model("approved", "custom", "https://api.example/v1"))


@pytest.mark.parametrize("classification", ["internal", "secret"])
def test_nonpublic_search_cannot_be_enabled(classification):
    with pytest.raises(ValueError):
        policy.save_resource_policy("tenant", "sales", classification, False, True, False, "admin")


def test_grants_do_not_follow_reporting_line_or_template_changes():
    create_tenant("sales", "Sales")
    directory.assign(assignment("manager", "manager@local"), "admin")
    staff = assignment("staff", "staff@local")
    staff["manager_id"] = "manager"
    staff["tenant_ids"] = []
    directory.assign(staff, "admin")
    assert allowed_tenants(Principal("staff", "staff@local", "user")) == {}
    directory.save_template("analyst", "Analyst", {"tenant_ids": ["sales"], "egress": {}})
    assert allowed_tenants(Principal("staff", "staff@local", "user")) == {}
    with pytest.raises(ValueError, match="成环"):
        parent = assignment("manager", "manager@local")
        parent["manager_id"] = "staff"
        directory.assign(parent, "admin")


def test_invalid_assignment_does_not_partially_revoke_existing_grants():
    create_tenant("sales", "Sales")
    directory.assign(assignment("staff", "staff@local"), "admin")
    wrong = assignment("staff", "staff@local")
    wrong["tenant_ids"] = ["missing"]
    with pytest.raises(ValueError):
        directory.assign(wrong, "admin")
    assert allowed_tenants(Principal("staff", "staff@local", "user")) == {"sales": "Sales"}


def test_live_revocation_aborts_existing_context(monkeypatch):
    context = SimpleNamespace(
        enforce_live_authorization=True,
        user_id="staff",
        knowledge_scopes=["private"],
        knowledge_labels={},
    )

    async def respond(request):
        return httpx.Response(
            200,
            json={
                "knowledge_scopes": [],
                "knowledge_labels": {},
                "role": "user",
                "email": "staff@local",
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        policy.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(respond))
    )
    with pytest.raises(ValueError, match="权限"):
        asyncio.run(policy.refresh_knowledge_authorization(context))
