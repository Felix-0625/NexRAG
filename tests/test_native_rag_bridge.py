"""不启动元数据库，直接测试生成出的 WebUI 桥接代码。"""

import ast
import asyncio
import sqlite3
import types
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    folder = Path(__file__).resolve().parents[1] / "webui/backend/open_webui/utils"
    if not (folder / "nexrag_native_rag.py").exists():
        pytest.skip("Prepare the pinned WebUI integration first")
    registry = {"DATA_DIR": tmp_path}
    source = ast.parse((folder / "nexrag_ingestion.py").read_text(encoding="utf8"))
    source.body = [
        item
        for item in source.body
        if not isinstance(item, ast.ImportFrom) or not item.module.startswith("open_webui")
    ]
    exec(compile(source, str(folder / "nexrag_ingestion.py"), "exec"), registry)
    scope = {
        "REQUESTS_VERIFY": True,
        **{name: registry[name] for name in ("scoped_id", "remember", "document_ids")},
    }
    source = ast.parse((folder / "nexrag_native_rag.py").read_text(encoding="utf8"))
    source.body = [
        item
        for item in source.body
        if not isinstance(item, ast.ImportFrom) or not item.module.startswith("open_webui")
    ]
    exec(compile(source, str(folder / "nexrag_native_rag.py"), "exec"), scope)
    monkeypatch.setenv("NEXRAG_WORKSPACE", "true")
    monkeypatch.setenv("INGESTION_API_BASE_URL", "http://127.0.0.1:19752")
    monkeypatch.setenv("INGESTION_API_KEY", "k" * 40)
    return scope


@pytest.mark.parametrize(
    "endpoint",
    ["https://127.0.0.1:19752", "http://example.com", "http://user:password@localhost:19752"],
)
def test_bridge_rejects_non_native_endpoints(bridge, monkeypatch, endpoint):
    monkeypatch.setenv("INGESTION_API_BASE_URL", endpoint)
    with pytest.raises(RuntimeError, match="loopback"):
        bridge["connection_settings"]()


def test_index_and_delete_are_library_scoped(bridge, tmp_path, monkeypatch):
    source = tmp_path / "policy.txt"
    source.write_text("企业折扣上限6%。", encoding="utf8")
    calls = []

    def ingest(url, **kwargs):
        calls.append((url, kwargs["data"].copy(), kwargs["files"]["file"][1].read()))
        assert kwargs["allow_redirects"] is False
        return Mock(status_code=200, json=lambda: {"status": True})

    http = bridge["requests"]
    monkeypatch.setattr(http, "put", ingest)
    delete = Mock(return_value=Mock(status_code=200, json=lambda: {"status": True}))
    monkeypatch.setattr(http, "delete", delete)
    for library in ("employee-private", "enterprise"):
        bridge["index_document"](source, "policy.txt", "same-file", library, "employee")
    assert calls[0][1]["file_id"] != calls[1][1]["file_id"]
    assert calls[0][2].decode("utf8") == "企业折扣上限6%。"
    assert calls[0][1]["collection_name"] == "employee-private"
    bridge["remove_document"]("same-file", "employee-private")
    assert delete.call_count == 1
    assert delete.call_args.args[0].endswith(calls[0][1]["file_id"])
    with sqlite3.connect(tmp_path / "nexrag-ingestion.db") as registry:
        assert registry.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2
        assert [row[1] for row in registry.execute("PRAGMA table_info(documents)")] == [
            "file_id",
            "collection_name",
            "scoped_id",
        ]


@pytest.mark.parametrize("status", [302, 401, 500])
def test_failed_index_is_not_recorded_as_success(bridge, tmp_path, monkeypatch, status):
    source = tmp_path / "policy.txt"
    source.write_text("policy", encoding="utf8")
    monkeypatch.setattr(
        bridge["requests"],
        "put",
        Mock(return_value=Mock(status_code=status, json=lambda: {"status": True})),
    )
    with pytest.raises(RuntimeError, match=str(status)):
        bridge["index_document"](source, "policy.txt", "failed-file", "private", "employee")
    assert not (tmp_path / "nexrag-ingestion.db").exists()


def test_content_update_uses_new_text_before_completion(bridge, monkeypatch):
    """来源被编辑后，必须先替换向量，再把元数据标记为完成。"""
    import sys
    from contextlib import asynccontextmanager

    files = types.SimpleNamespace(
        **{
            name: AsyncMock()
            for name in (
                "update_file_data_by_id",
                "update_file_hash_by_id",
                "update_file_metadata_by_id",
            )
        }
    )
    events = types.SimpleNamespace(RETRIEVAL_CONTENT_PROCESSED="processed")

    @asynccontextmanager
    async def session():
        yield "fresh-session"

    for name, attributes in {
        "open_webui.events": {"EVENTS": events, "publish_event": AsyncMock()},
        "open_webui.internal.db": {"get_async_db": session},
        "open_webui.models.files": {"Files": files},
        "open_webui.storage.provider": {"Storage": object()},
        "open_webui.utils.misc": {"calculate_sha256_string": lambda value: str(len(value))},
        "open_webui.utils.nexrag_generations": {
            "library_target": AsyncMock(side_effect=lambda value: value)
        },
    }.items():
        monkeypatch.setitem(sys.modules, name, types.SimpleNamespace(**attributes))
    db = types.SimpleNamespace(commit=AsyncMock())
    observed = []

    def index(path, filename, identifier, library, owner, *, edited):
        assert db.commit.await_count == 1
        assert files.update_file_data_by_id.await_count == 0
        observed.append((Path(path).read_text(encoding="utf8"), library, edited, path))

    bridge["index_document"] = index
    file = types.SimpleNamespace(
        id="document",
        filename="policy.pdf",
        path="old.pdf",
        user_id="employee",
        data={"content": "旧版本"},
    )
    result = asyncio.run(
        bridge["process_upload"](object(), file, "private", "新版本<br/>6%", object(), db)
    )
    assert observed[0][:3] == ("新版本\n6%", "private", True)
    assert not Path(observed[0][3]).exists()
    assert result["status"] is True
    assert files.update_file_data_by_id.call_args.args[1]["content"] == "新版本\n6%"
