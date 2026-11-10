"""Initial-chat background tasks must receive resolved model context."""

import ast
import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


def test_real_initial_title_context_can_run_actual_background_handler():
    # Load only shipped context expressions and handlers to avoid starting
    # another WebUI application and database in the core regression process.
    main = ast.parse(Path("webui/backend/open_webui/main.py").read_text("utf8"))
    assignment = next(
        node
        for node in ast.walk(main)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "title_ctx" for target in node.targets
        )
    )
    tasks = SimpleNamespace(
        TITLE_GENERATION="title", TAGS_GENERATION="tags", FOLLOW_UP_GENERATION="followup"
    )
    model = {"id": "nexrag-model-synthetic", "nexrag_agent": True}
    namespace = {
        "request": object(),
        "form_data": {"model": model["id"]},
        "user": object(),
        "model": model,
        "title_metadata": {"chat_id": "synthetic-chat", "message_id": "assistant"},
        "TASKS": tasks,
        "initial_title_generation": False,
        "event_emitter": AsyncMock(),
    }
    context = eval(
        compile(ast.Expression(assignment.value), "<initial-title-context>", "eval"), namespace
    )
    messages = [
        {"id": "user", "role": "user", "content": "Synthetic greeting", "model": model["id"]},
        {"id": "assistant", "role": "assistant", "content": "", "model": model["id"]},
    ]
    chats = SimpleNamespace(
        get_messages_map_by_chat_id=AsyncMock(return_value={item["id"]: item for item in messages}),
        update_chat_title_by_id=AsyncMock(),
    )
    review = AsyncMock()
    middleware = ast.parse(Path("webui/backend/open_webui/utils/middleware.py").read_text("utf8"))
    handler = next(
        node
        for node in middleware.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "background_tasks_handler"
    )
    ns = {
        "Chats": chats,
        "TASKS": tasks,
        "re": re,
        "is_saved_chat_id": lambda _: True,
        "get_message_list": lambda *_: messages,
        "get_last_user_message": lambda _: "Synthetic greeting",
        "review_memory_after_turn": review,
    }
    exec(compile(ast.Module(body=[handler], type_ignores=[]), "<background-handler>", "exec"), ns)
    asyncio.run(ns["background_tasks_handler"](context))
    chats.update_chat_title_by_id.assert_awaited_once_with("synthetic-chat", "Synthetic greeting")
    review.assert_awaited_once()
    assert review.await_args.kwargs["model"] is model
    # Initial title generation must not invent a completed assistant turn.
    assert review.await_args.kwargs["assistant_message"] == {}
