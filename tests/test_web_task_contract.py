"""公开调研不得被误路由到被禁止的内部数据工具。"""

import asyncio
from types import SimpleNamespace

from src.agent.grounding import EvidenceLedger, requested_capabilities
from src.agent.metric_results import prepare_business_tools
from src.agent.task_intent import knowledge_only_request, prohibited_tools


def test_public_search_prohibition_cannot_become_document_only_obligation():
    prompt = (
        "请联网搜索 retrieval augmented generation，只查询这个公开主题，不读取知识库、"
        "不查询业务数据库、不调用其他外部工具、不导出。"
        "给出 RAG 的简短定义并引用 AWS 的真实网页链接。"
    )
    assert not knowledge_only_request(prompt)
    assert requested_capabilities(prompt, False) == {"web"}
    ledger = EvidenceLedger(required=requested_capabilities(prompt, False), prompt=prompt)
    tools = [
        SimpleNamespace(name=name)
        for name in ("search_web_engine", "search_knowledge_documents", "plan_analysis")
    ]
    allowed = asyncio.run(
        prepare_business_tools(SimpleNamespace(deps=SimpleNamespace(ledger=ledger)), tools)
    )
    assert [tool.name for tool in allowed] == ["search_web_engine"]


def test_explicit_negative_families_hide_tools_in_mixed_requests():
    blocked = prohibited_tools("查知识库，不查询业务数据库，不导出。")
    assert blocked == {
        "query_database",
        "query_business_metrics",
        "get_business_schema",
        "export_analysis",
    }
    assert not prohibited_tools("查询业务数据库和知识库并导出。")


def test_web_only_exposes_search_and_requires_actual_returned_citation():
    ledger = EvidenceLedger(
        required={"web"}, prompt="联网搜索公开主题，不读取知识库、不查询业务数据库、不导出。"
    )
    tools = [
        SimpleNamespace(name=name)
        for name in (
            "search_web_engine",
            "search_knowledge_documents",
            "query_database",
            "export_analysis",
        )
    ]
    allowed = asyncio.run(
        prepare_business_tools(SimpleNamespace(deps=SimpleNamespace(ledger=ledger)), tools)
    )
    assert [tool.name for tool in allowed] == ["search_web_engine"]
    ledger.add(
        "web",
        [
            {
                "title": "Public definition",
                "url": "https://www.example.org/definition",
                "snippet": "A public definition.",
            }
        ],
    )
    assert any(
        "Missing actual web citation" in item for item in ledger.issues("内部知识库没有资料。")
    )
    assert not ledger.issues("定义见[公开来源](https://www.example.org/definition)。")
