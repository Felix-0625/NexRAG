"""Enforce shipped chat authorization before mutations or disclosures."""

import ast
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException


def shipped_function(path, name, namespace):
    """Provide shipped function test data or behavior for this scenario."""
    tree = ast.parse(Path(path).read_text("utf8"))
    function = next(
        item
        for item in tree.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name
    )
    function.decorator_list = []
    function.returns = None
    for argument in [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]:
        argument.annotation = None
    function.args.defaults = [ast.Constant(None) for _ in function.args.defaults]
    function.args.kw_defaults = [
        ast.Constant(None) if item is not None else None for item in function.args.kw_defaults
    ]
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), path, "exec"
        ),
        namespace,
    )
    return namespace[name]


@pytest.fixture
def chat_boundary(monkeypatch):
    """Provide chat boundary test data or behavior for this scenario."""
    monkeypatch.setenv("NEXRAG_WORKSPACE", "true")
    namespace = {"os": os, "HTTPException": HTTPException}
    privacy_path = "webui/backend/open_webui/utils/nexrag_privacy.py"
    shipped_function(privacy_path, "private_workspace", namespace)
    shipped_function(privacy_path, "admin_content_bypass", namespace)
    chat = SimpleNamespace(user_id="employee", id="private-chat", meta={})
    mutated = AsyncMock(return_value=chat)

    async def owned(identifier, user_id, **kwargs):
        """Provide owned test data or behavior for this scenario."""
        return chat if user_id == chat.user_id else None

    namespace.update(
        Chats=SimpleNamespace(
            get_chat_by_id=AsyncMock(return_value=chat),
            get_chat_by_id_and_user_id=AsyncMock(side_effect=owned),
            is_chat_owner=AsyncMock(side_effect=lambda chat_id, user_id: user_id == "employee"),
            upsert_message_to_chat_by_id_and_message_id=mutated,
            delete_message_from_chat_by_id_and_message_id=mutated,
        ),
        ERROR_MESSAGES=SimpleNamespace(
            ACCESS_PROHIBITED="denied", NOT_FOUND="not found", DEFAULT=lambda: "not found"
        ),
        status=SimpleNamespace(
            HTTP_401_UNAUTHORIZED=401, HTTP_404_NOT_FOUND=404, HTTP_400_BAD_REQUEST=400
        ),
        ChatResponse=SimpleNamespace(model_validate=lambda item, **kwargs: item),
        get_event_emitter=AsyncMock(return_value=None),
        publish_event=AsyncMock(),
        EVENTS=SimpleNamespace(
            MESSAGE_UPDATED="update", MESSAGE_DELETED="delete", MESSAGE_EVENT_RECEIVED="event"
        ),
        AccessGrants=SimpleNamespace(
            set_access_grants=AsyncMock(), get_grants_by_resource=AsyncMock(return_value=[])
        ),
        Config=SimpleNamespace(get=AsyncMock(return_value={})),
        filter_allowed_access_grants=AsyncMock(return_value=[]),
        is_temporary_chat_id=lambda identifier: False,
    )
    return namespace, mutated


@pytest.mark.parametrize(
    "name",
    ["update_chat_message_by_id", "delete_chat_message_by_id", "send_chat_message_event_by_id"],
)
def test_admin_cannot_use_message_mutation_to_read_employee_private_chat(chat_boundary, name):
    namespace, mutated = chat_boundary
    function = shipped_function("webui/backend/open_webui/routers/chats.py", name, namespace)
    arguments = dict(
        request=SimpleNamespace(),
        id="private-chat",
        message_id="message",
        user=SimpleNamespace(id="admin", role="admin"),
        db=None,
    )
    if name != "delete_chat_message_by_id":
        arguments["form_data"] = SimpleNamespace(content="replacement", model_dump=lambda: {})
    with pytest.raises(HTTPException) as error:
        asyncio.run(function(**arguments))
    assert error.value.status_code == 401
    mutated.assert_not_called()
    namespace["get_event_emitter"].assert_not_called()
    namespace["publish_event"].assert_not_called()
    # The same shipped route still permits owners, including administrators' own chats.
    arguments["user"] = SimpleNamespace(id="employee", role="admin")
    result = asyncio.run(function(**arguments))
    assert result is not None


@pytest.mark.parametrize(
    "name", ["update_shared_chat_access_by_id", "get_shared_chat_access_by_id"]
)
def test_admin_cannot_self_grant_access_to_employee_chat(chat_boundary, name):
    namespace, _ = chat_boundary
    function = shipped_function("webui/backend/open_webui/routers/chats.py", name, namespace)
    arguments = dict(id="private-chat", user=SimpleNamespace(id="admin", role="admin"), db=None)
    if name == "update_shared_chat_access_by_id":
        arguments.update(request=SimpleNamespace(), form_data=SimpleNamespace(access_grants=[]))
    with pytest.raises(HTTPException) as error:
        asyncio.run(function(**arguments))
    assert error.value.status_code == 404
    namespace["AccessGrants"].set_access_grants.assert_not_called()
    arguments["user"] = SimpleNamespace(id="employee", role="admin")
    assert asyncio.run(function(**arguments)) is not None


def test_main_chat_completed_and_action_require_owner_even_for_admin(chat_boundary):
    namespace, _ = chat_boundary
    function = shipped_function(
        "webui/backend/open_webui/main.py", "verify_chat_ownership", namespace
    )
    with pytest.raises(HTTPException) as error:
        asyncio.run(function("private-chat", SimpleNamespace(id="admin", role="admin")))
    assert error.value.status_code == 404
    asyncio.run(function("private-chat", SimpleNamespace(id="employee", role="admin")))


@pytest.mark.parametrize("temporary", [False, True])
def test_task_listing_does_not_disclose_employee_task_ids(chat_boundary, temporary):
    namespace, _ = chat_boundary
    namespace.update(
        get_temporary_chat_session_id=lambda identifier: "socket" if temporary else None,
        get_user_id_from_session_pool=lambda socket: "employee",
        list_task_ids_by_item_id=AsyncMock(return_value=["private-task"]),
    )
    function = shipped_function(
        "webui/backend/open_webui/main.py", "list_tasks_by_chat_id_endpoint", namespace
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=None)))
    assert asyncio.run(
        function(request, "private-chat", SimpleNamespace(id="admin", role="admin"))
    ) == {"task_ids": []}
    namespace["list_task_ids_by_item_id"].assert_not_called()
    namespace["log"] = SimpleNamespace(debug=lambda *args: None)
    assert asyncio.run(
        function(request, "private-chat", SimpleNamespace(id="employee", role="admin"))
    ) == {"task_ids": ["private-task"]}
