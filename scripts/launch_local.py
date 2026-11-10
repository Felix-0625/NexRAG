"""Prepare and supervise Windows-native NexRAG processes.

start.cmd/start.ps1 invokes uv-managed Python 3.12. Keep dependencies, tools, and logs
inside the checkout; avoid shell concatenation, global installs, and killing other services.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
import zipfile
from pathlib import Path

if __package__:
    from . import native_environment, native_ollama, native_processes
    from .native_processes import Child, WindowsJob  # noqa: F401 - public compatibility exports
    from .webui_dependencies import locked_webui_requirements
else:
    import native_environment
    import native_ollama
    import native_processes
    from native_processes import Child, WindowsJob  # noqa: F401 - public compatibility exports
    from webui_dependencies import locked_webui_requirements

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".local-run"
SECRET_NAMES = (
    "NEXRAG_SHARED_API_KEY",
    "NEXRAG_USER_JWT_SECRET",
    "WEBUI_SECRET_KEY",
    "INGESTION_API_KEY",
    "RETRIEVAL_API_KEY",
)
NODE_VERSION = "22.23.4"
NODE_SHA256 = "cbc74b5e0982c9dbb41525fd8fb5dcd32fbee237863f7829072f4639a1ce5f20"


def say(message: str) -> None:
    """Print a flushed launcher progress message."""
    print(f"[NexRAG] {message}", flush=True)


def run(args: list[str | Path], *, cwd: Path = ROOT, env: dict | None = None) -> str:
    """Capture tool output so credential-bearing arguments are not echoed."""
    process = subprocess.Popen(
        [str(arg) for arg in args],
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        while True:
            try:
                output, _ = process.communicate(timeout=20)
                break
            except subprocess.TimeoutExpired:
                say(f"{Path(str(args[0])).name}: still preparing dependencies or source files...")
    except BaseException:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise
    if process.returncode:
        raise RuntimeError(f"{Path(str(args[0])).name} failed:\n{output[-6000:]}")
    return output.strip()


def download(url: str, target: Path, digest: str | None = None) -> Path:
    """Download through a temporary .part file, verifying an optional SHA-256 before replacement."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists() or (digest and sha256(target) != digest):
        say(f"Downloading {target.name} (first run; cached for subsequent starts)...")
        temporary = target.with_suffix(target.suffix + ".part")
        request = urllib.request.Request(url, headers={"User-Agent": "NexRAG-local-launcher"})
        with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as out:
            last_report = time.monotonic()
            received = 0
            while chunk := response.read(256 * 1024):
                out.write(chunk)
                received += len(chunk)
                if time.monotonic() - last_report > 20:
                    say(f"{target.name}: {received // (1024 * 1024)} MB downloaded...")
                    last_report = time.monotonic()
        if digest and sha256(temporary) != digest:
            raise RuntimeError(f"Checksum mismatch: {target.name}; download was not installed.")
        temporary.replace(target)
    return target


def sha256(path: Path) -> str:
    """Return a file's SHA-256 hex digest."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fetch_json(url: str) -> dict:
    """Fetch and decode JSON with a 20-second timeout."""
    request = urllib.request.Request(url, headers={"User-Agent": "NexRAG-local-launcher"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def github_zip(repository: str, version: str, asset_name: str, folder: Path) -> None:
    """Download a pinned GitHub release and require a published SHA-256 before extraction."""
    release = fetch_json(f"https://api.github.com/repos/{repository}/releases/tags/{version}")
    asset = next(item for item in release["assets"] if item["name"] == asset_name)
    digest = asset.get("digest", "")
    if not digest.startswith("sha256:") or len(digest) != 71:
        raise RuntimeError(f"Official release has no SHA256 digest: {asset_name}")
    archive = download(asset["browser_download_url"], STATE / "downloads" / asset_name, digest[7:])
    with zipfile.ZipFile(archive) as package:
        package.extractall(folder)


def prepare_node() -> Path:
    """Use Node 22 at the security baseline or download a verified pinned release."""
    node = shutil.which("node")
    if node:
        version = run([node, "--version"]).strip().lstrip("v").split(".")
        if (
            version[0] == "22"
            and all(part.isdecimal() for part in version)
            and tuple(map(int, version)) >= tuple(map(int, NODE_VERSION.split(".")))
        ):
            return Path(node)
    folder = STATE / "tools" / f"node-v{NODE_VERSION}-win-x64"
    executable = folder / "node.exe"
    if not executable.exists():
        archive = download(
            f"https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-win-x64.zip",
            STATE / "downloads" / f"node-v{NODE_VERSION}-win-x64.zip",
            NODE_SHA256,
        )
        with zipfile.ZipFile(archive) as package:
            package.extractall(STATE / "tools")
    return executable


def prepare_qdrant() -> Path:
    """Download pinned Qdrant on demand and return its executable path."""
    executable = STATE / "tools" / "qdrant-1.19.1" / "qdrant.exe"
    if not executable.exists():
        github_zip(
            "qdrant/qdrant", "v1.19.1", "qdrant-x86_64-pc-windows-msvc.zip", executable.parent
        )
    return executable


def prepare_environment() -> dict[str, str]:
    """Load native runtime settings and fill absent service secrets."""
    return native_environment.prepare_environment(ROOT, STATE, SECRET_NAMES)


def pick_ports() -> dict[str, int]:
    """Reserve and select nonconflicting loopback service ports."""
    return native_environment.pick_ports()


def url(port: int) -> str:
    """Return the service HTTP base URL on loopback."""
    return native_environment.url(port)


def service_environments(base: dict[str, str], ports: dict[str, int]) -> dict[str, dict]:
    """Build isolated environment mappings for each native service."""
    return native_environment.service_environments(base, ports, root=ROOT, state=STATE, report=say)


def prepare_python(uv: str, name: str, project: Path, base: dict) -> Path:
    """Sync a project's isolated environment from frozen locks when fingerprints change."""
    folder = STATE / "venvs" / name
    python = folder / "Scripts/python.exe"
    fingerprint = hashlib.sha256(
        (project / "uv.lock").read_bytes() + (project / "pyproject.toml").read_bytes()
    ).hexdigest()
    stamp = folder / ".nexrag-dependencies"
    if not python.exists() or not stamp.exists() or stamp.read_text() != fingerprint:
        say(f"Preparing Python dependencies: {name}...")
        env = dict(base, UV_PROJECT_ENVIRONMENT=str(folder))
        run(
            [uv, "sync", "--frozen", "--no-dev", "--no-install-project", "--python", "3.12"],
            cwd=project,
            env=env,
        )
        stamp.write_text(fingerprint)
    return python


def prepare_webui_python(uv: str, base: dict) -> Path:
    """Sync the locked slim WebUI environment and check dependency consistency when changed."""
    folder = STATE / "venvs/webui"
    python = folder / "Scripts/python.exe"
    # Use upstream slim dependencies because other services own AI and document processing.
    requirements = locked_webui_requirements(ROOT)
    target = STATE / "webui-requirements.txt"
    target.write_text(requirements, encoding="utf-8")
    fingerprint = hashlib.sha256(requirements.encode()).hexdigest()
    stamp = folder / ".nexrag-dependencies"
    if not python.exists() or not stamp.exists() or stamp.read_text() != fingerprint:
        say("Preparing Open WebUI Python dependencies (slim)...")
        if not python.exists():
            run([uv, "venv", "--python", "3.12", folder], env=base)
        run([uv, "pip", "sync", "--python", python, target], env=base)
        run([uv, "pip", "check", "--python", python], env=base)
        stamp.write_text(fingerprint)
    return python


def prepare_frontend(node: Path, base: dict) -> None:
    """Install frontend dependencies only when locks change, with lifecycle scripts disabled."""
    project = ROOT / "webui"
    fingerprint = sha256(project / "package-lock.json")
    stamp = project / "node_modules/.nexrag-dependencies"
    if not stamp.exists() or stamp.read_text() != fingerprint:
        say("Preparing browser interface dependencies...")
        env = dict(
            base,
            PATH=str(node.parent) + os.pathsep + base.get("PATH", ""),
        )
        run(
            [
                node,
                node.parent / "node_modules/npm/bin/npm-cli.js",
                "ci",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
            ],
            cwd=project,
            env=env,
        )
        # The browser interpreter is disabled, so skip its large optional runtime download.
        stamp.write_text(fingerprint)


def prepare_business_data(base: dict, python: Path) -> None:
    """Bootstrap Olist only for unchanged built-in example configuration without existing data."""
    # Bootstrap only built-in examples; never replace custom configuration or data.
    if (
        base.get("BUSINESS_SOURCE_PATH", "examples/olist/business-source.yaml").replace("\\", "/")
        != "examples/olist/business-source.yaml"
    ):
        return
    if base.get("BUSINESS_DB_PATH") or (ROOT / "data/sqlite/business.db").exists():
        return
    say("Preparing bundled Olist example data...")
    run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            ROOT / "scripts/fetch-olist.ps1",
        ],
        env=base,
    )
    run([python, ROOT / "scripts/import_olist.py"], env=base)


class Supervisor(native_processes.Supervisor):
    """Preserve the launcher API while delegating process lifecycle implementation."""

    def __init__(self) -> None:
        super().__init__(STATE, say)


def ollama_cloud_disabled(endpoint: str, opener=None) -> bool:
    """Verify the server explicitly reports cloud inference disabled."""
    return native_ollama.ollama_cloud_disabled(endpoint, opener)


def prepare_ollama(base: dict, supervisor: Supervisor, skip_models: bool) -> None:
    """Verify local-only inference and prepare the configured Ollama models."""
    native_ollama.prepare_ollama(
        base,
        supervisor,
        skip_models,
        root=ROOT,
        state=STATE,
        report=say,
        cloud_disabled=ollama_cloud_disabled,
        release_json=fetch_json,
        release_zip=github_zip,
    )


@contextlib.contextmanager
def launcher_lock():
    """Use an OS-released lock so stale PID files cannot block launches after crashes."""
    import msvcrt

    path = STATE / "launcher.lock"
    with path.open("a+b") as lock:
        if path.stat().st_size == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError(
                "This checkout is already starting/running. See .local-run/runtime.json for its URL."
            ) from exc
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def _prepare_runtime(args: argparse.Namespace, base: dict) -> tuple[Path, dict[str, Path], Path]:
    """Prepare tools, Python environments, frontend dependencies, Qdrant, and sample data."""
    say("Preparing local runtime. The first run downloads dependencies; later starts reuse them.")
    if not shutil.which("git"):
        raise RuntimeError("Git is required to prepare the pinned Open WebUI source checkout.")
    run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            ROOT / "scripts/prepare-webui.ps1",
        ],
        env=base,
    )
    node = prepare_node()
    base["PATH"] = str(node.parent) + os.pathsep + base.get("PATH", "")
    pythons = {
        name: prepare_python(args.uv, name, project, base)
        for name, project in [
            ("nexrag", ROOT),
            ("retrieval", ROOT / "base/retrieval-agent"),
            ("ingestion", ROOT / "base/ingestion-service"),
        ]
    }
    pythons["webui"] = prepare_webui_python(args.uv, base)
    prepare_frontend(node, base)
    qdrant = prepare_qdrant()
    prepare_business_data(base, pythons["nexrag"])
    return node, pythons, qdrant


def _write_runtime_state(runtime_file: Path, status: str, ports: dict[str, int]) -> None:
    """Write launch phase and ports for external runtime-address discovery."""
    runtime_file.write_text(
        json.dumps({"status": status, "url": url(ports["web"]), "ports": ports}, indent=2),
        encoding="utf-8",
    )


def _start_qdrant(supervisor: Supervisor, base: dict, ports: dict[str, int], qdrant: Path) -> None:
    """Start owned Qdrant and wait until ready."""
    qdrant_env = native_environment.qdrant_environment(base, ports, ROOT)
    supervisor.start("qdrant", [qdrant], qdrant.parent, qdrant_env)
    supervisor.wait_http({"qdrant": url(ports["qdrant"]) + "/readyz"}, timeout=90)


def _start_backends(
    supervisor: Supervisor,
    pythons: dict[str, Path],
    ports: dict[str, int],
    environments: dict[str, dict],
) -> None:
    """Start the NexRAG, WebUI, ingestion, and retrieval backends in order."""
    for name, module, cwd in [
        ("nexrag", "src.main:app", ROOT),
        ("webui", "open_webui.main:app", ROOT / "webui/backend"),
        ("ingestion", "scripts.local_ingestion:app", ROOT),
        ("retrieval", "scripts.local_retrieval:app", ROOT),
    ]:
        supervisor.start(
            name,
            [
                pythons[name],
                "-m",
                "uvicorn",
                module,
                "--host",
                "127.0.0.1",
                "--port",
                str(ports[name]),
            ],
            cwd,
            environments[name],
        )


def _start_frontend(
    supervisor: Supervisor, node: Path, ports: dict[str, int], environments: dict[str, dict]
) -> None:
    """Start Vite, failing if its port is already occupied."""
    supervisor.start(
        "frontend",
        [
            node,
            ROOT / "webui/node_modules/vite/bin/vite.js",
            "--host",
            "127.0.0.1",
            "--port",
            str(ports["web"]),
            "--strictPort",
        ],
        ROOT / "webui",
        environments["frontend"],
    )


def _serve_until_stopped(
    args: argparse.Namespace,
    base: dict,
    supervisor: Supervisor,
    ports: dict[str, int],
    runtime_file: Path,
) -> None:
    """Wait for services, update state, then supervise child processes."""
    supervisor.wait_http(
        {
            "nexrag": url(ports["nexrag"]) + "/health",
            "webui": url(ports["webui"]) + "/health",
            "browser interface": url(ports["web"]) + "/auth",
        }
    )
    _write_runtime_state(runtime_file, "starting-models", ports)
    say(
        f"Browser interface ready: {url(ports['web'])} — sign in, or create the first administrator on a new installation."
    )
    if not args.no_browser:
        webbrowser.open(url(ports["web"]))
    supervisor.wait_http(
        {
            "ingestion": url(ports["ingestion"]) + "/health/ready",
            "retrieval": url(ports["retrieval"]) + "/health/ready",
        }
    )
    _write_runtime_state(runtime_file, "ready", ports)
    say(f"All local services ready: {url(ports['web'])}")
    if (
        base.get("ENABLE_RERANKING", "").lower() == "true"
        and base.get("RERANKER_PROVIDER") == "fastembed"
    ):
        say(
            "The local cross-encoder is ready; retrieval ranks child chunks before parent expansion."
        )
    say("Keep this terminal open. Press Ctrl+C to stop this launch. Data is retained.")
    while True:
        supervisor.check()
        time.sleep(1)


def launch(args: argparse.Namespace) -> None:
    """Prepare and supervise the stack, stopping only owned services; prepare-only starts nothing."""
    base = prepare_environment()
    node, pythons, qdrant = _prepare_runtime(args, base)
    if args.prepare_only:
        say("Runtime prepared. Run start.cmd to start services.")
        return
    ports = pick_ports()
    supervisor = Supervisor()
    runtime_file = STATE / "runtime.json"
    try:
        # Prove inference stays local before starting services that may send document text
        # to embeddings or business evidence to chat models.
        prepare_ollama(base, supervisor, args.skip_models)
        environments = service_environments(base, ports)
        _write_runtime_state(runtime_file, "starting", ports)
        _start_qdrant(supervisor, base, ports, qdrant)
        _start_backends(supervisor, pythons, ports, environments)
        _start_frontend(supervisor, node, ports, environments)
        _serve_until_stopped(args, base, supervisor, ports, runtime_file)
    finally:
        say("Stopping services started by this launcher...")
        supervisor.close()
        if runtime_file.exists():
            state = json.loads(runtime_file.read_text(encoding="utf-8"))
            state["status"] = "stopped"
            runtime_file.write_text(json.dumps(state, indent=2), encoding="utf-8")


def main() -> int:
    """Run the Windows x64 launcher under its lock; return zero on success or interruption."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uv", default="uv")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--skip-models", action="store_true", help="Skip Ollama model downloads.")
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    if os.name != "nt" or platform.machine().lower() not in {"amd64", "x86_64"}:
        parser.error("This launcher currently supports Windows x64.")
    os.chdir(ROOT)
    STATE.mkdir(exist_ok=True)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        with launcher_lock():
            launch(args)
    except KeyboardInterrupt:
        say("Stopped. Run start.cmd to start again.")
    except Exception as exc:
        say(f"Startup failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
