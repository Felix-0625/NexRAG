import asyncio
import json
import time
from unittest.mock import AsyncMock

import jwt
import pytest
from fastapi.testclient import TestClient
from pydantic_ai.models.test import TestModel

from src.agent.agent import AgenticRunner, UserContext, runner
from src.config import settings
from src.main import app
from src.midplatform import agent_models
from src.midplatform.data_policy import save_account_policy, save_resource_policy


def test_anthropic_sdk_serializes_messages_and_round_trips_tools(monkeypatch):
    """验证真实 SDK 的序列化，而不只是 provider 的构造。

    Anthropic 2.x 把 BetaMessageParam 改成了联合类型。PydanticAI 2.59 构造该别名
    时在发出 HTTP 请求前就失败，仅测试构造函数的用例漏掉了这一问题。
    """
    from anthropic.types.beta import BetaMessage
    from pydantic_ai import Agent

    monkeypatch.setattr(agent_models, "key_for", lambda _: "synthetic-sdk-key")
    model = agent_models.build_model(
        {
            "protocol": "anthropic",
            "model_name": "deepseek-flash",
            "base_url": "https://model.example/anthropic",
        }
    )

    def response(content, stop_reason):
        return BetaMessage.model_validate(
            {
                "id": "msg-synthetic",
                "type": "message",
                "role": "assistant",
                "model": "deepseek-flash",
                "content": content,
                "stop_reason": stop_reason,
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 10},
            }
        )

    sdk = AsyncMock(
        side_effect=[
            response(
                [
                    {
                        "type": "tool_use",
                        "id": "tool-synthetic",
                        "name": "connection_check",
                        "input": {"value": "NEXRAG_MODEL_OK"},
                    }
                ],
                "tool_use",
            ),
            response([{"type": "text", "text": "NEXRAG_MODEL_OK"}], "end_turn"),
        ]
    )
    monkeypatch.setattr(model.client.beta.messages, "create", sdk)
    called = []

    def connection_check(value: str) -> str:
        called.append(value)
        return value

    result = asyncio.run(Agent(model, tools=[connection_check]).run("Perform the synthetic check."))
    assert result.output == "NEXRAG_MODEL_OK"
    assert called == ["NEXRAG_MODEL_OK"]
    assert sdk.await_count == 2
    first, second = [call.kwargs["messages"] for call in sdk.await_args_list]
    assert first[0]["role"] == "user"
    assert any(
        part.get("type") == "tool_result" and part.get("tool_use_id") == "tool-synthetic"
        for message in second
        for part in message["content"]
        if isinstance(part, dict)
    )


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(settings, "NEXRAG_SHARED_API_KEY", "k" * 32)
    monkeypatch.setattr(settings, "NEXRAG_USER_JWT_SECRET", "s" * 32)
    return TestClient(app)


def headers(role="admin"):
    claims = dict(
        sub="model-user",
        email="model@local.test",
        role=role,
        iss="open-webui",
        iat=int(time.time()),
        exp=int(time.time()) + 60,
        knowledge_scopes=["my-knowledge"],
    )
    return {
        "Authorization": "Bearer " + "k" * 32,
        "X-OpenWebUI-User-Jwt": jwt.encode(claims, "s" * 32, algorithm="HS256"),
    }


def form(**changes):
    return dict(
        name="Second model",
        provider="custom",
        protocol="openai",
        model_name="second-actual-model",
        base_url="https://model.example/v1",
        api_key="private-provider-key",
        **changes,
    )


def test_keys_are_encrypted_never_returned_and_not_reused_for_new_host(api, isolated_ops):
    response = api.post("/v1/workspace/models", headers=headers(), json=form())
    assert response.status_code == 200
    identifier = response.json()["id"]
    assert "private-provider-key" not in response.text
    stored = isolated_ops.query_one("SELECT config FROM agent_models WHERE id=?", (identifier,))
    assert "private-provider-key" not in stored["config"]
    assert agent_models.key_for(agent_models.resolve_model(identifier)) == "private-provider-key"
    public = api.get("/v1/workspace/models", headers=headers("user")).json()
    assert "base_url" not in json.dumps(public) and "api_key" not in json.dumps(public)
    body = form()
    body["api_key"] = ""
    assert (
        api.post(f"/v1/workspace/models/{identifier}", headers=headers(), json=body).status_code
        == 200
    )
    assert agent_models.key_for(agent_models.resolve_model(identifier)) == "private-provider-key"
    body["base_url"] = "https://another.example/v1"
    assert (
        api.post(f"/v1/workspace/models/{identifier}", headers=headers(), json=body).status_code
        == 400
    )
    body["clear_api_key"] = True
    assert (
        api.post(f"/v1/workspace/models/{identifier}", headers=headers(), json=body).status_code
        == 200
    )
    assert agent_models.key_for(agent_models.resolve_model(identifier)) == ""


def test_model_selection_defaults_disabled_models_and_admin_boundary(api, monkeypatch):
    assert api.post("/v1/workspace/models", headers=headers("user"), json=form()).status_code == 403
    created = api.post("/v1/workspace/models", headers=headers(), json=form()).json()
    identifier = created["id"]
    for path in (identifier, identifier + "/test", identifier + "/default"):
        assert (
            api.post(
                "/v1/workspace/models/" + path, headers=headers("user"), json=form()
            ).status_code
            == 403
        )
    assert (
        api.post(f"/v1/workspace/models/{identifier}/default", headers=headers()).status_code == 200
    )
    assert agent_models.resolve_model("NexRAG")["id"] == identifier
    body = form(enabled=False)
    assert (
        api.post(f"/v1/workspace/models/{identifier}", headers=headers(), json=body).status_code
        == 400
    )
    assert (
        api.post(
            f"/v1/workspace/models/{agent_models.DEFAULT_ID}/default", headers=headers()
        ).status_code
        == 200
    )
    assert (
        api.post(f"/v1/workspace/models/{identifier}", headers=headers(), json=body).status_code
        == 200
    )
    run = AsyncMock(return_value="ok")
    monkeypatch.setattr(runner, "run", run)
    response = api.post(
        "/v1/chat/completions",
        headers=headers("user"),
        json={"model": identifier, "messages": [{"role": "user", "content": "question"}]},
    )
    assert response.status_code == 404
    run.assert_not_awaited()
    discovered = api.get("/v1/models").json()["data"]
    assert [item["id"] for item in discovered] == [agent_models.DEFAULT_ID]
    assert discovered[0]["info"]["meta"]["capabilities"]["builtin_tools"] is False


def test_chat_passes_real_selection_and_keeps_signed_knowledge_scope(api, monkeypatch):
    identifier = api.post("/v1/workspace/models", headers=headers(), json=form()).json()["id"]
    save_resource_policy("knowledge", "my-knowledge", "public", True, False, False, "test-admin")
    save_account_policy(
        "model@local.test",
        {"external_models": True, "allowed_model_ids": [identifier]},
        "test-admin",
    )
    run = AsyncMock(return_value="ok")
    monkeypatch.setattr(runner, "run", run)
    response = api.post(
        "/v1/chat/completions",
        headers=headers("user"),
        json={"model": identifier, "messages": [{"role": "user", "content": "question"}]},
    )
    assert response.status_code == 200
    context = run.await_args.args[1]
    assert context.model_id == identifier
    assert context.knowledge_scopes == ["my-knowledge"]
    assert context.user_id == "model-user"


def test_runner_uses_request_model_without_replacing_shared_tools(api, monkeypatch):
    identifier = api.post("/v1/workspace/models", headers=headers(), json=form()).json()["id"]
    save_resource_policy("knowledge", "my-knowledge", "public", True, False, False, "test-admin")
    save_account_policy(
        "test@local.test",
        {"external_models": True, "allowed_model_ids": [identifier]},
        "test-admin",
    )
    agent_runner = AgenticRunner()
    agent_runner._agent = agent_runner._build_agent()
    used = []

    def build(record):
        used.append(record["model_name"])
        return TestModel(call_tools=["plan_analysis", "search_knowledge_documents"])

    async def search(*args, **kwargs):
        return [{"source": "test-doc", "content": "known evidence", "score": 0.9}]

    monkeypatch.setattr(agent_models, "build_model", build)
    monkeypatch.setattr("src.agent.core_tools.search_knowledge", search)
    result = asyncio.run(
        agent_runner.run(
            "question",
            UserContext(
                user_id="model-user",
                email="test@local.test",
                tenant_id="",
                model_id=identifier,
                knowledge_scopes=["my-knowledge"],
            ),
        )
    )
    assert used == ["second-actual-model"]
    assert result


@pytest.mark.parametrize(
    "protocol,provider,expected",
    [
        ("openai", "custom", "OpenAIChatModel"),
        ("responses", "openai", "OpenAIResponsesModel"),
        ("anthropic", "anthropic", "AnthropicModel"),
        ("google", "google", "GoogleModel"),
    ],
)
def test_native_protocol_factories_do_not_need_live_api_calls(api, protocol, provider, expected):
    body = form()
    body.update(protocol=protocol, provider=provider)
    identifier = api.post("/v1/workspace/models", headers=headers(), json=body).json()["id"]
    model = agent_models.build_model(agent_models.resolve_model(identifier))
    assert type(model).__name__ == expected


def test_provider_profiles_preserve_reasoning_model_compatibility(api):
    body = form()
    body.update(provider="openai", model_name="o3-mini")
    identifier = api.post("/v1/workspace/models", headers=headers(), json=body).json()["id"]
    model = agent_models.build_model(agent_models.resolve_model(identifier))
    assert model.profile["openai_supports_reasoning"] is True
    assert model.profile["thinking_always_enabled"] is True
    body.update(provider="deepseek", model_name="deepseek-reasoner")
    identifier = api.post("/v1/workspace/models", headers=headers(), json=body).json()["id"]
    model = agent_models.build_model(agent_models.resolve_model(identifier))
    assert model.system == "deepseek"


@pytest.mark.parametrize("model_name", ["deepseek-flash", "deepseek-v4-pro"])
def test_deepseek_thinking_models_do_not_send_unsupported_forced_tool_choice(
    monkeypatch, model_name
):
    """验证真实 SDK 的请求路径；思考模式下 API 会拒绝 'required'。"""
    from openai.types.chat import ChatCompletion
    from pydantic_ai import Agent

    from src.agent.model_settings import agent_model_settings

    monkeypatch.setattr(agent_models, "key_for", lambda _: "synthetic-sdk-key")
    model = agent_models.build_model(
        {
            "provider": "deepseek",
            "protocol": "openai",
            "model_name": model_name,
            "base_url": "https://model.example",
        }
    )
    sdk = AsyncMock(
        return_value=ChatCompletion.model_validate(
            {
                "id": "synthetic-response",
                "object": "chat.completion",
                "created": 1,
                "model": model_name,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "synthetic"},
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )
    )
    monkeypatch.setattr(model.client.chat.completions, "create", sdk)

    def lookup() -> str:
        return "synthetic"

    asyncio.run(
        Agent(
            model,
            tools=[lookup],
            model_settings=lambda _: agent_model_settings(
                {"provider": "deepseek", "model_name": model_name}, {"kb"}
            ),
        ).run("Synthetic request.")
    )
    assert sdk.await_args.kwargs["model"] == model_name
    assert sdk.await_args.kwargs["tool_choice"] == "auto"
    assert model.profile["thinking_enabled_by_default"] is True
