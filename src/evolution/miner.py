"""从近期线上 trace 中挖掘失败，并归并为问题聚类。

满足以下任一条件即视为失败：运行出错或超时、工具失败、Agent 需要反思重试、
用户点踩。单次投诉不会触发任何改动：规模低于 ``EVOLUTION_MIN_CLUSTER_SIZE``
的聚类只作为积压信号保留。用户反馈的权重随时间衰减，陈旧投诉会逐渐淡出而不是堆积。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

from src.config import settings
from src.midplatform.ops_store import OpsStore, get_ops_store, now

FEEDBACK_HALF_LIFE_DAYS = 7.0


@dataclass
class FailureCluster:
    key: str
    reason: str
    tool: str | None
    trace_ids: list[str] = field(default_factory=list)
    weight: float = 0.0

    @property
    def size(self) -> int:
        return len(self.trace_ids)


def _reason(
    trace: dict, scores: dict[str, float], first_error_tool: str | None
) -> tuple[str, str | None] | None:
    if trace["status"] == "timeout":
        return "timeout", None
    if trace["status"] != "ok":
        return "run_error", None
    if scores.get("user_rating", 1.0) < 0.5:
        return "user_downvote", first_error_tool
    if first_error_tool:
        return "tool_failure", first_error_tool
    if scores.get("reflections", 0) > 0:
        return "needed_reflection", None
    if scores.get("evidence_calls", 1) == 0:
        return "no_evidence", None
    return None


def mine_failures(
    store: OpsStore | None = None, *, window_days: float | None = None, at: float | None = None
) -> list[FailureCluster]:
    """返回近期窗口内的失败聚类，按权重从大到小排序。"""
    store = store or get_ops_store()
    at = at if at is not None else now()
    since = at - (window_days or settings.EVOLUTION_WINDOW_DAYS) * 86_400
    traces = store.query("SELECT * FROM traces WHERE created_at >= ? ORDER BY created_at", (since,))
    clusters: dict[str, FailureCluster] = {}
    for trace in traces:
        if json.loads(trace["artifact_versions"]).get("content_scope") == "private":
            continue
        scores = {
            row["name"]: row["value"]
            for row in store.query(
                "SELECT name, value FROM scores WHERE trace_id = ?", (trace["id"],)
            )
        }
        failing = store.query_one(
            "SELECT name FROM observations WHERE trace_id = ? AND error IS NOT NULL ORDER BY position LIMIT 1",
            (trace["id"],),
        )
        reason = _reason(trace, scores, failing["name"] if failing else None)
        if reason is None:
            continue
        label, tool = reason
        key = f"{label}:{tool or '-'}"
        cluster = clusters.setdefault(key, FailureCluster(key, label, tool))
        cluster.trace_ids.append(trace["id"])
        age_days = max(at - trace["created_at"], 0) / 86_400
        decay = (
            math.pow(0.5, age_days / FEEDBACK_HALF_LIFE_DAYS) if label == "user_downvote" else 1.0
        )
        cluster.weight += decay
    return sorted(clusters.values(), key=lambda c: c.weight, reverse=True)


def actionable(clusters: list[FailureCluster]) -> list[FailureCluster]:
    """只保留规模不小于 EVOLUTION_MIN_CLUSTER_SIZE 的聚类，单次投诉不会触发改动。"""
    return [c for c in clusters if c.size >= settings.EVOLUTION_MIN_CLUSTER_SIZE]
