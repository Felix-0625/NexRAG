"""Inspect temporary tables and text in a dedicated sandbox.

Bound paths, bytes, and rows to prevent traversal, secret disclosure, and context overflow.
Never use the process working directory: outside an empty container this exposes source
and configuration files.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

FORBIDDEN_FILE_PATTERNS = {
    ".env",
    ".git",
    ".venv",
    ".secrets",
}

FORBIDDEN_EXTENSIONS = {
    ".key",
    ".pem",
    ".p12",
    ".crt",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".yaml",
    ".yml",
    ".py",
}


class FileSandboxSecurityError(ValueError):
    """Reject file operations outside sandbox boundaries or access policy."""


def validate_safe_path(
    file_path: str | Path,
    base_dir: Path,
) -> Path:
    """Require resolved targets inside an explicitly supplied dedicated sandbox root."""
    root = base_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = (root / file_path if not Path(file_path).is_absolute() else Path(file_path)).resolve()

    # Require root containment to prevent traversal.
    if not target.is_relative_to(root):
        raise FileSandboxSecurityError(
            f"Access denied: path {file_path!r} resolves outside sandbox root ({root})"
        )

    # Block sensitive components and extensions even inside the sandbox.
    for part in target.parts:
        for forbidden in FORBIDDEN_FILE_PATTERNS:
            if part == forbidden or part.startswith(f"{forbidden}."):
                raise FileSandboxSecurityError(
                    f"Access denied: sensitive path component {part!r} is restricted"
                )

    if target.suffix.lower() in FORBIDDEN_EXTENSIONS:
        raise FileSandboxSecurityError(
            f"Access denied: file extension {target.suffix!r} is restricted"
        )

    if not target.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    if not target.is_file():
        raise FileSandboxSecurityError(f"Target is not a regular file: {file_path}")

    return target


def _inspect_tabular(path: Path, ext: str, clamped_rows: int) -> dict[str, Any]:
    """Read CSV or TSV headers, row counts, and bounded samples."""
    delimiter = "\t" if ext == ".tsv" else ","
    try:
        with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
            reader = csv.reader(handle, delimiter=delimiter)
            headers = next(reader, None)
            if headers is None:
                return {
                    "filename": path.name,
                    "format": "tabular",
                    "subtype": ext.lstrip("."),
                    "total_rows": 0,
                    "columns": [],
                    "sample_rows": [],
                    "summary": "Empty tabular file.",
                }

            sample_rows: list[dict[str, str]] = []
            row_count = 0
            for row in reader:
                row_count += 1
                if len(sample_rows) < clamped_rows:
                    # Keep malformed sample rows aligned with the header.
                    row_dict = {
                        headers[i]: row[i] if i < len(row) else "" for i in range(len(headers))
                    }
                    sample_rows.append(row_dict)

            return {
                "filename": path.name,
                "format": "tabular",
                "subtype": ext.lstrip("."),
                "total_rows": row_count,
                "columns": headers,
                "column_count": len(headers),
                "sample_rows": sample_rows,
                "sampled_count": len(sample_rows),
            }
    except Exception as exc:
        return {
            "filename": path.name,
            "error": f"Failed to parse tabular file: {exc}",
        }


def _inspect_json(path: Path, clamped_rows: int) -> dict[str, Any]:
    """Summarize JSON arrays, objects, or primitives within limits."""
    try:
        raw_text = path.read_text(encoding="utf-8", errors="replace")
        parsed = json.loads(raw_text)
        if isinstance(parsed, list):
            total_items = len(parsed)
            sample = parsed[:clamped_rows]

            columns = list(sample[0].keys()) if sample and isinstance(sample[0], dict) else []
            return {
                "filename": path.name,
                "format": "json_array",
                "total_items": total_items,
                "columns": columns,
                "sample_items": sample,
            }
        if isinstance(parsed, dict):
            return {
                "filename": path.name,
                "format": "json_object",
                "top_level_keys": list(parsed.keys()),
                "key_count": len(parsed),
                "summary_preview": {k: type(v).__name__ for k, v in list(parsed.items())[:20]},
            }
        return {
            "filename": path.name,
            "format": "json_primitive",
            "value": parsed,
        }
    except Exception as exc:
        return {
            "filename": path.name,
            "error": f"Failed to parse JSON file: {exc}",
        }


def _inspect_text(path: Path, ext: str, start_line_idx: int, clamped_lines: int) -> dict[str, Any]:
    """Read a line window from text, Markdown, or logs."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            all_lines = handle.readlines()

        total_lines = len(all_lines)
        start_idx = start_line_idx - 1
        end_idx = min(start_idx + clamped_lines, total_lines)
        sliced = all_lines[start_idx:end_idx] if start_idx < total_lines else []

        return {
            "filename": path.name,
            "format": "text",
            "subtype": ext.lstrip(".") or "plain",
            "total_lines": total_lines,
            "start_line": start_line_idx,
            "returned_lines": len(sliced),
            "content": "".join(sliced),
            "has_more": end_idx < total_lines,
        }
    except Exception as exc:
        return {
            "filename": path.name,
            "error": f"Failed to read file: {exc}",
        }


def inspect_file_content(
    file_path: str | Path,
    *,
    base_dir: Path,
    max_rows: int = 10,
    start_line: int = 1,
    max_lines: int = 50,
) -> dict[str, Any]:
    """Inspect and sample safe local files without overloading model context."""
    path = validate_safe_path(file_path, base_dir=base_dir)
    ext = path.suffix.lower()
    clamped_rows = max(1, min(max_rows, 50))
    clamped_lines = max(1, min(max_lines, 200))
    start_line_idx = max(1, start_line)

    if ext in {".csv", ".tsv"}:
        return _inspect_tabular(path, ext, clamped_rows)

    if ext == ".json":
        return _inspect_json(path, clamped_rows)

    return _inspect_text(path, ext, start_line_idx, clamped_lines)
