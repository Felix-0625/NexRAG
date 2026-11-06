"""私有内容对应用管理员拒绝开放，也不进入全局学习。"""

import ast
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic_ai.messages import ModelRequest, ToolReturnPart

from src.observability.tracer import Tracer, TraceRecord


def test_actual_webui_knowledge_acl_admin_has_no_bypass(monkeypatch):
    # 用一个很小的模拟模型边界执行已发布的函数，避免在本进程中
    # 再导入第二个 FastAPI 应用的数据库与配置。
    source = Path("webui/backend/open_webui/utils/nexrag_privacy.py").read_text("utf8")
    names = {
        "private_workspace",
        "admin_content_bypass",
        "require_knowledge",
        "protect_private_response",
    }
    tree = ast.Module(
        body=[
            n
            for n in ast.parse(source).body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names
        ],
        type_ignores=[],
    )
    grant = AsyncMock(return_value=False)
    ns = {
        "os": os,
        "HTTPException": HTTPException,
        "Knowledges": SimpleNamespace(
            get_knowledge_by_id=AsyncMock(return_value=SimpleNamespace(user_id="employee"))
        ),
        "AccessGrants": SimpleNamespace(has_access=grant),
    }
    exec(compile(tree, "<shipped-privacy-helper>", "exec"), ns)
    monkeypatch.setenv("NEXRAG_WORKSPACE", "true")
    admin = SimpleNamespace(id="admin", role="admin")
    assert not ns["admin_content_bypass"](admin)
    with pytest.raises(HTTPException) as error:
        asyncio.run(ns["require_knowledge"]("private-kb", admin))
    assert error.value.status_code == 404
    owner = SimpleNamespace(id="employee", role="user")
    assert asyncio.run(ns["require_knowledge"]("private-kb", owner)).user_id == "employee"
    grant.return_value = True
    assert asyncio.run(ns["require_knowledge"]("shared-kb", admin))
    assert grant.call_args.args[-1] == "read"
    response = SimpleNamespace(headers={})
    ns["protect_private_response"](
        SimpleNamespace(url=SimpleNamespace(path="/api/v1/files/id/content")), response
    )
    assert "no-store" in response.headers["Cache-Control"]
    assert "Cookie" in response.headers["Vary"]


def test_private_trace_scrubs_tool_results_and_blocks_global_learning(isolated_ops, embedder):
    trace = TraceRecord(
        "private-trace",
        "employee",
        "ui-a",
        None,
        "PRIVATE_BUDGET_32000",
        artifact_versions={"content_scope": "private"},
    )
    Tracer(isolated_ops).record(
        trace,
        output_text="secret",
        messages=[
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        "search_knowledge_documents",
                        {"content": "PRIVATE_BUDGET_32000"},
                        tool_call_id="x",
                    )
                ]
            )
        ],
    )
    saved = isolated_ops.query_one("SELECT * FROM traces WHERE id=?", (trace.trace_id,))
    assert saved["input_text"] is None and saved["output_text"] is None
    observation = isolated_ops.query_one(
        "SELECT * FROM observations WHERE trace_id=?", (trace.trace_id,)
    )
    assert observation["input_summary"] is None and observation["output_summary"] is None
