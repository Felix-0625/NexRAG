"""Evaluation datasets partitioned into train, holdout, and regression splits.

Proposal generation may read train only; holdout is gate-only, and regression retains
historical defects. Versioned evals/*.jsonl seeds load into operations storage, with
administrator-curated synthetic additions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.midplatform.ops_store import OpsStore, dumps, get_ops_store, new_id, now

SPLITS = ("train", "holdout", "regression")
DEFAULT_DATASET = "nexrag"


@dataclass(frozen=True)
class DatasetItem:
    """Evaluation input, expectations, split, and synthetic provenance."""

    id: str
    split: str
    category: str
    tenant_id: str
    input: str
    expected: dict[str, Any]
    provenance: str = "manual"
    origin_trace_id: str | None = None


def load_seed(
    path: str | Path, store: OpsStore | None = None, dataset: str = DEFAULT_DATASET
) -> int:
    """Insert seed entries not already present by split and input."""
    store = store or get_ops_store()
    added = 0
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        if raw["split"] not in SPLITS:
            raise ValueError(f"Unknown split {raw['split']!r}")
        exists = store.query_one(
            "SELECT id FROM dataset_items WHERE dataset = ? AND split = ? AND input = ?",
            (dataset, raw["split"], raw["input"]),
        )
        if exists:
            continue
        expected = {
            **raw["expected"],
            "tenant_id": raw["tenant_id"],
            "_provenance": raw.get("provenance", "manual"),
        }
        store.execute(
            "INSERT INTO dataset_items VALUES (?,?,?,?,?,?,?,?)",
            (
                new_id(),
                dataset,
                raw["split"],
                raw["category"],
                raw["input"],
                dumps(expected),
                None,
                now(),
            ),
        )
        added += 1
    return added


def items(
    splits: tuple[str, ...], store: OpsStore | None = None, dataset: str = DEFAULT_DATASET
) -> list[DatasetItem]:
    """Read entries in creation order, separating tenant and provenance metadata from expectations."""
    store = store or get_ops_store()
    marks = ",".join("?" for _ in splits)
    rows = store.query(
        f"SELECT * FROM dataset_items WHERE dataset = ? AND split IN ({marks}) ORDER BY created_at, id",
        (dataset, *splits),
    )
    result = []
    for row in rows:
        expected = json.loads(row["expected"])
        result.append(
            DatasetItem(
                row["id"],
                row["split"],
                row["category"],
                expected.pop("tenant_id", ""),
                row["input"],
                {k: v for k, v in expected.items() if k != "_provenance"},
                expected.get("_provenance", "manual"),
                row["origin_trace_id"],
            )
        )
    return result


def add_synthetic_item(
    input_text: str,
    tenant_id: str,
    split: str,
    expected: dict[str, Any],
    *,
    category: str = "synthetic",
    store: OpsStore | None = None,
) -> str:
    """Curate a nonprivate synthetic sample without copying conversations.

    Administrators remain responsible for fictional data; PII detection is an additional
    defense, not proof that arbitrary input contains no secrets.
    """
    from src.evaluation.fixtures import TENANT_IDS
    from src.observability.redaction import contains_pii

    if split not in SPLITS or not input_text.strip() or len(input_text) > 8_000:
        raise ValueError("Invalid synthetic evaluation sample")
    if contains_pii(json.dumps([input_text, expected], ensure_ascii=False)):
        raise ValueError("Synthetic samples must not contain personal identifiers or secrets")
    if tenant_id not in TENANT_IDS or any(
        key in expected for key in ("knowledge_scopes", "business_dsn", "file_path", "source_sql")
    ):
        raise ValueError(
            "Synthetic evaluation must use a fixed synthetic tenant and cannot reference live resources"
        )
    from src.evaluation.validation import validate_expected

    expected = validate_expected(expected)
    store = store or get_ops_store()
    item_id = new_id()
    payload = {**expected, "tenant_id": tenant_id, "_provenance": "synthetic"}
    store.execute(
        "INSERT INTO dataset_items VALUES (?,?,?,?,?,?,?,?)",
        (item_id, DEFAULT_DATASET, split, category, input_text, dumps(payload), None, now()),
    )
    return item_id
