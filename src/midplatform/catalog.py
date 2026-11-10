"""Load operator-maintained business schemas and metric descriptions."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from src.config import settings

# Ignore common words that cannot distinguish views or generic queries.
COMMON_STOPWORDS = frozenset(
    {
        "当前",
        "卖家",
        "查询",
        "查看",
        "统计",
        "以及",
        "和",
        "的",
        "了",
        "在",
        "是",
        "有",
        "seller",
        "the",
        "and",
        "or",
        "in",
        "of",
        "to",
        "for",
        "with",
        "a",
        "an",
    }
)


def _extract_query_tokens(text: str) -> set[str]:
    """Extract Latin keywords and CJK unigram, bigram, and trigram tokens."""
    text_lower = text.lower()
    tokens = set(re.findall(r"[a-z0-9_]{2,}", text_lower))
    cjk_chars = re.findall(r"[\u4e00-\u9fff]", text_lower)
    for char in cjk_chars:
        if char not in COMMON_STOPWORDS:
            tokens.add(char)
    for i in range(len(cjk_chars) - 1):
        bigram = cjk_chars[i] + cjk_chars[i + 1]
        if bigram not in COMMON_STOPWORDS:
            tokens.add(bigram)
    for i in range(len(cjk_chars) - 2):
        trigram = cjk_chars[i] + cjk_chars[i + 1] + cjk_chars[i + 2]
        tokens.add(trigram)
    return tokens


class MetadataCatalog:
    """Metadata catalog exposed to the business-schema tool."""

    def __init__(self, config_path: str | None = None):
        self.config_path = Path(config_path or settings.METADATA_PATH)
        self.metadata: dict[str, Any] = {}
        self.load_metadata()

    def load_metadata(self) -> None:
        """Fail on missing deployment metadata instead of hiding misconfiguration."""
        if not self.config_path.is_file():
            raise FileNotFoundError(self.config_path)
        with open(self.config_path, "r", encoding="utf-8") as file:
            self.metadata = yaml.safe_load(file) or {}

    def prune_schema(self, query: str, max_tables: int = 2) -> list[str]:
        """Select relevant tables using question keywords."""
        tables = self.metadata.get("tables", {})
        if not tables:
            return []
        if len(tables) <= max_tables or not query or not query.strip():
            return list(tables.keys())

        tokens = _extract_query_tokens(query)
        scores: dict[str, float] = {table_name: 0.0 for table_name in tables}
        metrics = self.metadata.get("metrics", {})

        for table_name, info in tables.items():
            name_lower = table_name.lower()
            desc_lower = str(info.get("description", "")).lower()
            columns = info.get("columns", {})

            for token in tokens:
                weight = 1.0 if len(token) == 1 else (3.0 if len(token) == 2 else 5.0)

                if token in name_lower:
                    scores[table_name] += weight * 2.0

                if token in desc_lower:
                    scores[table_name] += weight * 1.5

                for col_name, col_info in columns.items():
                    col_name_lower = col_name.lower()
                    col_desc_lower = str(col_info.get("description", "")).lower()
                    if token in col_name_lower:
                        scores[table_name] += weight * 2.0
                    if token in col_desc_lower:
                        scores[table_name] += weight * 1.2

        # Metric matches boost their owning table so pruning retains required schema.
        for metric_name, metric_info in metrics.items():
            target_table = metric_info.get("table")
            if target_table in scores:
                m_desc = str(metric_info.get("description", "")).lower()
                m_name = metric_name.lower()
                for token in tokens:
                    weight = 1.0 if len(token) == 1 else (3.0 if len(token) == 2 else 5.0)
                    if token in m_name:
                        scores[target_table] += weight * 2.0
                    if token in m_desc:
                        scores[target_table] += weight * 1.5

        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        selected = [t for t, s in ranked if s > 0][:max_tables]
        return selected if selected else list(tables.keys())[:max_tables]

    def get_table_schema_summary(self, allowed_tables: list[str] | None = None) -> str:
        """Describe configured public views, optionally restricted to selected tables."""
        tables = self.metadata.get("tables", {})
        lines = []
        for table_name, info in tables.items():
            if allowed_tables and table_name not in allowed_tables:
                continue
            lines.append(f"### Table: {table_name} ({info.get('description', '')})")
            lines.append("| Column | Type | Description |")
            lines.append("| :--- | :--- | :--- |")
            for col, col_info in info.get("columns", {}).items():
                lines.append(
                    f"| {col} | {col_info.get('type', 'TEXT')} | {col_info.get('description', '')} |"
                )
            lines.append("")
        return "\n".join(lines)
