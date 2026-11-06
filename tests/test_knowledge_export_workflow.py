"""文档报告在行投影被拒绝后，会改用有依据的文本导出重试。"""

import asyncio
from types import SimpleNamespace

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from src.agent import core_tools
from src.agent.agent import AgenticRunner, UserContext
from src.agent.grounding import EvidenceLedger, requested_capabilities
from src.agent.metric_results import prepare_business_tools
from src.config import settings

PROMPT = (
    "仅检索知识库文档，不查询业务数据库、不联网。"
    "将验收代码、项目负责人和测试预算这三项导出为 Markdown 文件。"
)
SOURCE = "本测试完全使用虚构资料。验收代码为DEMO_ACCEPTANCE。项目负责人为林青。测试预算为280元。"
REPORT = (
    "| 字段 | 内容 |\n| --- | --- |\n"
    "| 验收代码 | DEMO_ACCEPTANCE |\n| 项目负责人 | 林青 |\n| 测试预算 | 280元 |\n\n"
    "来源：[KB1] synthetic.md。"
)
ROWS = [{"验收代码": "DEMO_ACCEPTANCE", "项目负责人": "林青", "测试预算": 280}]


def test_actual_agent_retries_kb_rows_as_text_and_writes_only_verified_report(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "EXPORTS_DIR", str(tmp_path))
    retrieved = []

    async def search(*args, **kwargs):
        retrieved.append(kwargs)
        return [{"source": "synthetic.md", "content": SOURCE}]

    monkeypatch.setattr(core_tools, "search_knowledge", search)
    calls = 0

    def model(messages, info):
        nonlocal calls
        calls += 1
        tools = {tool.name for tool in info.function_tools}
        if calls == 1:
            assert tools == {"search_knowledge_documents"}
            return ModelResponse(
                [ToolCallPart("search_knowledge_documents", {"query": "验收代码 负责人 预算"})]
            )
        if calls == 2:
            assert tools == {"export_analysis"}
            return ModelResponse(
                [
                    ToolCallPart(
                        "export_analysis", {"title": "验收报告", "format": "md", "rows": ROWS}
                    )
                ]
            )
        if calls == 3:
            assert tools == {"export_analysis"}
            assert not list(tmp_path.rglob("*.md"))
            # 先是一次真实的 ModelRetry，随后同一工具再成功调用一次。
            assert "rows=None" in str(messages[-1])
            return ModelResponse(
                [
                    ToolCallPart(
                        "export_analysis",
                        {"title": "验收报告", "format": "md", "rows": None, "text": REPORT},
                    )
                ]
            )
        assert calls == 4 and tools == set()
        return ModelResponse([TextPart("报告已导出，可下载查看。")])

    runner = AgenticRunner()
    runner._agent = runner._build_agent(FunctionModel(model))

    async def run():
        with runner._agent.override(model=FunctionModel(model)):
            return await runner.run(
                PROMPT,
                UserContext(
                    user_id="kb-export",
                    email="kb-export@local.test",
                    tenant_id="",
                    capture_memory=False,
                ),
            )

    answer = asyncio.run(run())
    files = list(tmp_path.rglob("*.md"))
    assert len(files) == 1 and len(retrieved) == 1 and calls == 4
    content = files[0].read_text("utf8")
    assert content == "# 验收报告\n\n" + REPORT + "\n"
    assert "/api/nexrag-download/kb-export/" in answer
    assert "未完成" not in answer


def test_kb_export_keeps_numeric_checks_and_exact_database_row_binding():
    ledger = EvidenceLedger(prompt=PROMPT, required=requested_capabilities(PROMPT, False))
    ledger.add("kb", [{"content": SOURCE}])
    assert ledger.export_issues(ROWS, None)
    assert ledger.export_issues(None, REPORT) == []
    assert ledger.export_issues(None, REPORT.replace("280", "281"))
    ledger.add("db", [{"owner": "林青", "budget": 280}])
    assert ledger.export_issues([{"owner": "王明", "budget": 280}], None)


def test_export_tool_does_not_override_explicit_prohibition():
    ledger = EvidenceLedger(
        required={"kb", "export"}, prompt="只读取知识库文档，不查询数据库，不要导出。"
    )
    ledger.add("kb", [{"content": SOURCE}])
    definitions = [
        SimpleNamespace(name=name)
        for name in ("export_analysis", "recall_memory", "inspect_local_file")
    ]
    context = SimpleNamespace(deps=SimpleNamespace(ledger=ledger))
    assert asyncio.run(prepare_business_tools(context, definitions)) == []
