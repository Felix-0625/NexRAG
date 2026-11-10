"""Split JSONL records only on actual newlines, preserving Unicode paragraph characters in text."""

from __future__ import annotations

import json
from pathlib import Path


def read_jsonl(path: Path) -> list[dict]:
    # str.splitlines() also splits U+0085/U+2028/U+2029 inside valid JSON
    # strings. Official legal source names can contain those characters.
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def jsonl_line(value: object) -> str:
    # Keep exports compatible with external line-oriented readers while
    # preserving the exact decoded question/reference strings.
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("\u0085", "\\u0085")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
        + "\n"
    )
