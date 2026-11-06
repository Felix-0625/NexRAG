"""源码导出必须排除机器状态，并拒绝已知泄露的密钥。"""

import subprocess

import pytest

from scripts.verify_source_delivery import export_source


def repository(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    subprocess.run(["git", "init", "--quiet", root], check=True)
    return root


def test_export_copies_current_product_source_but_not_local_state(tmp_path):
    root = repository(tmp_path)
    (root / "README.md").write_text("Current edited source", encoding="utf8")
    subprocess.run(["git", "-C", root, "add", "README.md"], check=True)
    for name in ("src/feature.py", "webui/generated.py", "data/ops/ops.db", ".claude/launch.json"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf8")
    (root / ".env").write_text("NEXRAG_SHARED_API_KEY=" + "machine-secret-" * 4, encoding="utf8")
    destination = root / ".local-run/isolated/source"
    report = export_source(root, destination)
    assert (destination / "README.md").read_text(encoding="utf8") == "Current edited source"
    assert (destination / "src/feature.py").is_file()
    assert not any(
        (destination / name).exists() for name in (".git", ".env", "webui", "data", ".claude")
    )
    assert report["file_count"] == 2
    assert report["generated_runtime_copied"] is False
    assert (destination.parent / "source-manifest.json").is_file()


def test_export_refuses_known_credential_in_product_code(tmp_path):
    root = repository(tmp_path)
    secret = "not-a-real-cloud-key-123456789"
    (root / ".env").write_text("LLM_API_KEY=" + secret, encoding="utf8")
    (root / "src").mkdir()
    (root / "src/leak.py").write_text(secret, encoding="utf8")
    with pytest.raises(ValueError, match="Known local credential"):
        export_source(root, root / ".local-run/isolated/source")


def test_export_never_overwrites_or_escapes_acceptance_directory(tmp_path):
    root = repository(tmp_path)
    with pytest.raises(ValueError, match="inside"):
        export_source(root, tmp_path / "outside")
    destination = root / ".local-run/existing/source"
    destination.mkdir(parents=True)
    marker = destination / "keep.txt"
    marker.write_text("do not replace", encoding="utf8")
    with pytest.raises(ValueError, match="new"):
        export_source(root, destination)
    assert marker.read_text(encoding="utf8") == "do not replace"
