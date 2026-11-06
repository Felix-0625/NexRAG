"""模型分发必须同时约束发现与执行，包括旧对话。"""

import ast
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import jwt
import pytest
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from src.agent.agent import AgenticRunner, UserContext, runner
from src.config import settings
from src.main import app
from src.midplatform import agent_models


def headers(user_id="admin", role="admin"):
    claims = dict(
        sub=user_id,
        email=f"{user_id}@local.test",
        role=role,
        iss="open-webui",
        iat=int(time.time()),
        exp=int(time.time()) + 60,
    )
    return {
        "Authorization": "Bearer " + "k" * 32,
        "X-OpenWebUI-User-Jwt": jwt.encode(claims, "s" * 32, algorithm="HS256"),
    }


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(settings, "NEXRAG_SHARED_API_KEY", "k" * 32)
    monkeypatch.setattr(settings, "NEXRAG_USER_JWT_SECRET", "s" * 32)
    client = TestClient(app)
    for identifier in ("employee-a", "employee-b"):
        response = client.post(
            "/v1/workspace/directory/identities",
            headers=headers(),
            json={"user_id": identifier, "email": f"{identifier}@local.test", "name": identifier},
        )
        assert response.status_code == 200
    return client


def form(**changes):
    return (
        dict(
            name="Restricted local model",
            provider="ollama",
            model_name="qwen-synthetic",
            base_url="http://127.0.0.1:11434/v1",
            audience="selected",
            allowed_user_ids=["employee-a"],
        )
        | changes
    )


def test_model_distribution_filters_all_catalogs_and_denies_revoked_old_chat(api, monkeypatch):
    created = api.post("/v1/workspace/models", headers=headers(), json=form())
    assert created.status_code == 200
    identifier = created.json()["id"]
    run = AsyncMock(return_value="Synthetic answer")
    monkeypatch.setattr(runner, "run", run)

    def ids(path, user_id):
        payload = api.get(path, headers=headers(user_id, "user")).json()
        return {item["id"] for item in payload.get("models", payload.get("data", []))}

    for path in ("/v1/models", "/v1/workspace/models", "/v1/workspace/policy/tools"):
        assert identifier in ids(path, "employee-a")
        assert identifier not in ids(path, "employee-b")
    anonymous = api.get("/v1/models").json()["data"]
    assert identifier not in {item["id"] for item in anonymous}
    employee_payload = api.get(
        "/v1/workspace/models", headers=headers("employee-a", "user")
    ).json()["data"]
    assert all(
        "allowed_user_ids" not in item and "base_url" not in item for item in employee_payload
    )

    body = {"model": identifier, "messages": [{"role": "user", "content": "Synthetic question"}]}
    assert (
        api.post(
            "/v1/chat/completions", headers=headers("employee-a", "user"), json=body
        ).status_code
        == 200
    )
    run.reset_mock()
    assert (
        api.post(
            "/v1/chat/completions", headers=headers("employee-b", "user"), json=body
        ).status_code
        == 403
    )
    run.assert_not_awaited()

    assert (
        api.post(
            f"/v1/workspace/models/{identifier}", headers=headers(), json=form(allowed_user_ids=[])
        ).status_code
        == 200
    )
    assert (
        api.post(
            "/v1/chat/completions", headers=headers("employee-a", "user"), json=body
        ).status_code
        == 403
    )
    run.assert_not_awaited()


def test_default_stays_usable_and_unknown_account_is_rejected(api):
    assert (
        api.post(
            "/v1/workspace/models", headers=headers(), json=form(allowed_user_ids=["unknown"])
        ).status_code
        == 400
    )
    created = api.post("/v1/workspace/models", headers=headers(), json=form()).json()
    assert (
        api.post(f"/v1/workspace/models/{created['id']}/default", headers=headers()).status_code
        == 400
    )
    assert (
        api.post(
            f"/v1/workspace/models/{agent_models.DEFAULT_ID}", headers=headers(), json=form()
        ).status_code
        == 400
    )


def test_revocation_between_agent_model_requests_prevents_second_provider_call(api, monkeypatch):
    identifier = api.post("/v1/workspace/models", headers=headers(), json=form()).json()["id"]
    calls = []

    def provider(messages, info):
        calls.append(messages)
        agent_models.save_model(agent_models.ModelForm(**form(allowed_user_ids=[])), identifier)
        return ModelResponse(
            parts=[ToolCallPart("plan_analysis", {"sub_tasks": ["Synthetic task"]})]
        )

    monkeypatch.setattr(agent_models, "build_model", lambda _: FunctionModel(provider))
    monkeypatch.setattr("src.midplatform.data_policy.refresh_knowledge_authorization", AsyncMock())
    context = UserContext(
        user_id="employee-a",
        email="employee-a@local.test",
        role="user",
        tenant_id="",
        model_id=identifier,
        enforce_live_authorization=True,
    )
    with pytest.raises(ValueError, match="使用权限已撤销"):
        asyncio.run(AgenticRunner().run("Synthetic task", context))
    assert len(calls) == 1


def test_actual_webui_filter_rechecks_cached_model_list_and_fails_closed(monkeypatch):
    source = Path("webui/backend/open_webui/utils/nexrag_core.py").read_text("utf8")
    function = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "filter_distributed_models"
    )
    import os

    core = AsyncMock(return_value={"data": [{"id": "allowed", "enabled": True}]})
    ns = {"os": os, "core_request": core}
    exec(
        compile(ast.Module(body=[function], type_ignores=[]), "<shipped-model-filter>", "exec"), ns
    )
    monkeypatch.setenv("NEXRAG_WORKSPACE", "true")
    cached = [{"id": "allowed"}, {"id": "revoked"}]
    user = SimpleNamespace(id="employee-a")
    assert asyncio.run(ns["filter_distributed_models"](None, user, cached)) == [{"id": "allowed"}]
    core.return_value = {"data": []}
    assert asyncio.run(ns["filter_distributed_models"](None, user, cached)) == []
    core.side_effect = RuntimeError("core unavailable")
    with pytest.raises(RuntimeError, match="core unavailable"):
        asyncio.run(ns["filter_distributed_models"](None, user, cached))
