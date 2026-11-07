"""为共享改进循环提供显式、非隐私的评估失败记录。

用户对话始终保持私密。只有管理员编写、且没有来源对话的合成样本，
才能产生共享的失败证据。
"""

from __future__ import annotations

from src.evaluation.datasets import DatasetItem
from src.evaluation.evaluators import RunOutcome
from src.midplatform.ops_store import OpsStore, dumps, new_id, now
from src.observability.tracer import Tracer, TraceRecord

# 旧版可能把 holdout 和 regression 写成同样的 synthetic trace。
# 读取时回查样本来源，让已有记录也遵守 train 边界。
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
    """为失败的合成样本记录一条共享失败 trace，返回其 ID。

    只接受无来源对话、且在合成夹具中运行的 train 样本；质量达标时返回 None。
    同一样本已有记录则复用，避免重复运行制造虚假聚类。
    """
    if (
        item.split != "train"
        or item.provenance != "synthetic"
        or item.origin_trace_id
        or not outcome.synthetic_fixture
        or quality >= 0.999
    ):
        return None
    # 同一条目刷新失败证据和时间窗口。并发评测也只更新同一条 trace。
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
