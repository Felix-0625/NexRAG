"""编写可执行工具与使用共享工具的权限是两回事。"""

import ast
import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

BACKEND = Path("webui/backend/open_webui")


@pytest.fixture
def native_module(monkeypatch):
    monkeypatch.setenv("NEXRAG_WORKSPACE", "true")
    spec = importlib.util.spec_from_file_location(
        "native_tool_identity", BACKEND / "utils/nexrag_identity.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setitem(sys.modules, "open_webui.utils.nexrag_identity", module)
    policy_spec = importlib.util.spec_from_file_location(
        "native_plugin_policy", BACKEND / "utils/nexrag_plugin_policy.py"
    )
    policy = importlib.util.module_from_spec(policy_spec)
    policy_spec.loader.exec_module(policy)
    monkeypatch.setitem(sys.modules, "open_webui.utils.nexrag_plugin_policy", policy)
    monkeypatch.delenv("NEXRAG_ALLOW_TRUSTED_PLUGINS", raising=False)
    return module


@pytest.mark.parametrize(
    "role,method,trusted,expected",
    [
        ("user", "POST", False, 403),
        ("user", "DELETE", True, 403),
        ("user", "GET", False, 200),
        ("admin", "POST", False, 403),
        ("admin", "POST", True, 200),
    ],
)
def test_actual_router_dependency_guards_all_executable_mutations(
    native_module, monkeypatch, role, method, trusted, expected
):
    if trusted:
        monkeypatch.setenv("NEXRAG_ALLOW_TRUSTED_PLUGINS", "true")
    source = ast.parse((BACKEND / "routers/tools.py").read_text("utf8"))
    guard = next(
        node
        for node in source.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "guard_native_tool_mutations"
    )
    router = next(
        node
        for node in source.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "router" for target in node.targets)
    )

    async def verified_user():
        return SimpleNamespace(role=role)

    namespace = {
        "Request": Request,
        "Depends": Depends,
        "HTTPException": HTTPException,
        "get_verified_user": verified_user,
        "APIRouter": APIRouter,
    }
    exec(
        compile(
            ast.Module(body=[guard, router], type_ignores=[]), "<actual-tool-router-policy>", "exec"
        ),
        namespace,
    )
    endpoint_called = []

    @namespace["router"].api_route("/create", methods=["GET", "POST", "DELETE"])
    async def inert_tool_endpoint():
        endpoint_called.append(True)
        return {"ok": True}

    app = FastAPI()
    app.include_router(namespace["router"])
    response = TestClient(app).request(method, "/create")
    assert response.status_code == expected
    assert bool(endpoint_called) == (expected == 200)


def test_old_employee_authored_module_is_quarantined_before_execution(native_module, monkeypatch):
    monkeypatch.setenv("NEXRAG_ALLOW_TRUSTED_PLUGINS", "true")
    source = ast.parse((BACKEND / "utils/plugin.py").read_text("utf8"))
    loader = next(
        node
        for node in source.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "load_tool_module_by_id"
    )
    owners = SimpleNamespace(get_user_by_id=AsyncMock(return_value=SimpleNamespace(role="user")))
    monkeypatch.setitem(sys.modules, "open_webui.models.users", SimpleNamespace(Users=owners))
    namespace = {
        "ENABLE_PLUGINS": True,
        "Tools": SimpleNamespace(
            get_tool_by_id=AsyncMock(
                return_value=SimpleNamespace(user_id="employee", content="must never execute")
            )
        ),
    }
    exec(
        compile(ast.Module(body=[loader], type_ignores=[]), "<actual-plugin-loader>", "exec"),
        namespace,
    )
    with pytest.raises(PermissionError, match="隔离"):
        asyncio.run(namespace["load_tool_module_by_id"]("old-employee-tool"))


@pytest.mark.parametrize("name", ["load_tool_module_by_id", "load_function_module_by_id"])
def test_native_default_blocks_source_before_loader_or_exec(native_module, name):
    source = ast.parse((BACKEND / "utils/plugin.py").read_text("utf8"))
    loader = next(
        node for node in source.body if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    )
    loader.args.args[0].annotation = None
    loader.args.args[1].annotation = None
    namespace = {}
    exec(
        compile(
            ast.Module(body=[loader], type_ignores=[]), "<actual-disabled-plugin-loader>", "exec"
        ),
        namespace,
    )
    with pytest.raises(PermissionError, match="默认禁止"):
        asyncio.run(
            namespace[name]("blocked-source", content='raise AssertionError("must never execute")')
        )


def test_native_default_does_not_install_plugin_dependencies(native_module):
    source = ast.parse((BACKEND / "utils/plugin.py").read_text("utf8"))
    installer = next(
        node
        for node in source.body
        if isinstance(node, ast.FunctionDef) and node.name == "install_frontmatter_requirements"
    )
    namespace = {}
    exec(
        compile(
            ast.Module(body=[installer], type_ignores=[]),
            "<actual-disabled-plugin-installer>",
            "exec",
        ),
        namespace,
    )
    # 未提供包安装器或全局配置：原生策略必须在
    # 评估或安装不受信任的依赖之前就返回。
    assert namespace["install_frontmatter_requirements"]("not-a-real-dependency") is None


def test_function_source_execution_routes_already_require_admin():
    source = ast.parse((BACKEND / "routers/functions.py").read_text("utf8"))
    executable = [
        node
        for node in source.body
        if isinstance(node, ast.AsyncFunctionDef)
        and any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "load_function_module_by_id"
            for call in ast.walk(node)
        )
    ]
    assert executable
    for node in executable:
        assert any(
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "Depends"
            and value.args
            and isinstance(value.args[0], ast.Name)
            and value.args[0].id == "get_admin_user"
            for value in node.args.defaults
        ), node.name
