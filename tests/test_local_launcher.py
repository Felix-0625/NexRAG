"""验证启动安全与配置，不下载任何内容，也不依赖真实 AI 服务。"""

import hashlib
import os
import socket
import subprocess
import sys

import pytest
from dotenv import dotenv_values

from scripts import launch_local as launcher
from scripts.native_environment import qdrant_environment
from scripts.webui_dependencies import locked_webui_requirements, webui_requirements


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    monkeypatch.setattr(launcher, "STATE", tmp_path / ".local-run")
    monkeypatch.chdir(tmp_path)
    for name in launcher.SECRET_NAMES:
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env.example").write_text(
        "\n".join(f"{name}=" for name in launcher.SECRET_NAMES)
        + "\nLLM_API_BASE=http://localhost:11434/v1\n"
        "EMBEDDING_API_BASE_URL=http://host.docker.internal:11434/v1\n",
        encoding="utf-8",
    )
    return tmp_path


def test_secrets_are_generated_once_and_custom_values_survive(checkout):
    existing = "custom-" + "x" * 40
    (checkout / ".env").write_text(
        f'NEXRAG_SHARED_API_KEY={existing}\nCUSTOM="value with spaces"\n',
        encoding="utf-8",
    )
    first = launcher.prepare_environment()
    contents = (checkout / ".env").read_bytes()
    second = launcher.prepare_environment()
    assert (checkout / ".env").read_bytes() == contents
    assert second["CUSTOM"] == "value with spaces"
    assert first["NEXRAG_SHARED_API_KEY"] == existing
    for name in launcher.SECRET_NAMES:
        assert first[name] == second[name]
        assert len(first[name]) >= 32
    assert len({first[name] for name in launcher.SECRET_NAMES}) == 5


def test_fresh_config_normalizes_docker_urls_without_editing_them(checkout):
    env = launcher.prepare_environment()
    assert env["EMBEDDING_API_BASE_URL"] == "http://127.0.0.1:11434/v1"
    assert (
        dotenv_values(checkout / ".env")["EMBEDDING_API_BASE_URL"]
        == "http://host.docker.internal:11434/v1"
    )


def test_empty_inherited_secrets_cannot_erase_generated_credentials(checkout, monkeypatch):
    for name in launcher.SECRET_NAMES:
        monkeypatch.setenv(name, "")
    environment = launcher.prepare_environment()
    configured = dotenv_values(checkout / ".env")
    for name in launcher.SECRET_NAMES:
        assert len(environment[name]) >= 32
        assert environment[name] == configured[name]


@pytest.mark.parametrize("configured", ["", "data/artifacts", "custom-artifacts"])
def test_relative_artifacts_stay_in_checkout_from_another_working_directory(
    checkout, monkeypatch, configured
):
    base = launcher.prepare_environment()
    base["ARTIFACTS_DIR"] = configured
    monkeypatch.chdir(checkout.parent)
    ports = launcher.pick_ports()
    environments = launcher.service_environments(base, ports)
    expected = (checkout / (configured or "data/artifacts")).resolve()
    assert environments["nexrag"]["EXPORTS_DIR"] == str(expected)
    assert environments["webui"]["NEXRAG_EXPORTS_DIR"] == str(expected)


def test_absolute_bound_artifact_folder_is_preserved(checkout):
    base = launcher.prepare_environment()
    bound = checkout.parent / "user-bound-artifacts"
    base["ARTIFACTS_DIR"] = str(bound)
    environments = launcher.service_environments(base, launcher.pick_ports())
    assert environments["nexrag"]["EXPORTS_DIR"] == str(bound.resolve())


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell integration")
def test_windows_shell_helpers_load_compatible_builtin_modules(checkout):
    environment = launcher.prepare_environment()
    fixture = checkout / "hash fixture.txt"
    fixture.write_bytes(b"native-startup-fixture")
    environment["NEXRAG_HASH_FIXTURE"] = str(fixture)
    process = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "$ErrorActionPreference='Stop'; (Get-FileHash -LiteralPath $env:NEXRAG_HASH_FIXTURE).Hash",
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert process.returncode == 0, process.stderr
    assert process.stdout.strip().lower() == hashlib.sha256(fixture.read_bytes()).hexdigest()


def test_dynamic_ports_are_wired_into_every_backend(checkout):
    base = launcher.prepare_environment()
    base["EMBEDDING_API_BASE_URL"] = "https://embeddings.example/v1"
    ports = {
        name: 19000 + index
        for index, name in enumerate(
            ["web", "nexrag", "ingestion", "retrieval", "webui", "qdrant", "qdrant_grpc"]
        )
    }
    envs = launcher.service_environments(base, ports)
    assert (checkout / "data/openwebui").is_dir()
    for name in ["nexrag", "retrieval", "ingestion", "webui"]:
        assert envs[name]["QDRANT_URI"] == launcher.url(ports["qdrant"])
        assert envs[name]["EMBEDDING_API_BASE_URL"] == "https://embeddings.example/v1"
    assert envs["webui"]["OPENAI_API_BASE_URL"] == launcher.url(ports["nexrag"]) + "/v1"
    assert envs["frontend"]["WEBUI_BACKEND_URL"] == launcher.url(ports["webui"])
    assert envs["ingestion"]["API_KEY"] == base["INGESTION_API_KEY"]
    assert envs["retrieval"]["API_KEY"] == base["RETRIEVAL_API_KEY"]
    assert envs["retrieval"]["RERANK_TOP_K"] == "3"
    assert envs["retrieval"]["RERANK_FUSION"] == "replace"
    assert envs["retrieval"]["RERANK_REQUIRED"] == "true"
    assert envs["retrieval"]["ENABLE_AGENTIC_RAG"] == "false"
    assert envs["webui"]["ENABLE_PLUGINS"] == "false"


@pytest.mark.parametrize("client_url", ["", "http://localhost:6333", "https://other.example:6333"])
def test_managed_qdrant_ignores_application_client_and_peer_urls(checkout, client_url):
    base = {"QDRANT_URI": client_url, "QDRANT_BOOTSTRAP": "http://other.example:6335"}
    environment = qdrant_environment(base, {"qdrant": 19933, "qdrant_grpc": 19934}, checkout)
    assert "QDRANT_URI" not in environment
    assert "QDRANT_BOOTSTRAP" not in environment
    assert environment["QDRANT__CLUSTER__ENABLED"] == "false"
    assert environment["QDRANT__SERVICE__HTTP_PORT"] == "19933"
    assert environment["QDRANT__STORAGE__STORAGE_PATH"] == str(checkout / "data/qdrant/storage")
    assert base["QDRANT_URI"] == client_url


def test_webui_security_pins_replace_upstream_instead_of_conflicting(checkout):
    backend = checkout / "webui/backend"
    backend.mkdir(parents=True)
    scripts = checkout / "scripts"
    scripts.mkdir()
    (backend / "requirements-slim.txt").write_text(
        "# pinned upstream\nPyJWT[crypto]==2.13.0\nmcp==1.27.2 # old\nhttpx==0.28.1\n",
        encoding="utf8",
    )
    (scripts / "webui-security-requirements.txt").write_text(
        "# reviewed replacements\nPyJWT[crypto]==2.15.0\nmcp==2.2.0\n", encoding="utf8"
    )
    result = webui_requirements(checkout)
    assert "PyJWT[crypto]==2.15.0" in result and "2.13.0" not in result
    assert "mcp==2.2.0" in result and "1.27.2" not in result
    assert "httpx==0.28.1" in result
    assert "python-dotenv==1.2.4" in result
    lock_path = scripts / "webui-requirements.lock"
    lock_path.write_text(result, encoding="utf8")
    assert locked_webui_requirements(checkout) == result
    lock_path.write_text(result.replace("2.15.0", "2.13.0"), encoding="utf8")
    with pytest.raises(RuntimeError, match="lock does not match"):
        locked_webui_requirements(checkout)
    # 上游重命名或移除时，不得悄悄漏掉安全替换项。
    (scripts / "webui-security-requirements.txt").write_text(
        "missing-package==1.0\n", encoding="utf8"
    )
    with pytest.raises(RuntimeError, match="no longer match"):
        webui_requirements(checkout)


def test_ollama_localhost_without_cloud_status_is_not_trusted():
    import io
    from unittest.mock import Mock

    assert launcher.ollama_cloud_disabled(
        "http://127.0.0.1:11434",
        Mock(open=lambda *a, **k: io.BytesIO(b'{"cloud":{"disabled":true}}')),
    )
    assert not launcher.ollama_cloud_disabled(
        "http://127.0.0.1:11434",
        Mock(open=lambda *a, **k: io.BytesIO(b'{"cloud":{"disabled":false}}')),
    )
    assert not launcher.ollama_cloud_disabled(
        "http://127.0.0.1:11434", Mock(open=lambda *a, **k: io.BytesIO(b'{"models":[]}'))
    )


def test_owned_ollama_forces_local_only_and_rewrites_service_endpoints(checkout, monkeypatch):
    import io
    from unittest.mock import Mock

    trusted = iter([False, True])
    monkeypatch.setattr(launcher, "ollama_cloud_disabled", lambda *a: next(trusted))
    monkeypatch.setattr(launcher.shutil, "which", lambda name: "existing-ollama.exe")
    monkeypatch.setattr(
        launcher.urllib.request,
        "build_opener",
        lambda *a: Mock(open=lambda *a, **k: io.BytesIO(b'{"models":[]}')),
    )
    supervisor = Mock()
    env = {
        "LLM_API_BASE": "http://localhost:11434/v1",
        "EMBEDDING_API_BASE_URL": "http://localhost:11434/v1",
    }
    launcher.prepare_ollama(env, supervisor, skip_models=True)
    started_environment = supervisor.start.call_args.args[-1]
    assert started_environment["OLLAMA_NO_CLOUD"] == "1"
    assert started_environment["OLLAMA_HOST"].startswith("127.0.0.1:")
    assert (
        env["LLM_API_BASE"]
        == env["EMBEDDING_API_BASE_URL"]
        == env["NEXRAG_MANAGED_OLLAMA_API_BASE"]
    )
    assert (checkout / ".local-run/ollama-trust.json").is_file()


def test_port_selection_avoids_existing_listener():
    with socket.socket() as busy:
        try:
            busy.bind(("127.0.0.1", 3000))
        except OSError:
            pass  # 已有应用占用了该端口。
        ports = launcher.pick_ports()
        assert ports["web"] != 3000
        assert len(set(ports.values())) == len(ports)


def test_supervisor_only_stops_its_children(checkout):
    import subprocess

    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    supervisor = launcher.Supervisor()
    try:
        supervisor.start(
            "owned",
            [sys.executable, "-c", "import time; time.sleep(60)"],
            checkout,
            dict(os.environ),
        )
        child = supervisor.children[0].process
        supervisor.close()
        assert child.poll() is not None
        assert unrelated.poll() is None
    finally:
        supervisor.close()
        unrelated.terminate()
        unrelated.wait(timeout=10)


def test_failed_service_is_not_reported_ready(checkout):
    supervisor = launcher.Supervisor()
    try:
        supervisor.start(
            "broken", [sys.executable, "-c", "raise SystemExit(7)"], checkout, dict(os.environ)
        )
        supervisor.children[0].process.wait(timeout=10)
        with pytest.raises(RuntimeError, match="broken stopped"):
            supervisor.wait_http({"broken": "http://127.0.0.1:1/health"}, timeout=1)
    finally:
        supervisor.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows job-object exit/assignment race")
def test_fast_exited_service_reports_real_exit_instead_of_job_access_denied(checkout):
    supervisor = launcher.Supervisor()
    assign = supervisor.job.assign

    def after_exit(process):
        process.wait(timeout=10)
        # 确定性地复现这个竞态状态：子进程已终止后，
        # 这次真实的 Win32 调用会返回 ERROR_ACCESS_DENIED。
        assign(process)

    supervisor.job.assign = after_exit
    try:
        with pytest.raises(RuntimeError, match=r"frontend stopped during startup \(exit 7\)"):
            supervisor.start(
                "frontend",
                [sys.executable, "-c", "raise SystemExit(7)"],
                checkout,
                dict(os.environ),
            )
        assert supervisor.children == []
    finally:
        supervisor.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows job-object containment")
def test_live_service_job_rejection_stops_child_and_does_not_bypass_isolation(checkout):
    supervisor = launcher.Supervisor()
    started = []

    def reject(process):
        started.append(process)
        raise OSError(5, "synthetic live-process isolation refusal")

    supervisor.job.assign = reject
    try:
        with pytest.raises(RuntimeError, match="frontend process isolation failed"):
            supervisor.start(
                "frontend",
                [sys.executable, "-c", "import time; time.sleep(60)"],
                checkout,
                dict(os.environ),
            )
        assert len(started) == 1 and started[0].poll() is not None
        assert supervisor.children == []
    finally:
        supervisor.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows job-object lifecycle")
def test_supervisor_stops_descendant_workers(checkout):
    import ctypes
    import time
    from ctypes import wintypes

    marker = checkout / "worker.pid"
    code = (
        "import subprocess, sys, time; from pathlib import Path; "
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        # 原子发布：仅凭 exists() 可能读到 write_text 写出的空文件。
        f"marker = Path({str(marker)!r}); temporary = marker.with_suffix('.tmp'); "
        "temporary.write_text(str(p.pid)); temporary.replace(marker); time.sleep(60)"
    )
    supervisor = launcher.Supervisor()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = None
    try:
        supervisor.start("parent", [sys.executable, "-c", code], checkout, dict(os.environ))
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists()
        handle = kernel.OpenProcess(0x100000, False, int(marker.read_text()))
        assert handle
        supervisor.close()
        assert kernel.WaitForSingleObject(handle, 5000) == 0
    finally:
        supervisor.close()
        if handle:
            kernel.CloseHandle(handle)


def test_corrupt_download_is_rejected_without_replacing_cached_file(checkout, monkeypatch):
    import io

    monkeypatch.setattr(launcher.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"corrupt"))
    cached = checkout / "runtime.zip"
    cached.write_bytes(b"existing")
    with pytest.raises(RuntimeError, match="Checksum mismatch"):
        launcher.download("https://example.com/runtime.zip", cached, "0" * 64)
    assert cached.read_bytes() == b"existing"
