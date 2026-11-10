"""Record explicit nonprivate evaluation failures for shared improvement.

User conversations remain private. Only administrator-written synthetic samples without
source conversations may contribute shared failure evidence.
"""

from __future__ import annotations

from src.evaluation.datasets import DatasetItem
from src.evaluation.evaluators import RunOutcome
from src.midplatform.ops_store import OpsStore, dumps, new_id, now
from src.observability.tracer import Tracer, TraceRecord

# Older versions wrote holdout and regression failures as synthetic traces too.
# Recheck sample provenance so existing traces also respect the train-only boundary.
TRAIN_FAILURE_TRACES = """
    FROM traces AS t
    JOIN dataset_items AS item ON item.id = json_extract(t.artifact_versions, '$.synthetic_item')
    WHERE json_extract(t.artifact_versions, '$.content_scope') = 'synthetic'
      AND item.split = 'train' AND item.origin_trace_id IS NULL
      AND json_extract(item.expected, '$._provenance') = 'synthetic'
"""


def record_synthetic_failure(
    item: DatasetItem, outcome: RunOutcome, quality: float, store: OpsStore
) -> str | None:
    """Record or reuse a shared failure trace for a synthetic train entry.

    Require a fixed synthetic fixture and no conversation provenance; return None for passing
    samples. Reuse existing traces so repeated runs cannot fabricate cluster size.
    """
    if (
        item.split != "train"
        or item.provenance != "synthetic"
        or item.origin_trace_id
        or not outcome.synthetic_fixture
        or quality >= 0.999
    ):
        return None
    # Refresh one entry's evidence and time window; concurrent evaluations share its trace.
    with store.transaction(immediate=True) as conn:
        existing = store.query_one(
            "SELECT id FROM traces WHERE json_extract(artifact_versions,'$.synthetic_item')=? "
            "ORDER BY created_at DESC, id LIMIT 1",
            (item.id,),
        )
        trace = TraceRecord(
            existing["id"] if existing else new_id(),
            "synthetic-evaluation",
            item.tenant_id,
            None,
            item.input,
            artifact_versions={"content_scope": "synthetic", "synthetic_item": item.id},
        )
        if not Tracer(store).record(
            trace,
            output_text=None,
            status="error",
            error="synthetic_evaluation_failed",
            rule_scores={"evaluation_quality": quality},
        ):
            return None
        missing = set(item.expected.get("required_tools", [])) - set(outcome.tools)
        conn.executemany(
            "INSERT INTO observations VALUES (?,?,?,?,?,?,?,?)",
            (
                (
                    new_id(),
                    trace.trace_id,
                    "tool",
                    tool,
                    None,
                    None,
                    "required_tool_missing",
                    position,
                )
                for position, tool in enumerate(sorted(missing))
            ),
        )
        conn.execute(
            "INSERT INTO scores VALUES (?,?,?,?,?,?,?,?)",
            (
                new_id(),
                trace.trace_id,
                "synthetic_origin",
                1.0,
                "numeric",
                "rule",
                dumps({"item_id": item.id}),
                now(),
            ),
        )
        return trace.trace_id
