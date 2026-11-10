"""Optional search must not send queries to WebUI or claim absent services are available."""

import asyncio
from unittest.mock import Mock

import pytest

from src.agent.agent import AgenticRunner
from src.agent.dependencies import UserContext
from src.agent.grounding import EvidenceLedger, requested_capabilities
from src.agent.run_options import EvaluationRun
from src.config import settings
from src.midplatform.data_policy import save_account_policy, save_resource_policy
from src.midplatform.search_configuration import public_search_configuration, search_configuration


@pytest.mark.parametrize(
    "url", ["http://localhost:8080", "http://127.0.0.1:8080/", "http://[::1]:8080"]
)
def test_webui_aliases_are_not_search_services(monkeypatch, url):
    monkeypatch.setattr(settings, "ENABLE_WEB_SEARCH", True)
    monkeypatch.setattr(settings, "WEBUI_TOOL_BRIDGE_URL", "http://127.0.0.1:8080")
    monkeypatch.setattr(settings, "SEARXNG_URL", url)
    assert search_configuration()["status"] == "misconfigured"
    assert "endpoint" not in public_search_configuration()


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/search",
        "https://user:private-value@search.example",
        "https://search.example/?token=private-value",
        "http://[",
    ],
)
def test_invalid_addresses_never_echo_credentials(monkeypatch, url):
    monkeypatch.setattr(settings, "ENABLE_WEB_SEARCH", True)
    monkeypatch.setattr(settings, "SEARXNG_URL", url)
    result = search_configuration()
    assert result["status"] == "misconfigured"
    assert "private-value" not in str(result) and "endpoint" not in result


def test_configuration_is_not_a_health_claim(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_WEB_SEARCH", True)
    monkeypatch.setattr(settings, "SEARXNG_URL", "https://search.example/searxng/")
    result = search_configuration()
    assert (
        result["status"] == "configured" and result["endpoint"] == "https://search.example/searxng"
    )
    assert "endpoint" not in public_search_configuration()


@pytest.mark.parametrize("endpoint", ["", "http://localhost:8080"])
def test_explicit_search_missing_service_is_refused_before_model(monkeypatch, endpoint):
    monkeypatch.setattr(settings, "ENABLE_WEB_SEARCH", True)
    monkeypatch.setattr(settings, "SEARXNG_URL", endpoint)
    monkeypatch.setattr(settings, "WEBUI_TOOL_BRIDGE_URL", "http://127.0.0.1:8080")
    save_resource_policy("knowledge", "public", "public", False, True, False, "test")
    save_account_policy("search@local", {"external_search": True}, "test")
    context = UserContext(
        user_id="search", email="search@local", tenant_id="", knowledge_scopes=["public"]
    )
    runner = AgenticRunner()
    runner._agent = Mock()
    evaluation = EvaluationRun(guidance="", params={})
    answer = asyncio.run(runner.run("请联网搜索 Qdrant 官网", context, evaluation=evaluation))
    assert "搜索" in answer and "不会发送" in answer
    assert evaluation.status == "unavailable"
    runner._agent.run.assert_not_called()


def test_explicit_and_negative_search_require_actual_web_evidence():
    assert requested_capabilities("请联网搜索 Qdrant 官网", False) == {"web"}
    assert requested_capabilities("请联网搜索 Qdrant 官网文档", False) == {"web"}
    assert requested_capabilities("检索知识库，再联网搜索 Qdrant 官网文档", False) == {"kb", "web"}
    assert requested_capabilities("检索知识库，不联网搜索，不查询数据库", False) == {"kb"}
    ledger = EvidenceLedger(required={"web"})
    assert "联网搜索" in ledger.safe_answer()
    assert ledger.missing() == {"web"}
    ledger.add("web", [{"title": "Public result", "url": "https://example.com"}])
    assert not ledger.missing()
