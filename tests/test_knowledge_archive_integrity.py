"""不另起 WebUI 应用，直接验证已发布的版本存储与 ZIP 端点。"""

import ast
import asyncio
import hashlib
import io
import json
import re
import shutil
import sqlite3
import time
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.responses import Response


@pytest.fixture
def archive(tmp_path):
    source = Path("webui/backend/open_webui/utils/nexrag_versions.py").read_text("utf8")
    tree = ast.parse(source)
    tree.body = [node for node in tree.body if not isinstance(node, (ast.Import, ast.ImportFrom))]
    knowledge = SimpleNamespace(id="kb", name="Team policy", description="Synthetic", meta={})
    models = SimpleNamespace(
        get_knowledge_by_id=AsyncMock(return_value=knowledge),
        get_files_by_id=AsyncMock(return_value=[]),
    )
    namespace = dict(
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
        Knowledges=models,
        Storage=SimpleNamespace(get_file=lambda path: path),
    )
    exec(compile(tree, "<shipped-knowledge-archive>", "exec"), namespace)
    return namespace


def download_endpoint(archive):
    source = Path("webui/backend/open_webui/routers/nexrag_workspace.py").read_text("utf8")
    node = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "download_revision"
    )
    node.decorator_list = []
    namespace = {
        **archive,
        "verify_knowledge": AsyncMock(
            return_value=archive["Knowledges"].get_knowledge_by_id.return_value
        ),
        "Depends": lambda dependency: None,
        "get_verified_user": lambda: None,
        "HTTPException": HTTPException,
        "Response": Response,
        "io": io,
        "zipfile": zipfile,
    }
    exec(
        compile(ast.Module(body=[node], type_ignores=[]), "<shipped-version-download>", "exec"),
        namespace,
    )
    return namespace


def publish_files(archive, tmp_path):
    first = tmp_path / "one.txt"
    second = tmp_path / "two.txt"
    first.write_text("policy version one", encoding="utf8")
    second.write_text("policy approval", encoding="utf8")
    archive["Knowledges"].get_files_by_id.return_value = [
        SimpleNamespace(id="one", filename=first.name, path=str(first), data={}),
        SimpleNamespace(id="two", filename=second.name, path=str(second), data={}),
    ]
    return asyncio.run(archive["record_revision"]("kb", "owner", "V1", force=True))


def test_download_contains_all_original_bytes_and_keeps_older_revision(archive, tmp_path):
    revision = publish_files(archive, tmp_path)
    (tmp_path / "one.txt").write_text("policy version two", encoding="utf8")
    newer = asyncio.run(archive["record_revision"]("kb", "owner", "V2", force=True))
    assert newer != revision
    endpoint = download_endpoint(archive)
    response = asyncio.run(endpoint["download_revision"]("kb", revision, "reader"))
    with zipfile.ZipFile(io.BytesIO(response.body)) as result:
        manifest = json.loads(result.read("manifest.json"))
        assert len(manifest["files"]) == 2
        assert result.read("files/1-one.txt") == b"policy version one"
        assert result.read("files/2-two.txt") == b"policy approval"
    assert len(archive["list_revisions"]("kb")) == 2


@pytest.mark.parametrize("damage", ["missing", "same_size_corruption"])
def test_download_refuses_partial_or_corrupt_version(archive, tmp_path, damage):
    revision = publish_files(archive, tmp_path)
    body = archive["list_revisions"]("kb")[0]["body"]
    blob = archive["root"]() / "objects" / body["files"][1]["blob"]
    if damage == "missing":
        blob.unlink()
    else:
        blob.write_bytes(b"x" * blob.stat().st_size)
    endpoint = download_endpoint(archive)
    with pytest.raises(HTTPException) as error:
        asyncio.run(endpoint["download_revision"]("kb", revision, "reader"))
    assert error.value.status_code == 409


def test_archive_rejects_unavailable_source_without_publishing_partial_revision(archive, tmp_path):
    publish_files(archive, tmp_path)
    archive["Knowledges"].get_files_by_id.return_value.append(
        SimpleNamespace(id="missing", filename="missing.txt", path=None, data={})
    )
    with pytest.raises(archive["ArchiveIntegrityError"]):
        asyncio.run(archive["record_revision"]("kb", "owner", "broken", force=True))
    assert len(archive["list_revisions"]("kb")) == 1


def test_invalid_blob_identifier_cannot_reference_outside_archive(archive):
    with pytest.raises(archive["ArchiveIntegrityError"]):
        archive["verified_archive_files"]({"files": [{"blob": "../../other-file", "size": 1}]})


def test_live_authorization_is_checked_before_reading_archive(archive):
    endpoint = download_endpoint(archive)
    endpoint["verify_knowledge"].side_effect = HTTPException(404, "Not authorized")
    with pytest.raises(HTTPException) as error:
        asyncio.run(endpoint["download_revision"]("private-kb", "revision", "admin"))
    assert error.value.status_code == 404
    assert not (Path(archive["DATA_DIR"]) / "nexrag-versions").exists()
