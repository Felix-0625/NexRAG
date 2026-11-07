"""准备并监管 NexRAG 的 Windows 原生进程。

由 start.cmd/start.ps1 使用 uv 管理的 Python 3.12 调用。依赖、下载的可执行文件
和日志都留在当前检出目录内；不拼接 shell 命令、不做全局包安装，也不终止已有服务。
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
    print(f"[NexRAG] {message}", flush=True)


def run(args: list[str | Path], *, cwd: Path = ROOT, env: dict | None = None) -> str:
    """捕获工具输出，避免凭据作为命令参数被回显。"""
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
    """下载到临时 .part 文件，提供 digest 时校验 SHA256，通过后才替换目标；已缓存且校验通过则跳过。"""
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
    """返回文件内容的 SHA256 十六进制摘要。"""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fetch_json(url: str) -> dict:
    """以 20 秒超时请求 URL 并解析 JSON 响应。"""
    request = urllib.request.Request(url, headers={"User-Agent": "NexRAG-local-launcher"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def github_zip(repository: str, version: str, asset_name: str, folder: Path) -> None:
    """下载 GitHub 指定版本的发布压缩包并解压到 folder；发布资产没有 SHA256 摘要时拒绝安装。"""
    release = fetch_json(f"https://api.github.com/repos/{repository}/releases/tags/{version}")
    asset = next(item for item in release["assets"] if item["name"] == asset_name)
    digest = asset.get("digest", "")
    if not digest.startswith("sha256:") or len(digest) != 71:
        raise RuntimeError(f"Official release has no SHA256 digest: {asset_name}")
    archive = download(asset["browser_download_url"], STATE / "downloads" / asset_name, digest[7:])
    with zipfile.ZipFile(archive) as package:
        package.extractall(folder)


def prepare_node() -> Path:
    """使用达到安全基线的 Node 22，或下载经 SHA256 校验的固定版本。"""
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
    """按需下载固定版本的 Qdrant 发布包到 .local-run/tools，返回 qdrant.exe 路径。"""
    executable = STATE / "tools" / "qdrant-1.19.1" / "qdrant.exe"
    if not executable.exists():
        github_zip(
            "qdrant/qdrant", "v1.19.1", "qdrant-x86_64-pc-windows-msvc.zip", executable.parent
        )
    return executable


def prepare_environment() -> dict[str, str]:
    return native_environment.prepare_environment(ROOT, STATE, SECRET_NAMES)


def pick_ports() -> dict[str, int]:
    return native_environment.pick_ports()


def url(port: int) -> str:
    return native_environment.url(port)


def service_environments(base: dict[str, str], ports: dict[str, int]) -> dict[str, dict]:
    return native_environment.service_environments(base, ports, root=ROOT, state=STATE, report=say)


def prepare_python(uv: str, name: str, project: Path, base: dict) -> Path:
    """按 uv.lock 与 pyproject.toml 的指纹，用 uv sync --frozen 同步该项目的独立虚拟环境，指纹未变则跳过，返回其 python.exe。"""
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
    """按锁定的精简依赖清单同步 Open WebUI 的虚拟环境并执行 uv pip check，依赖指纹未变则跳过，返回其 python.exe。"""
    folder = STATE / "venvs/webui"
    python = folder / "Scripts/python.exe"
    # 使用上游的精简依赖集：所有 AI/文档处理都委托给其他服务。
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
    """锁文件变化时重装前端依赖，安装阶段禁用包的生命周期脚本。"""
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
        # 浏览器代码解释器已禁用，因此不下载其体积庞大的可选运行时。
        stamp.write_text(fingerprint)


def prepare_business_data(base: dict, python: Path) -> None:
    """仅在使用内置 Olist 示例配置且尚无业务数据库时，获取并导入示例数据；自定义配置或已有数据一律不动。"""
    # 只引导内置示例；自定义配置/数据绝不会被替换。
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
    """保持入口 API 不变，而进程生命周期的实现放在别处。"""

    def __init__(self) -> None:
        super().__init__(STATE, say)


def ollama_cloud_disabled(endpoint: str, opener=None) -> bool:
    return native_ollama.ollama_cloud_disabled(endpoint, opener)


def prepare_ollama(base: dict, supervisor: Supervisor, skip_models: bool) -> None:
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
    """进程崩溃时由操作系统释放该锁；残留的 PID 文件不会阻止其他进程。"""
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
    """准备 Node、各 Python 环境、前端依赖、Qdrant 与示例数据，返回 (node, pythons, qdrant)。"""
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
    """把当前启动阶段与端口写入 runtime.json，供外部查询访问地址。"""
    runtime_file.write_text(
        json.dumps({"status": status, "url": url(ports["web"]), "ports": ports}, indent=2),
        encoding="utf-8",
    )


def _start_qdrant(supervisor: Supervisor, base: dict, ports: dict[str, int], qdrant: Path) -> None:
    """启动受管 Qdrant 并等待其就绪。"""
    qdrant_env = native_environment.qdrant_environment(base, ports, ROOT)
    supervisor.start("qdrant", [qdrant], qdrant.parent, qdrant_env)
    supervisor.wait_http({"qdrant": url(ports["qdrant"]) + "/readyz"}, timeout=90)


def _start_backends(
    supervisor: Supervisor,
    pythons: dict[str, Path],
    ports: dict[str, int],
    environments: dict[str, dict],
) -> None:
    """依次启动 nexrag、webui、ingestion、retrieval 四个 uvicorn 后端。"""
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
    """用 vite 启动浏览器界面，端口被占用时直接失败。"""
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
    """等待界面、数据服务依次就绪并更新运行状态，之后持续监管子进程。"""
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
    """准备运行环境后依次启动 Ollama、Qdrant、各后端与前端并监管；退出时停止本次启动的服务并把状态写为 stopped。--prepare-only 时只准备不启动。"""
    base = prepare_environment()
    node, pythons, qdrant = _prepare_runtime(args, base)
    if args.prepare_only:
        say("Runtime prepared. Run start.cmd to start services.")
        return
    ports = pick_ports()
    supervisor = Supervisor()
    runtime_file = STATE / "runtime.json"
    try:
        # 先证明推理仅在本地，再启动任何可能把文档文本发给 embedding、
        # 或把业务证据发给对话模型的服务。
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
    """命令行入口：仅支持 Windows x64，在启动锁保护下调用 launch；成功或被中断返回 0，启动失败返回 1。"""
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
