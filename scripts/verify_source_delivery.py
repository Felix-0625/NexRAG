"""Export current deliverable source into a fresh native acceptance directory.

Do not commit or clone. Snapshot tracked and product-added files, excluding machine state,
and record SHA-256 hashes. Run the exported start.cmd separately to test clean bootstrap
without generated WebUI, virtual environments, accounts, or service secrets.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

PRODUCT_ADDITIONS = {"base", "docs", "evals", "patches", "scripts", "src", "tests"}
ROOT_ADDITIONS = {"start.cmd", "start.ps1", "SECURITY.md", "CONTRIBUTING.md", "NOTICE.md"}
FORBIDDEN_PARTS = {".git", ".local-run", ".venv", "webui", "node_modules", ".claude", ".bug-hunter"}
FORBIDDEN_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".pem", ".key", ".log", ".pyc"}
SECRET_NAMES = {
    "NEXRAG_SHARED_API_KEY",
    "NEXRAG_USER_JWT_SECRET",
    "WEBUI_SECRET_KEY",
    "INGESTION_API_KEY",
    "RETRIEVAL_API_KEY",
    "BUSINESS_DB_DSN",
}


def git_paths(root: Path, *arguments: str) -> list[str]:
    """Run git ls-files with extra arguments and return repository-relative paths."""
    output = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z", *arguments])
    return [item for item in output.decode("utf-8").split("\0") if item]


def local_secret_values(root: Path) -> list[bytes]:
    """Compare only known machine secrets without printing or copying their values."""
    config = root / ".env"
    if not config.exists():
        return []
    values = []
    for line in config.read_text(encoding="utf-8").splitlines():
        name, separator, value = line.partition("=")
        secret_name = name.strip()
        if separator and (
            secret_name in SECRET_NAMES
            or secret_name.endswith(("_KEY", "_SECRET", "_PASSWORD", "_TOKEN"))
        ):
            value = value.strip().strip("\"'")
            if len(value) >= 16:
                values.append(value.encode())
    return values


def export_source(root: Path, destination: Path) -> dict:
    """Export a fresh local snapshot with hashes, rejecting secrets, unsafe links, and concurrent
    source changes.
    """
    root, destination = root.resolve(), destination.resolve()
    acceptance_root = (root / ".local-run").resolve()
    if not destination.is_relative_to(acceptance_root):
        raise ValueError("Acceptance source must stay inside this checkout's .local-run")
    if destination.exists():
        raise ValueError("Destination must be new; existing acceptance data is never replaced")
    tracked = set(git_paths(root, "--cached"))
    additions = git_paths(root, "--others", "--exclude-standard")
    permitted = tracked | {
        name
        for name in additions
        if Path(name).parts[0] in PRODUCT_ADDITIONS or name in ROOT_ADDITIONS
    }
    secrets = local_secret_values(root)
    files, omitted = [], []
    destination.mkdir(parents=True)
    for name in sorted(permitted):
        relative = Path(name)
        source = root / relative
        blocked = (
            any(part in FORBIDDEN_PARTS for part in relative.parts)
            or (relative.name.startswith(".env") and relative.name != ".env.example")
            or relative.suffix.lower() in FORBIDDEN_SUFFIXES
            or (relative.parts[0] == "data" and relative.name != ".gitkeep")
        )
        if blocked or not source.is_file():
            omitted.append(name)
            continue
        if source.is_symlink() or not source.resolve().is_relative_to(root):
            raise ValueError(f"Source link is not deliverable: {name}")
        content = source.read_bytes()
        if any(secret in content for secret in secrets):
            raise ValueError(f"Known local credential found in source: {name}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        shutil.copymode(source, target)
        files.append(
            {
                "path": relative.as_posix(),
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
                "tracked": name in tracked,
            }
        )
    # Detect concurrent edits rather than labeling a mixed-state export as consistent.
    for item in files:
        if hashlib.sha256((root / item["path"]).read_bytes()).hexdigest() != item["sha256"]:
            raise RuntimeError(
                f"Source changed during export: {item['path']}; create a new snapshot"
            )
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_type": "current-worktree-deliverable-export-not-remote-clone",
        "source": str(destination),
        "file_count": len(files),
        "source_bytes": sum(item["bytes"] for item in files),
        "known_local_credentials_found": False,
        "generated_runtime_copied": False,
        "files": files,
        "omitted": omitted,
        "limits": [
            "No commit or push performed",
            "Export does not prove remote Git state",
            "Bootstrap and browser acceptance must be run separately",
        ],
    }
    manifest = destination.parent / "source-manifest.json"
    manifest.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    folder = (
        args.destination
        or repository / ".local-run" / f"clean-acceptance-{uuid4().hex[:12]}" / "source"
    )
    result = export_source(repository, folder)
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "snapshot_type",
                    "source",
                    "file_count",
                    "source_bytes",
                    "known_local_credentials_found",
                    "generated_runtime_copied",
                )
            },
            indent=2,
        )
    )
