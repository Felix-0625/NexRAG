"""为共享改进循环提供显式、非隐私的评估失败记录。

用户对话始终保持私密。只有管理员编写、且没有来源对话的合成样本，
才能产生共享的失败证据。
"""

from __future__ import annotations

from src.evaluation.datasets import DatasetItem
from src.evaluation.evaluators import RunOutcome
from src.midplatform.ops_store import OpsStore, dumps, new_id, now
from src.observability.tracer import Tracer, TraceRecord


def record_synthetic_failure(
    item: DatasetItem, outcome: RunOutcome, quality: float, store: OpsStore
) -> str | None:
    """为失败的合成样本记录一条共享失败 trace，返回其 ID。

    只接受无来源对话、且在合成 fixture 中运行的合成样本；质量达标时返回 None。
    同一样本已有记录则复用，避免重复运行制造虚假聚类。
    """
    if (
        item.provenance != "synthetic"
        or item.origin_trace_id
        or not outcome.synthetic_fixture
        or quality >= 0.999
    ):
        return None
    # 反复运行同一个失败条目不会制造出虚假的聚类。
    existing = store.query_one(
        "SELECT id FROM traces WHERE json_extract(artifact_versions,'$.synthetic_item')=?",
        (item.id,),
    )
    if existing:
        return existing["id"]
    trace = TraceRecord(
        new_id(),
        "synthetic-evaluation",
        item.tenant_id,
        None,
        item.input,
        artifact_versions={"content_scope": "synthetic", "synthetic_item": item.id},
    )
    recorder = Tracer(store)
    recorder.record(
        trace,
        output_text=None,
        status="error",
        error="synthetic_evaluation_failed",
        rule_scores={"evaluation_quality": quality},
    )
    if not store.query_one("SELECT id FROM traces WHERE id=?", (trace.trace_id,)):
        return None
    missing = set(item.expected.get("required_tools", [])) - set(outcome.tools)
    for position, tool_name in enumerate(sorted(missing)):
        store.execute(
            "INSERT INTO observations VALUES (?,?,?,?,?,?,?,?)",
            (
                new_id(),
                trace.trace_id,
                "tool",
                tool_name,
                None,
                None,
                "required_tool_missing",
                position,
            ),
        )
    store.execute(
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
