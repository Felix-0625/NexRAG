"""Exercise the shipped restore flow with real async SQLite transactions and local files."""

import ast
import asyncio
import hashlib
import json
import logging
import re
import shutil
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base


def load_shipped(path, namespace, classes=None):
    """Load shipped."""
    tree = ast.parse(Path(path).read_text(encoding="utf8"))
    tree.body = [
        node
        for node in tree.body
        if not isinstance(node, (ast.Import, ast.ImportFrom))
        and (classes is None or isinstance(node, ast.ClassDef) and node.name in classes)
    ]
    exec(compile(tree, path, "exec"), namespace)


@pytest.fixture
def restore_store(tmp_path):
    """Provide a published revision and a newer live version for restore tests."""
    schema = _restore_schema()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'metadata.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    File, Knowledge, Link = (schema[name] for name in ("File", "Knowledge", "KnowledgeFile"))
    live_path = tmp_path / "live.txt"
    live_path.write_text("VERSION_ONE_671", encoding="utf8")
    policy = {"nexrag_policy": {"classification": "internal", "external_models": False}}

    @asynccontextmanager
    async def database():
        """Database."""
        async with sessions() as session:
            yield session

    async def knowledge(identifier):
        """Knowledge."""
        async with sessions() as session:
            return await session.get(Knowledge, identifier)

    async def files(identifier):
        """Files."""
        async with sessions() as session:
            return list(
                (
                    await session.scalars(
                        sa.select(File)
                        .join(Link, Link.file_id == File.id)
                        .where(Link.knowledge_id == identifier)
                    )
                ).all()
            )

    Storage = _restore_storage(tmp_path)
    indexed, removed = {}, []

    def index(path, filename, file_id, generation, owner):
        """Index."""
        indexed[(generation, file_id)] = Path(path).read_text(encoding="utf8")

    def remove(file_id, generation):
        """Remove."""
        removed.append((generation, file_id))
        indexed.pop((generation, file_id), None)

    ns = dict(
        asyncio=asyncio,
        hashlib=hashlib,
        json=json,
        re=re,
        shutil=shutil,
        sqlite3=sqlite3,
        time=time,
        uuid=uuid,
        contextmanager=contextmanager,
        Path=Path,
        DATA_DIR=tmp_path,
        Storage=Storage,
        Knowledges=SimpleNamespace(get_knowledge_by_id=knowledge, get_files_by_id=files),
    )
    load_shipped("webui/backend/open_webui/utils/nexrag_versions.py", ns)
    ns.update(
        get_async_db=database,
        File=File,
        Knowledge=Knowledge,
        KnowledgeFile=Link,
        select=sa.select,
        delete=sa.delete,
        HTTPException=HTTPException,
        enabled=lambda: True,
        extract_document=lambda path, name: Path(path).read_text("utf8"),
        index_document=index,
        remove_document=remove,
        logging=logging,
    )
    load_shipped("webui/backend/open_webui/utils/nexrag_restore.py", ns)

    revision = asyncio.run(_prepare_live_revision(engine, sessions, schema, live_path, policy, ns))
    yield SimpleNamespace(
        ns=ns,
        sessions=sessions,
        schema=schema,
        indexed=indexed,
        removed=removed,
        revision=revision,
        policy=policy,
        path=live_path,
    )
    asyncio.run(engine.dispose())


def test_restore_publishes_files_index_and_audit_together_without_restoring_acl(restore_store):
    """Test restore publishes files index and audit together without restoring acl."""
    store = restore_store

    async def run():
        """Run."""
        before = await store.ns["Knowledges"].get_knowledge_by_id("kb")
        result = await store.ns["restore_revision"](
            before, store.revision, SimpleNamespace(id="owner")
        )
        current = await store.ns["Knowledges"].get_knowledge_by_id("kb")
        files = await store.ns["Knowledges"].get_files_by_id("kb")
        assert current.name == "V1"
        assert current.meta["nexrag_policy"] == store.policy["nexrag_policy"]
        assert len(files) == 1 and files[0].id != "original"
        assert files[0].data["content"] == "VERSION_ONE_671"
        assert store.indexed[(current.meta["nexrag_generation"], files[0].id)] == "VERSION_ONE_671"
        assert store.path.read_text("utf8") == "VERSION_TWO_982"
        history = store.ns["revision_history"](current)
        assert history[0]["id"] == result["id"]
        assert history[0]["restored_from"] == store.revision
        assert any(item["body"]["name"] == "V2" for item in history)
        generations = {"Knowledges": store.ns["Knowledges"], "re": re}
        load_shipped("webui/backend/open_webui/utils/nexrag_generations.py", generations)
        assert await generations["library_target"]("kb") == current.meta["nexrag_generation"]

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["index", "database"])
def test_restore_failure_keeps_live_content_and_relations_and_cleans_staging(
    restore_store, failure
):
    """Test restore failure keeps live content and relations and cleans staging."""
    store = restore_store
    if failure == "index":

        def reject(*args):
            """Reject."""
            raise RuntimeError("injected indexing failure")

        store.ns["index_document"] = reject
    else:

        def reject(mapper, connection, target):
            """Reject."""
            if target.id != "original-link":
                raise RuntimeError("injected final transaction failure")

        sa.event.listen(store.schema["KnowledgeFile"], "before_insert", reject)

    async def run():
        """Run."""
        before = await store.ns["Knowledges"].get_knowledge_by_id("kb")
        with pytest.raises(RuntimeError, match="injected"):
            await store.ns["restore_revision"](before, store.revision, SimpleNamespace(id="owner"))
        current = await store.ns["Knowledges"].get_knowledge_by_id("kb")
        files = await store.ns["Knowledges"].get_files_by_id("kb")
        assert current.name == "V2" and current.meta == store.policy
        assert [item.id for item in files] == ["original"]
        assert files[0].data["content"] == "VERSION_TWO_982"
        assert store.path.exists() and not store.indexed and store.removed
        async with store.sessions() as session:
            assert (
                await session.scalar(sa.select(sa.func.count()).select_from(store.schema["File"]))
                == 1
            )

    asyncio.run(run())


def test_non_owner_cannot_restore_or_stage_files(restore_store):
    """Test non owner cannot restore or stage files."""
    store = restore_store

    async def run():
        """Run."""
        before = await store.ns["Knowledges"].get_knowledge_by_id("kb")
        with pytest.raises(HTTPException) as error:
            await store.ns["restore_revision"](before, store.revision, SimpleNamespace(id="admin"))
        assert error.value.status_code == 403
        assert not store.indexed and not store.removed

    asyncio.run(run())


async def _seed_restore_store(engine, sessions, schema, live_path, policy):
    File, Knowledge, Link = (schema[name] for name in ("File", "Knowledge", "KnowledgeFile"))
    async with engine.begin() as connection:
        await connection.run_sync(schema["Base"].metadata.create_all)
    async with sessions.begin() as session:
        session.add(
            Knowledge(id="kb", user_id="owner", name="V1", description="synthetic", meta=policy)
        )
        session.add(
            File(
                id="original",
                user_id="owner",
                filename="policy.txt",
                path=str(live_path),
                data={"content": "VERSION_ONE_671"},
            )
        )
        session.add(
            Link(
                id="original-link",
                knowledge_id="kb",
                file_id="original",
                user_id="owner",
                created_at=1,
                updated_at=1,
            )
        )


def _restore_storage(tmp_path):
    class Storage:
        get_file = staticmethod(str)

        @staticmethod
        def upload_file(stream, name, tags):
            """Upload file."""
            body = stream.read()
            path = tmp_path / name
            path.write_bytes(body)
            return body, str(path)

        @staticmethod
        def delete_file(path):
            """Delete file."""
            Path(path).unlink()

    return Storage


def _restore_schema():
    schema = {**vars(sa), "Base": declarative_base()}
    load_shipped("webui/backend/open_webui/models/files.py", schema, {"File"})
    load_shipped(
        "webui/backend/open_webui/models/knowledge.py",
        schema,
        {"Knowledge", "KnowledgeFile", "KnowledgeDirectory"},
    )
    return schema


async def _prepare_live_revision(engine, sessions, schema, live_path, policy, ns):
    File, Knowledge = schema["File"], schema["Knowledge"]
    """Prepare."""
    await _seed_restore_store(engine, sessions, schema, live_path, policy)
    revision = await ns["record_revision"]("kb", "owner", "published V1", force=True)
    live_path.write_text("VERSION_TWO_982", encoding="utf8")
    async with sessions.begin() as session:
        current = await session.get(Knowledge, "kb")
        current.name = "V2"
        document = await session.get(File, "original")
        document.data = {"content": "VERSION_TWO_982"}
    return revision
