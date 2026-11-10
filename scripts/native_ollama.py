"""Verify local-only Ollama and prepare models without touching other services."""

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
    """Trust explicit server status, not loopback addressing alone.

    Ollama can proxy cloud models through localhost; reject older servers without a status
    endpoint because they cannot prove local-only execution.
    """
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(endpoint.rstrip("/") + "/api/status", timeout=3) as response:
            return json.load(response).get("cloud", {}).get("disabled") is True
    except (OSError, ValueError, urllib.error.URLError):
        return False


def _local_ollama_endpoints(base: dict) -> set[str]:
    """Collect configured local endpoints using Ollama's default port."""
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
    """Find Ollama on PATH or in its default install directory, otherwise return None."""
    ollama = shutil.which("ollama")
    if not ollama:
        candidate = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Ollama/ollama.exe"
        ollama = str(candidate) if candidate.is_file() else None
    return ollama


def _install_ollama(state: Path, release_json, release_zip) -> str:
    """Download an official Ollama release into the checkout and return its executable."""
    folder = state / "tools/ollama"
    if not (folder / "ollama.exe").exists():
        release = release_json("https://api.github.com/repos/ollama/ollama/releases/latest")
        release_zip("ollama/ollama", release["tag_name"], "ollama-windows-amd64.zip", folder)
    return str(folder / "ollama.exe")


def _start_owned_ollama(base: dict, supervisor, ollama: str, root: Path, report) -> str:
    """Start launcher-owned Ollama on loopback with cloud features disabled."""
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
    """Record verified local-only endpoints for other processes."""
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
    """Collect model names configured to use verified endpoints."""
    desired = set()
    if base.get("LLM_API_BASE", "http://localhost:11434/v1") == endpoint + "/v1":
        desired.add(base.get("LLM_MODEL_NAME", "qwen3:4b-instruct"))
    if base.get("EMBEDDING_API_BASE_URL", "http://localhost:11434/v1") == endpoint + "/v1":
        desired.add(base.get("EMBEDDING_MODEL", "bge-m3"))
    return desired


def _pull_model(opener, endpoint: str, model: str, supervisor, report) -> None:
    """Pull a model through streaming HTTP with periodic progress reports."""
    report(f"Downloading local model {model}; this first download can be several GB...")
    # The API works even if the running server's CLI is absent from PATH.
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
    """Verify local Ollama has cloud disabled, install or start an owned instance if needed, and
    pull models.
    """
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
    # Build service environments only after this returns so the core can resolve verified
    # managed aliases without rewriting stored model credentials or unrelated projects.
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
