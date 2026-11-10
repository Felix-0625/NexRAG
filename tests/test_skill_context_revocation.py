"""Replayed skills require persisted grants, not browser selections."""

import ast
import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import JSON, Column, String, Text, cast, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base


def test_shipped_skill_context_rechecks_persisted_ids_and_preserves_other_metadata(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("NEXRAG_WORKSPACE", "true")
    base = declarative_base()

    class Chat(base):
        """Provide Chat test data or behavior for this scenario."""

        __tablename__ = "chat"
        id = Column(String, primary_key=True)
        user_id = Column(String)
        meta = Column(JSON)

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'receipts.db'}")
    sessions = async_sessionmaker(engine)

    @asynccontextmanager
    async def context():
        """Provide context test data or behavior for this scenario."""
        async with sessions() as db:
            yield db

    skills = SimpleNamespace(
        get_skills=AsyncMock(return_value=[SimpleNamespace(id="shared-skill", is_active=True)])
    )
    ns = {
        "os": os,
        "HTTPException": HTTPException,
        "Chat": Chat,
        "Skills": skills,
        "get_async_db_context": context,
        "is_saved_chat_id": lambda value: (
            bool(value) and not value.startswith(("temporary:", "local:", "channel:"))
        ),
        "Text": Text,
        "cast": cast,
        "select": select,
        "update": update,
    }
    path = Path("webui/backend/open_webui/utils/nexrag_skill_context.py")
    tree = ast.parse(path.read_text(encoding="utf8"))
    tree.body = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)]
    exec(compile(tree, str(path), "exec"), ns)
    guard = ns["guard_skill_context"]
    owner = SimpleNamespace(id="alice")

    async def run():
        """Run this scenario in an isolated asynchronous context."""
        try:
            async with engine.begin() as connection:
                await connection.run_sync(base.metadata.create_all)
            async with sessions() as db:
                db.add(Chat(id="conversation", user_id="alice", meta={"tags": ["keep"]}))
                await db.commit()
            await guard(owner, "conversation", ["shared-skill"])
            # Deselecting a skill does not remove its context authorization requirement.
            await guard(owner, "conversation", [])
            async with sessions() as db:
                row = await db.get(Chat, "conversation")
                assert row.meta == {
                    "tags": ["keep"],
                    "nexrag_skill_context_ids": ["shared-skill"],
                }
            skills.get_skills.return_value = []
            with pytest.raises(HTTPException) as revoked:
                await guard(owner, "conversation", [])
            assert revoked.value.status_code == 403
            # Temporary requests cannot bypass direct-selection checks either.
            with pytest.raises(HTTPException) as direct:
                await guard(owner, "temporary:test", ["shared-skill"])
            assert direct.value.status_code == 403
            skills.get_skills.return_value = [SimpleNamespace(id="shared-skill", is_active=False)]
            with pytest.raises(HTTPException):
                await guard(owner, "conversation", [])
            skills.get_skills.return_value = [SimpleNamespace(id="shared-skill", is_active=True)]
            await guard(owner, "conversation", [])
            with pytest.raises(HTTPException) as foreign:
                await guard(SimpleNamespace(id="bob"), "conversation", ["shared-skill"])
            assert foreign.value.status_code == 409
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_skill_guard_runs_before_content_is_injected():
    source = Path("webui/backend/open_webui/utils/middleware.py").read_text(encoding="utf8")
    assert source.index("await guard_skill_context(") < source.index(
        "for skill in available_skills:"
    )
