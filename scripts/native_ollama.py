"""验证 Ollama 仅在本地运行并准备模型，不触碰其他服务。"""

import json
import os
import shutil
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


def ollama_cloud_disabled(endpoint: str, opener=None) -> bool:
    """以服务端明确返回的状态为准，不能只凭回环地址就信任。

    Ollama 可以通过 localhost 代理云端模型。没有该状态端点的旧版本
    无法证明"仅本地"的约定，因此会被拒绝。
    """
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(endpoint.rstrip("/") + "/api/status", timeout=3) as response:
            return json.load(response).get("cloud", {}).get("disabled") is True
    except (OSError, ValueError, urllib.error.URLError):
        return False


def _local_ollama_endpoints(base: dict) -> set[str]:
    """配置中指向本机默认 Ollama 端口（11434）的端点集合。"""
    endpoints = {
        base.get("LLM_API_BASE", "http://localhost:11434/v1"),
        base.get("EMBEDDING_API_BASE_URL", "http://localhost:11434/v1"),
    }
    return {
        endpoint
        for endpoint in endpoints
        if urlparse(endpoint).hostname in {"localhost", "127.0.0.1"}
        and urlparse(endpoint).port == 11434
    }


def _find_ollama() -> str | None:
    """优先用 PATH 上的 ollama，其次是默认安装目录；都没有则返回 None。"""
    ollama = shutil.which("ollama")
    if not ollama:
        candidate = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Ollama/ollama.exe"
        ollama = str(candidate) if candidate.is_file() else None
    return ollama


def _install_ollama(state: Path, release_json, release_zip) -> str:
    """从官方发布页下载并解压 Ollama 到检出目录内，返回可执行文件路径。"""
    folder = state / "tools/ollama"
    if not (folder / "ollama.exe").exists():
        release = release_json("https://api.github.com/repos/ollama/ollama/releases/latest")
        release_zip("ollama/ollama", release["tag_name"], "ollama-windows-amd64.zip", folder)
    return str(folder / "ollama.exe")


def _start_owned_ollama(base: dict, supervisor, ollama: str, root: Path, report) -> str:
    """在回环地址上启动由本启动器管理的 Ollama（禁用云功能），返回其端点。"""
    report("Starting the local model service...")
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", 11434))
        except OSError:
            probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    endpoint = f"http://127.0.0.1:{port}"
    supervisor.start(
        "ollama",
        [ollama, "serve"],
        root,
        dict(
            base,
            OLLAMA_HOST=f"127.0.0.1:{port}",
            OLLAMA_NO_CLOUD="1",
            OLLAMA_CONTEXT_LENGTH=base.get("OLLAMA_CONTEXT_LENGTH", "16384"),
        ),
    )
    supervisor.wait_http({"ollama": endpoint + "/api/tags"}, timeout=120)
    return endpoint


def _write_trust_record(state: Path, endpoint: str, owned: bool) -> None:
    """记录已验证"仅本地"的 Ollama 端点，供其他进程读取。"""
    state.mkdir(parents=True, exist_ok=True)
    (state / "ollama-trust.json").write_text(
        json.dumps(
            {
                "endpoint": endpoint,
                "cloud_disabled": True,
                "owned": owned,
                "verified_at": time.time(),
            },
            indent=2,
        ),
        encoding="utf8",
    )


def _desired_models(base: dict, endpoint: str) -> set[str]:
    """配置里指向已验证端点的对话/向量模型名称集合。"""
    desired = set()
    if base.get("LLM_API_BASE", "http://localhost:11434/v1") == endpoint + "/v1":
        desired.add(base.get("LLM_MODEL_NAME", "qwen3:4b-instruct"))
    if base.get("EMBEDDING_API_BASE_URL", "http://localhost:11434/v1") == endpoint + "/v1":
        desired.add(base.get("EMBEDDING_MODEL", "bge-m3"))
    return desired


def _pull_model(opener, endpoint: str, model: str, supervisor, report) -> None:
    """通过 /api/pull 流式下载模型，每 15 秒汇报一次进度。"""
    report(f"Downloading local model {model}; this first download can be several GB...")
    # 即使运行中的 Ollama 服务在 PATH 上没有 CLI，也可以使用其 API。
    request = urllib.request.Request(
        endpoint + "/api/pull",
        data=json.dumps({"name": model, "stream": True}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with opener.open(request, timeout=120) as response:
        last_report = 0.0
        for line in response:
            supervisor.check()
            progress = json.loads(line)
            if progress.get("error"):
                raise RuntimeError(f"Ollama model download failed: {progress['error']}")
            if time.monotonic() - last_report > 15:
                report(f"{model}: {progress.get('status', 'downloading')}")
                last_report = time.monotonic()


def prepare_ollama(
    base: dict,
    supervisor,
    skip_models: bool,
    *,
    root: Path,
    state: Path,
    report,
    cloud_disabled,
    release_json,
    release_zip,
) -> None:
    """配置指向本机 Ollama 时，确认其已禁用云功能（必要时安装并启动受管实例，否则抛出 RuntimeError），记录信任并拉取缺失模型；无本机端点则直接返回。"""
    local = _local_ollama_endpoints(base)
    if not local:
        return
    ollama = _find_ollama()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    endpoint = "http://127.0.0.1:11434"
    owned = False
    if not cloud_disabled(endpoint, opener):
        if not ollama:
            ollama = _install_ollama(state, release_json, release_zip)
        endpoint = _start_owned_ollama(base, supervisor, ollama, root, report)
        owned = True
        if not cloud_disabled(endpoint, opener):
            raise RuntimeError(
                "Ollama local-only status could not be verified. Install a current Ollama version supporting OLLAMA_NO_CLOUD and /api/status."
            )
    with opener.open(endpoint + "/api/tags", timeout=5) as response:
        models = json.load(response)
    # 所有服务的环境变量都在本函数返回之后才构建。核心服务可借助这个已验证的
    # 受管 URL 解析已保存的默认回环别名，无需改写已存的模型凭据或其他项目。
    for name in ("LLM_API_BASE", "EMBEDDING_API_BASE_URL"):
        if base.get(name) in local:
            base[name] = endpoint + "/v1"
    base["NEXRAG_MANAGED_OLLAMA_API_BASE"] = endpoint + "/v1"
    _write_trust_record(state, endpoint, owned)
    report("Verified local-only Ollama: cloud features disabled.")
    if skip_models:
        report(
            "Model downloads skipped (--skip-models); model-dependent features require an available model."
        )
        return
    installed = {item["name"] for item in models.get("models", [])}
    for model in sorted(_desired_models(base, endpoint)):
        if model in installed or model + ":latest" in installed:
            continue
        _pull_model(opener, endpoint, model, supervisor, report)
