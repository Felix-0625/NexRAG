"""防止固定版本的 WebUI 集成丢失用户的业务任务。"""

import ast
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.mark.parametrize("system_context", [False, True])
@pytest.mark.parametrize("template_has_query", [False, True])
def test_retrieval_context_preserves_original_question(system_context, template_has_query):
    source = Path(__file__).resolve().parents[1] / "webui/backend/open_webui/utils/middleware.py"
    if not source.exists():
        pytest.skip("Run scripts/prepare-webui.ps1 to install the pinned WebUI integration")
    tree = ast.parse(source.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "apply_source_context_to_messages"
    )
    module = ast.Module(
        body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            function,
        ],
        type_ignores=[],
    )
    question = "查询全部订单、汇总金额并导出 CSV。"
    reference = "企业制度：折扣上限 6%。"
    rendered = reference + ("\n" + question if template_has_query else "")
    capture = Mock(return_value=["updated"])
    scope = {
        "get_source_context": Mock(return_value=reference),
        "Config": type("Config", (), {"get": AsyncMock(return_value="template")}),
        "rag_template": AsyncMock(return_value=rendered),
        "RAG_SYSTEM_CONTEXT": system_context,
        "add_or_update_system_message": capture,
        "add_or_update_user_message": capture,
    }
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), scope)
    asyncio.run(
        scope["apply_source_context_to_messages"](
            None,
            [{"role": "user", "content": question}],
            [{"source": "policy"}],
            question,
        )
    )
    injected = capture.call_args.args[0]
    assert reference in injected
    assert injected.count(question) == 1
