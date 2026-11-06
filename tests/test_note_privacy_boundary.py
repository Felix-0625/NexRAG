"""不另起应用数据库，直接测试已发布的 REST 与 socket 笔记授权。"""

import ast
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException


def shipped_functions(path, names, namespace):
    nodes = []
    for node in ast.parse(Path(path).read_text(encoding="utf8")).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            node.decorator_list = []
            node.returns = None
            for argument in node.args.args + node.args.kwonlyargs:
                argument.annotation = None
            node.args.defaults = [ast.Constant(None) for _ in node.args.defaults]
            nodes.append(node)
    module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    exec(compile(module, path, "exec"), namespace)
    return namespace


@pytest.mark.parametrize(
    "endpoint",
    [
        "get_note_by_id",
        "get_note_chat_by_id",
        "get_note_chats_by_id",
        "create_note_chat_by_id",
        "update_note_by_id",
        "update_note_access_by_id",
        "pin_note_by_id",
        "delete_note_by_id",
    ],
)
def test_private_note_rest_has_no_administrator_bypass(endpoint):
    grant = AsyncMock(return_value=False)
    ns = shipped_functions(
        "webui/backend/open_webui/routers/notes.py",
        {endpoint},
        {
            "HTTPException": HTTPException,
            "log": logging.getLogger(__name__),
            "status": SimpleNamespace(HTTP_403_FORBIDDEN=403),
            "ERROR_MESSAGES": SimpleNamespace(DEFAULT=lambda: "denied"),
            "admin_content_bypass": lambda user: False,
            "Notes": SimpleNamespace(
                get_note_by_id=AsyncMock(return_value=SimpleNamespace(id="note", user_id="owner"))
            ),
            "AccessGrants": SimpleNamespace(has_access=grant),
        },
    )
    kwargs = dict(request=None, id="note", user=SimpleNamespace(id="admin", role="admin"), db=None)
    if endpoint in {"update_note_by_id", "update_note_access_by_id"}:
        kwargs["form_data"] = SimpleNamespace()
    with pytest.raises(HTTPException) as error:
        asyncio.run(ns[endpoint](**kwargs))
    assert error.value.status_code == 403
    assert grant.await_args.kwargs["permission"] == (
        "write"
        if endpoint in {"update_note_by_id", "update_note_access_by_id", "delete_note_by_id"}
        else "read"
    )


def test_socket_rechecks_explicit_grants_and_prunes_revoked_readers():
    grant = AsyncMock(return_value=False)
    leave = AsyncMock()
    ns = shipped_functions(
        "webui/backend/open_webui/socket/main.py",
        {"normalize_document_id", "_note_document_access", "_prune_note_readers"},
        {
            "admin_content_bypass": lambda user: False,
            "Notes": SimpleNamespace(
                get_note_by_id=AsyncMock(return_value=SimpleNamespace(id="note", user_id="owner"))
            ),
            "AccessGrants": SimpleNamespace(has_access=grant),
            "get_session_ids_from_room": lambda room: ["sid"],
            "get_socket_session_user": AsyncMock(return_value={"id": "admin", "role": "admin"}),
            "sio": SimpleNamespace(leave_room=leave),
        },
    )
    admin = {"id": "admin", "role": "admin"}
    assert not asyncio.run(ns["_note_document_access"](admin, "note_note"))
    assert asyncio.run(ns["_note_document_access"]({"id": "owner"}, "note:note", "write"))
    grant.return_value = True
    assert asyncio.run(ns["_note_document_access"](admin, "note:note"))
    grant.return_value = False
    asyncio.run(ns["_prune_note_readers"]("doc_note:note", "note:note"))
    leave.assert_awaited_once_with("sid", "doc_note:note")
