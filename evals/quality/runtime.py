"""Manage native services in isolated data folders; supervise only newly created process trees."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import socket
import tempfile
import zipfile
from pathlib import Path

import httpx
from dotenv import dotenv_values

from scripts import native_environment as native
from scripts.launch_local import SECRET_NAMES
from scripts.native_processes import Supervisor

ROOT = Path(__file__).resolve().parents[2]


class NativeRuntime:
    def __init__(
        self,
        run: Path,
        *,
        config: dict | None = None,
        cuda_packages: Path | None = None,
        cuda_adapter: Path | None = None,
        resume_from: Path | None = None,
    ):
        self.run = run.resolve()
        self.run.mkdir(parents=True, exist_ok=False)
        if resume_from:
            previous = resume_from.resolve(strict=True)
            receipt = json.loads((previous / "source-preservation.json").read_text("utf-8"))
            location = json.loads((previous / "runtime-location.json").read_text("utf-8"))
            self.data = Path(location["data_root"]).resolve(strict=True)
            owned = (ROOT / ".local-run").resolve()
            if (
                not receipt.get("owned_services_stopped")
                or self.data.parent != owned
                or not self.data.name.startswith("q-")
            ):
                raise ValueError("Resume requires a stopped, task-owned isolated quality runtime")
            (self.run / "runtime-lineage.json").write_text(
                json.dumps(
                    {
                        "previous_run": str(previous),
                        "data_root": str(self.data),
                        "kind": "development-continuation",
                        "formal_release_eligible": False,
                        "previous_receipt_sha256": hashlib.sha256(
                            (previous / "source-preservation.json").read_bytes()
                        ).hexdigest(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        else:
            self.data = Path(tempfile.mkdtemp(prefix="q-", dir=ROOT / ".local-run"))
        self.supervisor = Supervisor(self.run, print)
        self.ports = native.pick_ports()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.ports["ollama"] = sock.getsockname()[1]
        if len(set(self.ports.values())) != len(self.ports):
            raise RuntimeError("Isolated service port collision")
        self.base = {k: v for k, v in dotenv_values(ROOT / ".env.example").items() if v is not None}
        self.base.update(
            {
                k: v
                for k, v in os.environ.items()
                if k.upper()
                in {
                    "PATH",
                    "SYSTEMROOT",
                    "WINDIR",
                    "USERPROFILE",
                    "TEMP",
                    "TMP",
                    "LOCALAPPDATA",
                    "APPDATA",
                    "COMSPEC",
                    "PROGRAMDATA",
                }
            }
        )
        self.base.update({key: secrets.token_hex(32) for key in SECRET_NAMES})
        ollama = self.url("ollama")
        self.base.update(
            PYTHONIOENCODING="utf-8",
            PYTHONUNBUFFERED="1",
            PYTHON_DOTENV_DISABLED="1",
            HF_HOME=str(ROOT / ".local-run/cache/huggingface"),
            FASTEMBED_CACHE_DIR=str(ROOT / ".local-run/cache/fastembed"),
            HF_HUB_OFFLINE="1",
            HF_HUB_DISABLE_TELEMETRY="1",
            ANONYMIZED_TELEMETRY="false",
            HAYSTACK_TELEMETRY_ENABLED="false",
            OMP_NUM_THREADS="2",
            OPENBLAS_NUM_THREADS="2",
            BUSINESS_SOURCE_PATH=str(ROOT / "examples/olist/business-source.yaml"),
            METADATA_PATH=str(ROOT / "examples/olist/metadata.yaml"),
            TENANT_ACCESS_PATH=str(ROOT / "examples/olist/tenant-access.yaml"),
            BUSINESS_DB_PATH=str(self.data / "data/empty-business.db"),
            OPS_DB_PATH=str(self.data / "data/ops/ops.db"),
            EMBEDDING_API_BASE_URL=ollama + "/v1",
            LLM_API_BASE=ollama + "/v1",
            NEXRAG_MANAGED_OLLAMA_API_BASE=ollama + "/v1",
            ENABLE_MEMORY="false",
            ENABLE_WEB_SEARCH="false",
            TRACE_STORE_CONTENT="none",
            STORAGE_PROVIDER="local",
            ENABLE_FILENAME_RETRIEVAL="true",
            ENABLE_TITLE_ROUTING="false",
            CHUNK_SIZE="400",
            CHUNK_OVERLAP="80",
            PARENT_CHUNK_SIZE="1200",
            ENABLE_CONTEXT_WINDOWS="false",
            RETRIEVAL_CONTEXT_MODE="parent",
            RERANK_TOP_K="10",
            KNOWLEDGE_TOP_K="10",
            INITIAL_RETRIEVAL_MULTIPLIER="5",
            RETRIEVAL_CANDIDATE_LIMIT="50",
            RERANK_FUSION="rrf",
            RERANK_DEDUP_BEFORE_LIMIT="true",
            HYBRID_FUSION="rrf",
        )
        if config:
            allowed = {
                k
                for k in self.base
                if k.startswith(("CHUNK_", "PARENT_", "RERANK_", "HYBRID_", "RETRIEVAL_"))
            }
            allowed |= {
                "ENABLE_CONTEXT_WINDOWS",
                "CONTEXT_WINDOW_SIZE",
                "ENABLE_FILENAME_RETRIEVAL",
                "ENABLE_TITLE_ROUTING",
            }
            allowed = {
                key
                for key in allowed
                if not any(part in key for part in ("API_", "SECRET", "PASSWORD"))
            }
            if set(config) - allowed:
                raise ValueError("Only explicitly allowed retrieval/ingestion parameters may vary")
            self.base.update({k: str(v) for k, v in config.items()})
        if bool(cuda_packages) != bool(cuda_adapter):
            raise ValueError(
                "CUDA packages and an explicit execution adapter are required together"
            )
        if cuda_packages:
            frozen_adapter = self.run / "cuda-execution-adapter.py"
            shutil.copyfile(cuda_adapter.resolve(strict=True), frozen_adapter)
            self.base.update(
                QUALITY_CUDA_PACKAGES=str(cuda_packages.resolve(strict=True)),
                QUALITY_CUDA_ADAPTER=str(frozen_adapter),
            )
        self.environments = native.service_environments(
            self.base, self.ports, root=self.data, state=self.run, report=print
        )
        for name, directory in (
            ("retrieval", "retrieval-agent"),
            ("ingestion", "ingestion-service"),
        ):
            self.environments[name]["PYTHONPATH"] = os.pathsep.join(
                [str(ROOT), str(ROOT / "base" / directory)]
            )
        self.environments["nexrag"]["PYTHONPATH"] = str(ROOT)
        web = self.environments["webui"]
        web.update(
            FRONTEND_BUILD_DIR=str(ROOT / ".local-run/webui-assets"),
            DATABASE_URL="sqlite:///" + (self.data / "data/openwebui/webui.db").as_posix(),
            ENABLE_PERSISTENT_CONFIG="false",
            PYTHONPATH=str(ROOT / "webui/backend"),
        )
        (self.run / "runtime-location.json").write_text(
            json.dumps({"data_root": str(self.data), "ports": self.ports}), encoding="utf-8"
        )
        safe_config = {
            key: value
            for key, value in self.base.items()
            if key.startswith(
                (
                    "CHUNK_",
                    "PARENT_",
                    "RERANK_",
                    "HYBRID_",
                    "RETRIEVAL_",
                    "EMBEDDING_",
                    "ENABLE_",
                    "KNOWLEDGE_",
                )
            )
            and not any(part in key for part in ("API_KEY", "TOKEN", "SECRET", "PASSWORD"))
        }
        environment = self.run.parent / "repro-environment.json"
        (self.run / "effective-config.json").write_text(
            json.dumps(
                {
                    "settings": safe_config,
                    "model_and_dependency_fingerprint_sha256": hashlib.sha256(
                        environment.read_bytes()
                    ).hexdigest()
                    if environment.exists()
                    else None,
                    "cuda_adapter_sha256": hashlib.sha256(frozen_adapter.read_bytes()).hexdigest()
                    if cuda_packages
                    else None,
                    "embedding_query_cache": False,
                    "pair_score_cache": False,
                    "answer_cache": False,
                    "ollama_context_length": 16384,
                    "ollama_num_parallel": 1,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def url(self, service: str) -> str:
        return native.url(self.ports[service])

    def start(self) -> None:
        try:
            self.supervisor.start(
                "qdrant",
                [ROOT / ".local-run/tools/qdrant-1.19.1/qdrant.exe"],
                self.data,
                native.qdrant_environment(self.base, self.ports, self.data),
            )
            self.supervisor.start(
                "ollama",
                [Path(self.base["LOCALAPPDATA"]) / "Programs/Ollama/ollama.exe", "serve"],
                self.run,
                dict(
                    self.base,
                    OLLAMA_HOST=self.url("ollama"),
                    OLLAMA_NO_CLOUD="1",
                    OLLAMA_CONTEXT_LENGTH="16384",
                    OLLAMA_NUM_PARALLEL="1",
                    OLLAMA_MODELS=str(Path.home() / ".ollama/models"),
                ),
            )
            self.supervisor.wait_http(
                {
                    "qdrant": self.url("qdrant") + "/readyz",
                    "ollama": self.url("ollama") + "/api/status",
                },
                timeout=90,
            )
            with httpx.Client(trust_env=False, timeout=10) as client:
                if (
                    client.get(self.url("ollama") + "/api/status").json()["cloud"]["disabled"]
                    is not True
                ):
                    raise RuntimeError("Isolated model service is not cloud-disabled")
            for name, venv, arguments in (
                (
                    "nexrag",
                    "core-security",
                    ["-m", "evals.quality.worker", "agent", str(self.ports["nexrag"])],
                ),
                (
                    "webui",
                    "webui",
                    [
                        "-m",
                        "uvicorn",
                        "open_webui.main:app",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(self.ports["webui"]),
                    ],
                ),
                (
                    "ingestion",
                    "ingestion-pillow13",
                    ["-m", "evals.quality.worker", "ingestion", str(self.ports["ingestion"])],
                ),
                (
                    "retrieval",
                    "retrieval-pillow13",
                    ["-m", "evals.quality.worker", "retrieval", str(self.ports["retrieval"])],
                ),
            ):
                self.supervisor.start(
                    name,
                    [ROOT / f".local-run/venvs/{venv}/Scripts/python.exe", *arguments],
                    self.run,
                    self.environments[name],
                )
            self.supervisor.wait_http(
                {
                    name: self.url(name) + path
                    for name, path in {
                        "nexrag": "/health",
                        "webui": "/health",
                        "ingestion": "/health/ready",
                        "retrieval": "/health/ready",
                    }.items()
                },
                timeout=240,
            )
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        self.supervisor.close()


def source_fingerprint() -> dict:
    """Log only source hashes; exclude instance credentials, business databases and corpus text."""
    files = {}
    for directory in (
        "src",
        "scripts",
        "base/retrieval-agent/app",
        "base/ingestion-service/app",
        "evals/quality",
        "webui/backend/open_webui",
    ):
        for path in sorted((ROOT / directory).rglob("*.py")):
            files[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    for path in sorted((ROOT / "patches/open-webui/v0.11.4/nexrag").rglob("*.diff")):
        files[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    for relative in (
        ".env.example",
        "pyproject.toml",
        "uv.lock",
        "base/ingestion-service/pyproject.toml",
        "base/ingestion-service/uv.lock",
        "base/retrieval-agent/pyproject.toml",
        "base/retrieval-agent/uv.lock",
    ):
        path = ROOT / relative
        if path.exists():
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    data = json.dumps(files, sort_keys=True).encode()
    return {"sha256": hashlib.sha256(data).hexdigest(), "files": files}


def archive_source_epoch(run: Path, fingerprint: dict) -> None:
    """Keep source bytes loaded at service launch; later edits must not overwrite past evidence."""
    with zipfile.ZipFile(
        run / "loaded-source-epoch.zip", "x", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        for relative, expected in fingerprint["files"].items():
            data = (ROOT / relative).read_bytes()
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError("Source changed while archiving the launch epoch")
            archive.writestr(relative, data)
