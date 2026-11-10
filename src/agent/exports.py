"""Write agent-generated reports into a user-bound local directory.

Files live under EXPORTS_DIR/<user_id>/<chat_id or "_">/. Authenticated Web downloads
prevent cross-account access through guessed paths or filesystem links.
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from src.midplatform.workspace import export_root

MAX_EXPORT_BYTES = (
    5 * 1024 * 1024
)  # Limit each report to 5 MiB: exports are reports, not database dumps.
ALLOWED_FORMATS = {"csv", "json", "md", "txt"}
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class ExportSecurityError(ValueError):
    """Raise when an export request exceeds its allowed boundary."""


def _user_root(user_id: str) -> tuple[Path, Path]:
    root = export_root().resolve()
    if (
        not isinstance(user_id, str)
        or not user_id
        or user_id in {".", ".."}
        or len(user_id) > 128
        or user_id != _SAFE_NAME.sub("", user_id)
    ):
        raise ExportSecurityError("Invalid user identifier")
    user_entry = root / user_id
    user_root = user_entry.resolve()
    if user_root != user_entry or user_root == root or not user_root.is_relative_to(root):
        raise ExportSecurityError("User export folder escapes its bound root")
    return root, user_root


def _chat_dir(user_root: Path, chat_id: str | None) -> Path:
    safe_chat = _SAFE_NAME.sub("_", chat_id) if chat_id else "_"
    if safe_chat in {".", ".."}:
        safe_chat = "_"
    target = (user_root / safe_chat).resolve()
    if target == user_root or not target.is_relative_to(user_root):
        raise ExportSecurityError("Chat export folder escapes its owner folder")
    return target


def _user_dir(user_id: str, chat_id: str | None) -> Path:
    _, user_root = _user_root(user_id)
    target = _chat_dir(user_root, chat_id)
    target.mkdir(parents=True, exist_ok=True)
    return target


def _render(fmt: str, title: str, rows: list[dict[str, Any]] | None, text: str | None) -> bytes:
    if fmt == "csv":
        if not rows:
            raise ValueError("CSV export requires at least one row")
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
        return buffer.getvalue().encode("utf-8")
    if fmt == "json":
        payload = rows if rows is not None else {"title": title, "text": text}
        return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    if fmt == "md":
        body = text or ""
        if rows:
            headers = list(rows[0].keys())
            body += (
                "\n\n| "
                + " | ".join(headers)
                + " |\n| "
                + " | ".join("---" for _ in headers)
                + " |\n"
            )
            body += "\n".join(
                "| " + " | ".join(str(row.get(h, "")) for h in headers) + " |" for row in rows
            )
        return f"# {title}\n\n{body}\n".encode("utf-8")
    return (text or "").encode("utf-8")


def export_analysis(
    *,
    user_id: str,
    chat_id: str | None,
    title: str,
    fmt: str = "md",
    rows: list[dict[str, Any]] | None = None,
    text: str | None = None,
) -> dict[str, Any]:
    """Write one report and return its path and size for the tool result.

    Invalid input raises ExportSecurityError or ValueError; the agent wrapper converts these
    into tool errors rather than crashing.
    """
    fmt = fmt.lower()
    if fmt not in ALLOWED_FORMATS:
        raise ValueError(f"Unsupported export format {fmt!r}; use one of {sorted(ALLOWED_FORMATS)}")
    if rows is None and not (text or "").strip():
        raise ValueError("Nothing to export: provide rows or text")

    data = _render(fmt, title, rows, text)
    if len(data) > MAX_EXPORT_BYTES:
        raise ValueError(
            f"Export exceeds the {MAX_EXPORT_BYTES // 1024} KiB limit; narrow the data first"
        )

    directory = _user_dir(user_id, chat_id)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    slug = _SAFE_NAME.sub("-", title.strip())[:60].strip("-") or "export"
    filename = f"{stamp}-{slug}-{uuid.uuid4().hex[:8]}.{fmt}"
    path = directory / filename
    with path.open("xb") as stream:
        stream.write(data)
    return {
        "filename": filename,
        "path": str(path),
        "relative_path": path.relative_to(export_root().resolve()).as_posix(),
        "download_url": "/api/nexrag-download/"
        + path.relative_to(export_root().resolve()).as_posix(),
        "size_bytes": len(data),
        "format": fmt,
    }


def list_exports(user_id: str, chat_id: str | None = None) -> list[dict[str, Any]]:
    """List exports visible to this user, optionally restricted to one conversation."""
    bound_root, root = _user_root(user_id)
    if not root.exists():
        return []
    search_dirs = (
        [_chat_dir(root, chat_id)] if chat_id else [p for p in root.iterdir() if p.is_dir()]
    )
    items: list[dict[str, Any]] = []
    for directory in search_dirs:
        if not directory.exists() or not directory.resolve().is_relative_to(root):
            continue
        for path in sorted(directory.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True):
            if path.is_file() and path.resolve().is_relative_to(root):
                items.append(
                    {
                        "filename": path.name,
                        "relative_path": path.relative_to(bound_root).as_posix(),
                        "size_bytes": path.stat().st_size,
                        "modified_at": path.stat().st_mtime,
                    }
                )
    return items
