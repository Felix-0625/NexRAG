"""运维接口（仅管理员）、记忆接口（仅本人）、反馈与追踪的 HTTP 边界。"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import jwt
import pytest
from fastapi.testclient import TestClient
from pydantic_ai.models.test import TestModel

from src.agent import agent as agent_module
from src.agent import core_tools
from src.agent.agent import AgentDeps, AgenticRunner, UserContext, drain_background
from src.config import settings
from src.main import app
from src.memory.models import MemoryCandidate, MemoryScope
from src.memory.runtime import set_memory_service
from src.memory.service import MemoryService
from src.memory.vectors import InMemoryVectorIndex
from src.observability.tracer import Tracer, TraceRecord

KEY = "test-service-key-with-32-characters"
SECRET = "a" * 32


def headers(sub="user-1", role="user", email="viewer@example.local"):
    claims = {
        "sub": sub,
        "email": email,
        "role": role,
        "iss": "open-webui",
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
    }
    return {
        "Authorization": f"Bearer {KEY}",
        "X-OpenWebUI-User-Jwt": jwt.encode(claims, SECRET, algorithm="HS256"),
    }


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "NEXRAG_SHARED_API_KEY", KEY)
    monkeypatch.setattr(settings, "NEXRAG_USER_JWT_SECRET", SECRET)
    return TestClient(app)


@pytest.fixture
def memory(isolated_ops, embedder, monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_MEMORY", True)
    monkeypatch.setattr(settings, "EMBEDDING_API_BASE_URL", "http://localhost:11434/v1")
    service = MemoryService(isolated_ops, InMemoryVectorIndex(), embedder)
    set_memory_service(service)
    return service


def test_ops_endpoints_require_admin(client):
    assert client.get("/v1/ops/overview", headers=headers()).status_code == 403
    assert client.get("/v1/ops/overview").status_code == 401
    response = client.get("/v1/ops/overview", headers=headers(role="admin"))
    assert response.status_code == 200
    assert set(response.json()["production"]) == {"prompt", "params"}


def test_admin_cannot_read_other_users_historical_trace_content(client, isolated_ops):
    trace = TraceRecord("employee-history", "employee", "t1", None, "PRIVATE_PROJECT_67000")
    Tracer(isolated_ops).record(trace, output_text="PRIVATE_ANSWER")
    response = client.get(
        "/v1/ops/traces/employee-history", headers=headers("admin", role="admin")
    ).json()
    assert response["trace"]["input_text"] is None
    assert response["trace"]["output_text"] is None
    listed = client.get("/v1/ops/traces", headers=headers("admin", role="admin")).json()
    assert listed[0]["input_preview"] is None


def test_memory_api_is_strictly_per_user(client, memory):
    pref = MemoryCandidate(
        "global", "preference", "user", "language", "Chinese", 0.9, "user_explicit", 6
    )
    asyncio.run(memory.write(pref, MemoryScope("user-1", "t1")))
    mine = client.get("/v1/memory", headers=headers("user-1")).json()["data"]
    assert [m["value"] for m in mine] == ["Chinese"]
    # 其他用户（即使是管理员）也看不到它，更无法删除。
    assert client.get("/v1/memory", headers=headers("user-2", role="admin")).json()["data"] == []
    assert (
        client.delete(
            f"/v1/memory/{mine[0]['id']}", headers=headers("user-2", role="admin")
        ).status_code
        == 404
    )
    assert client.delete(f"/v1/memory/{mine[0]['id']}", headers=headers("user-1")).json() == {
        "removed": 1
    }


def test_feedback_only_on_own_trace(client, isolated_ops):
    trace = TraceRecord("chatcmpl-x", "user-1", "t1", None, "q")
    Tracer(isolated_ops).record(trace, output_text="a")
    assert (
        client.post(
            "/v1/ops/feedback",
            json={"trace_id": "chatcmpl-x", "rating": -1},
            headers=headers("user-2"),
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/v1/ops/feedback",
            json={"trace_id": "chatcmpl-x", "rating": -1},
            headers=headers("user-1"),
        ).status_code
        == 200
    )
    score = isolated_ops.query_one("SELECT * FROM scores WHERE name = 'user_rating'")
    assert score["value"] == 0.0 and score["source"] == "user"
    assert (
        client.post(
            "/v1/ops/feedback",
            json={"trace_id": "chatcmpl-x", "rating": 1},
            headers=headers("user-1"),
        ).status_code
        == 200
    )
    assert isolated_ops.query("SELECT value FROM scores WHERE name = 'user_rating'") == [
        {"value": 1.0}
    ]


def test_agent_run_records_trace_and_ingests_memory(isolated_ops, memory, monkeypatch):
    async def fake_search(query, *, base_url, api_key, collection_name, client=None, top_k=3):
        return [{"source": "seller-metrics.md", "content": "price only", "score": 0.9}]

    async def completer(_system, _user):
        return (
            '{"items": [{"scope": "global", "kind": "preference", "subject": "user", "attribute": "answer language",'
            ' "value": "Chinese", "explicit": true, "confidence": 0.9, "importance": 6}]}'
        )

    memory.completer = completer
    monkeypatch.setattr(core_tools, "search_knowledge", fake_search)
    runner = AgenticRunner()
    runner._agent = runner._build_agent()
    context = UserContext(
        user_id="user-1", email="u@example.local", tenant_id="t1", chat_id="chat-9", trace_id="tr-1"
    )

    async def go():
        with runner._agent.override(
            model=TestModel(
                call_tools=["search_knowledge_documents"], custom_output_text="price only"
            )
        ):
            await runner.run("请用中文回答：收入口径是什么？", context)
        await drain_background()

        assert context.usage["prompt_tokens"] > 0
        assert context.usage["completion_tokens"] > 0
        assert context.usage["total_tokens"] == (
            context.usage["prompt_tokens"] + context.usage["completion_tokens"]
        )

    asyncio.run(go())
    trace = isolated_ops.query_one("SELECT * FROM traces WHERE id = 'tr-1'")
    assert (
        trace["chat_id"] == "chat-9" and trace["user_hash"] != "user-1" and trace["status"] == "ok"
    )
    observations = isolated_ops.query("SELECT name, type FROM observations WHERE trace_id = 'tr-1'")
    assert {"name": "search_knowledge_documents", "type": "retrieval"} in observations
    assert [r.value_text for r in memory.store.list_for_user("user-1")] == ["Chinese"]
    # 下一次运行会在指令中看到这条记忆。
    deps, _ = asyncio.run(runner._prepare("answer language", context))
    assert "Chinese" in deps.memory_block


def test_temporary_chat_does_not_store_trace_or_extract_memory(isolated_ops, memory, monkeypatch):
    context = UserContext(
        user_id="temporary-user",
        email="temp@example.local",
        tenant_id="t1",
        chat_id="temporary:browser-session",
    )
    runner = AgenticRunner()
    ingest = AsyncMock()
    monkeypatch.setattr(runner, "_ingest", ingest)
    runner._finish(
        TraceRecord(
            "temp-trace", context.user_id, context.tenant_id, context.chat_id, "private question"
        ),
        agent_module.AgentDeps(context=context),
        prompt="private question",
        output="answer",
        messages=(),
        status="ok",
    )
    assert isolated_ops.query_one("SELECT * FROM traces WHERE id = 'temp-trace'") is None
    ingest.assert_not_called()


def test_trace_redacts_pii(isolated_ops):
    Tracer(isolated_ops).record(
        TraceRecord("t-pii", "u", "t1", None, "my email is a.b@corp.com, phone 13812345678"),
        output_text="ok",
    )
    stored = isolated_ops.query_one("SELECT input_text FROM traces WHERE id = 't-pii'")[
        "input_text"
    ]
    assert "a.b@corp.com" not in stored and "13812345678" not in stored


def test_dynamic_guidance_reaches_the_model(isolated_ops):
    runner = AgenticRunner()
    agent = runner._build_agent()
    deps = AgentDeps(
        context=UserContext(user_id="u", email="e@x.local", tenant_id="t"),
        guidance="CUSTOM-GUIDANCE-v2",
        memory_block="<memory>x</memory>",
    )
    deps.evidence_calls = 1
    with agent.override(model=TestModel(call_tools=[])):
        result = asyncio.run(agent.run("hi", deps=deps))
    text = str(result.all_messages())
    assert (
        "CUSTOM-GUIDANCE-v2" in text
        and "<memory>x</memory>" in text
        and "Non-negotiable rules" in text
    )
