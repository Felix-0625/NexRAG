"""Check delivery files, code size, documentation links, and release-version consistency."""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
CODE_SUFFIXES = {".py", ".js", ".ts", ".svelte", ".ps1", ".cjs", ".mjs", ".cmd"}


def check_repository() -> str:
    """Reject runtime files, oversized source files, and broken project links; return the version."""
    paths = (
        subprocess.check_output(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT
        )
        .decode()
        .split("\0")
    )
    errors = []
    for name in filter(None, paths):
        path = ROOT / name
        if not path.is_file():
            continue
        parts = Path(name).parts
        if (
            any(
                part
                in {
                    ".env",
                    ".venv",
                    "node_modules",
                    "webui",
                    ".local-run",
                    ".bug-hunter",
                    "__pycache__",
                }
                for part in parts
            )
            or (path.name.startswith(".env") and path.name != ".env.example")
            or path.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".pyc", ".pem", ".key", ".log"}
            or (parts[0] == "data" and path.name != ".gitkeep")
        ):
            errors.append(f"Runtime or credential file in delivery: {name}")
        if path.suffix.lower() in CODE_SUFFIXES:
            line_count = len(path.read_text(encoding="utf-8-sig").splitlines())
            if line_count > 1000:
                errors.append(f"Code file exceeds 1000 lines ({line_count}): {name}")
        if path.suffix != ".md" or parts[0] == "base":
            continue
        for target in re.findall(
            r"\[[^\]\n]*\]\(([^)\n]+)\)", path.read_text(encoding="utf-8-sig")
        ):
            target = target.strip().strip("<>")
            parsed = urlsplit(target)
            if parsed.scheme or target.startswith("#") or not parsed.path:
                continue
            resolved = (path.parent / unquote(parsed.path)).resolve()
            if not resolved.is_relative_to(ROOT) or not resolved.exists():
                errors.append(f"Broken local link in {name}: {target}")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    version = project["version"]
    module = ast.parse((ROOT / "src/__init__.py").read_text(encoding="utf-8"))
    runtime_version = next(
        ast.literal_eval(node.value)
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        )
    )
    if version != runtime_version:
        errors.append("pyproject.toml and src.__version__ differ")
    if errors:
        raise ValueError("\n".join(errors))
    return version


def main() -> None:
    """Run the command-line entry point with parsed options."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-tag")
    parser.add_argument("--notes-out", type=Path)
    args = parser.parse_args()
    version = check_repository()
    if args.release_tag:
        if args.release_tag != f"v{version}":
            parser.error(f"Release tag must be v{version}")
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        match = re.search(
            rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}\s*\n(.*?)(?=^## |\Z)",
            changelog,
            flags=re.M | re.S,
        )
        if not match or not match.group(1).strip():
            parser.error("The release requires a dated, nonempty CHANGELOG section")
        if args.notes_out:
            args.notes_out.write_text(match.group(1).strip() + "\n", encoding="utf-8")
    elif args.notes_out:
        parser.error("--notes-out requires --release-tag")
    print(f"Repository and documentation checks passed for {version}")


if __name__ == "__main__":
    main()
