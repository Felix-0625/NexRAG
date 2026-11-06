"""智能体生成的导出文件：沙箱、格式、按用户隔离以及 API。"""

from __future__ import annotations

import os
import subprocess
import time

import jwt
import pytest
from fastapi.testclient import TestClient

from src.agent.exports import ExportSecurityError, export_analysis, list_exports
from src.config import settings
from src.main import app

KEY = "test-service-key-with-32-characters"
SECRET = "a" * 32


@pytest.fixture
def exports_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "EXPORTS_DIR", str(tmp_path))
    return tmp_path


def test_csv_export_round_trips(exports_dir):
    result = export_analysis(
        user_id="alice",
        chat_id="chat-1",
        title="Seller revenue",
        fmt="csv",
        rows=[{"seller": "A", "revenue": 100}],
    )
    path = exports_dir / result["relative_path"]
    assert path.exists() and path.read_text(encoding="utf-8").splitlines()[0] == "seller,revenue"
    assert result["relative_path"].startswith("alice/chat-1/")


def test_markdown_export_renders_table_and_text(exports_dir):
    result = export_analysis(
        user_id="alice",
        chat_id=None,
        title="Report",
        fmt="md",
        text="Summary line.",
        rows=[{"a": 1, "b": 2}],
    )
    content = (exports_dir / result["relative_path"]).read_text(encoding="utf-8")
    assert "# Report" in content and "Summary line." in content and "| a | b |" in content
    assert result["relative_path"].startswith("alice/_/")


def test_json_and_txt_formats(exports_dir):
    j = export_analysis(user_id="bob", chat_id="c", title="t", fmt="json", rows=[{"x": 1}])
    assert '"x": 1' in (exports_dir / j["relative_path"]).read_text(encoding="utf-8")
    t = export_analysis(user_id="bob", chat_id="c", title="t", fmt="txt", text="plain notes")
    assert (exports_dir / t["relative_path"]).read_text(encoding="utf-8") == "plain notes"


def test_unknown_format_and_empty_payload_are_rejected(exports_dir):
    with pytest.raises(ValueError, match="Unsupported export format"):
        export_analysis(user_id="alice", chat_id="c", title="t", fmt="exe", text="x")
    with pytest.raises(ValueError, match="Nothing to export"):
        export_analysis(user_id="alice", chat_id="c", title="t", fmt="md")


def test_oversized_export_is_rejected(exports_dir, monkeypatch):
    monkeypatch.setattr("src.agent.exports.MAX_EXPORT_BYTES", 10)
    with pytest.raises(ValueError, match="exceeds"):
        export_analysis(user_id="alice", chat_id="c", title="t", fmt="txt", text="x" * 100)


def test_user_id_cannot_escape_the_exports_root(exports_dir):
    with pytest.raises(ExportSecurityError):
        export_analysis(user_id="../../etc", chat_id="c", title="t", fmt="txt", text="x")


@pytest.mark.parametrize("user_id", [".", "..", "../bob", "alice/bob", ""])
def test_export_listing_and_writes_reject_invalid_owner(exports_dir, user_id):
    with pytest.raises(ExportSecurityError):
        list_exports(user_id)
    with pytest.raises(ExportSecurityError):
        export_analysis(user_id=user_id, chat_id="chat", title="x", text="private")


def test_dot_chat_id_cannot_write_into_global_root(exports_dir):
    result = export_analysis(user_id="alice", chat_id="..", title="x", text="safe")
    assert result["relative_path"].startswith("alice/_/")


def test_user_and_chat_symlinks_cannot_escape_owner(exports_dir, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (exports_dir / "alice").symlink_to(outside, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        # 用 NTFS 联接点复现同样的已解析路径逃逸，
        # 既不需要开发者模式，也不必给测试运行器授予额外权限。
        subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path $env:NEXRAG_TEST_LINK -Target $env:NEXRAG_TEST_TARGET | Out-Null",
            ],
            env={
                **os.environ,
                "NEXRAG_TEST_LINK": str(exports_dir / "alice"),
                "NEXRAG_TEST_TARGET": str(outside),
            },
            check=True,
            capture_output=True,
        )
    # 测试根目录包含 outside，所以归属隔离必须拒绝这类链接，
    # 即使它们解析到全局根目录内另一位用户的文件夹。
    (outside / "private.txt").write_text("other account", encoding="utf8")
    with pytest.raises(ExportSecurityError):
        export_analysis(user_id="alice", chat_id="chat", title="x", text="bad")


def test_chat_id_is_sanitised_not_rejected(exports_dir):
    result = export_analysis(
        user_id="alice", chat_id="../../escape", title="t", fmt="txt", text="x"
    )
    assert (exports_dir / result["relative_path"]).is_relative_to(exports_dir / "alice")


def test_list_exports_is_scoped_per_user_and_chat(exports_dir):
    export_analysis(user_id="alice", chat_id="c1", title="a", fmt="txt", text="1")
    export_analysis(user_id="alice", chat_id="c2", title="b", fmt="txt", text="2")
    export_analysis(user_id="bob", chat_id="c1", title="c", fmt="txt", text="3")
    assert len(list_exports("alice")) == 2
    assert len(list_exports("alice", "c1")) == 1
    assert len(list_exports("bob")) == 1
    assert list_exports("nobody") == []


def headers(role="user"):
    claims = {
        "sub": "alice",
        "email": "alice@example.local",
        "role": role,
        "iss": "open-webui",
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
    }
    return {
        "Authorization": f"Bearer {KEY}",
        "X-OpenWebUI-User-Jwt": jwt.encode(claims, SECRET, algorithm="HS256"),
    }


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "NEXRAG_SHARED_API_KEY", KEY)
    monkeypatch.setattr(settings, "NEXRAG_USER_JWT_SECRET", SECRET)
    return TestClient(app)


def test_api_lists_and_downloads_own_export_only(client, exports_dir):
    result = export_analysis(
        user_id="alice", chat_id="chat-1", title="Report", fmt="txt", text="hello"
    )

    listed = client.get("/v1/exports", headers=headers()).json()["data"]
    assert [item["filename"] for item in listed] == [result["filename"]]

    ok = client.get(f"/v1/exports/download/{result['relative_path']}", headers=headers())
    assert ok.status_code == 200 and ok.text == "hello"

    export_analysis(user_id="mallory", chat_id="c", title="secret", fmt="txt", text="private")
    leaked = client.get("/v1/exports/download/mallory/c/secret.txt", headers=headers())
    assert leaked.status_code == 404

    traversal = client.get("/v1/exports/download/../mallory/c/secret.txt", headers=headers())
    assert traversal.status_code == 404
