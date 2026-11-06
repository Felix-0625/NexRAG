"""沙箱化的文件检查器，用于临时的表格和文本文件。

严格限制路径范围、按大小/行数截断并检测格式，以防路径穿越、密钥泄露和上下文
窗口被撑满。

沙箱根目录始终是显式指定的专用目录（见 ``src.config.settings.SANDBOX_ROOT_DIR``），
而不是进程工作目录。若退回工作目录，服务在容器那个本来为空的工作目录之外运行时，
工具就能触及应用源码和配置文件。
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
    """文件操作违反沙箱边界或访问策略时抛出。"""


def validate_safe_path(
    file_path: str | Path,
    base_dir: Path,
) -> Path:
    """校验目标路径解析后位于允许的沙箱边界之内。

    ``base_dir`` 为必填：调用方必须传入专用的沙箱目录，
    而不是依赖与进程所在位置绑定的隐式默认值。
    """
    root = base_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = (root / file_path if not Path(file_path).is_absolute() else Path(file_path)).resolve()

    # 校验位于根目录边界之内，以防路径穿越
    if not target.is_relative_to(root):
        raise FileSandboxSecurityError(
            f"Access denied: path {file_path!r} resolves outside sandbox root ({root})"
        )

    # 检查是否含被禁止的敏感路径成分或扩展名
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
    """读取 CSV / TSV 的表头、总行数和采样行。"""
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
                    # 补齐或截断行，使其与表头长度一致
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
    """解析 JSON 文件，按数组/对象/基本类型分别整理结果。"""
    try:
        raw_text = path.read_text(encoding="utf-8", errors="replace")
        parsed = json.loads(raw_text)
        if isinstance(parsed, list):
            total_items = len(parsed)
            sample = parsed[:clamped_rows]
            # 若是字典列表，则提取列名
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
    """按行读取纯文本、Markdown、日志等文档的指定窗口。"""
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
    """检查并采样安全的本地文件，避免加载过多上下文。"""
    path = validate_safe_path(file_path, base_dir=base_dir)
    ext = path.suffix.lower()
    clamped_rows = max(1, min(max_rows, 50))
    clamped_lines = max(1, min(max_lines, 200))
    start_line_idx = max(1, start_line)

    # 表格类 CSV / TSV 文件
    if ext in {".csv", ".tsv"}:
        return _inspect_tabular(path, ext, clamped_rows)

    # 结构化 JSON 文件
    if ext == ".json":
        return _inspect_json(path, clamped_rows)

    # 纯文本、Markdown、日志或其他文档
    return _inspect_text(path, ext, start_line_idx, clamped_lines)
