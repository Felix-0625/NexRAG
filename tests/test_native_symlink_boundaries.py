"""原生 Windows 符号链接边界；绝不用 NTFS 联接点代替。

本地 Windows 会话没有符号链接权限时，会明确跳过链接相关用例。
GitHub Actions 的 Windows 运行器必须执行这些用例：创建链接失败即视为 CI 失败。
"""

import errno
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agent import exports as exports_module
from src.agent.exports import ExportSecurityError, export_analysis, list_exports
from src.midplatform.sandbox_file import FileSandboxSecurityError, inspect_file_content


def snapshot(directory):
    """同时记录名称与内容，这样即使多出一次意外写入也能被发现。"""
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


@pytest.fixture
def boundary(tmp_path, monkeypatch):
    root = tmp_path / "root"
    sandbox, exports, outside = root / "sandbox", root / "exports", tmp_path / "outside"
    for directory in (sandbox, exports, outside / "nested"):
        directory.mkdir(parents=True)
    (outside / "private.txt").write_text("synthetic outside private content", encoding="utf8")
    (outside / "nested" / "report.txt").write_text("synthetic outside report", encoding="utf8")
    original = snapshot(outside)
    monkeypatch.setattr(exports_module, "export_root", lambda: exports)
    yield SimpleNamespace(sandbox=sandbox, exports=exports, outside=outside)
    assert snapshot(outside) == original, "Outside file names and hashes must remain unchanged"


@pytest.fixture
def native_link():
    def create(path: Path, target: Path, *, directory=False):
        if os.name != "nt":
            pytest.skip("Native Windows symlink case requires Windows")
        try:
            path.symlink_to(target, target_is_directory=directory)
        except OSError as error:
            if os.getenv("GITHUB_ACTIONS", "").lower() == "true":
                pytest.fail(f"Windows CI must create native symlinks, no fallback allowed: {error}")
            if getattr(error, "winerror", None) == 1314 or error.errno in (
                errno.EACCES,
                errno.EPERM,
            ):
                pytest.skip(f"Local Windows session lacks native symlink privileges: {error}")
            raise
        assert path.is_symlink(), "The test requires a native symlink, not a junction"
        assert not getattr(path, "is_junction", lambda: False)()
        return path

    return create


def test_regular_local_read_and_export_remain_allowed(boundary):
    source = boundary.sandbox / "allowed.txt"
    source.write_text("synthetic allowed content", encoding="utf8")
    assert (
        inspect_file_content(source, base_dir=boundary.sandbox)["content"]
        == "synthetic allowed content"
    )
    result = export_analysis(
        user_id="alice", chat_id="chat", title="Allowed", text="verified report"
    )
    path = boundary.exports / result["relative_path"]
    assert path.is_file() and "verified report" in path.read_text("utf8")
    assert [entry["relative_path"] for entry in list_exports("alice", "chat")] == [
        result["relative_path"]
    ]


def test_native_file_symlink_cannot_read_outside_sandbox(boundary, native_link):
    link = native_link(boundary.sandbox / "linked.txt", boundary.outside / "private.txt")
    with pytest.raises(FileSandboxSecurityError, match="outside sandbox root"):
        inspect_file_content(link, base_dir=boundary.sandbox)


def test_native_directory_symlink_cannot_read_outside_sandbox(boundary, native_link):
    link = native_link(boundary.sandbox / "linked", boundary.outside, directory=True)
    with pytest.raises(FileSandboxSecurityError, match="outside sandbox root"):
        inspect_file_content(link / "nested" / "report.txt", base_dir=boundary.sandbox)


def test_native_user_symlink_cannot_export_outside_owner(boundary, native_link):
    native_link(boundary.exports / "alice", boundary.outside, directory=True)
    with pytest.raises(ExportSecurityError, match="User export folder"):
        export_analysis(user_id="alice", chat_id="chat", title="Blocked", text="must not write")


def test_native_user_symlink_cannot_list_outside_owner(boundary, native_link):
    native_link(boundary.exports / "alice", boundary.outside, directory=True)
    with pytest.raises(ExportSecurityError, match="User export folder"):
        list_exports("alice")


def test_native_chat_symlink_cannot_export_outside_owner(boundary, native_link):
    owner = boundary.exports / "alice"
    owner.mkdir()
    native_link(owner / "chat", boundary.outside, directory=True)
    with pytest.raises(ExportSecurityError, match="Chat export folder"):
        export_analysis(user_id="alice", chat_id="chat", title="Blocked", text="must not write")


def test_native_chat_symlink_cannot_list_outside_owner(boundary, native_link):
    owner = boundary.exports / "alice"
    owner.mkdir()
    native_link(owner / "chat", boundary.outside, directory=True)
    with pytest.raises(ExportSecurityError, match="Chat export folder"):
        list_exports("alice", "chat")
    assert list_exports("alice") == []
