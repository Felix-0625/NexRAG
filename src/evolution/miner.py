"""Group recent synthetic training failures by reason and tool."""

from __future__ import annotations

from dataclasses import dataclass, field

from src.config import settings
from src.evaluation.synthetic import TRAIN_FAILURE_TRACES
from src.midplatform.ops_store import OpsStore, get_ops_store, now


@dataclass
class FailureCluster:
    """Related synthetic training failures eligible for proposal generation."""

    key: str
    reason: str
    tool: str | None
    trace_ids: list[str] = field(default_factory=list)
    weight: float = 0.0

    @property
    def size(self) -> int:
        """Return the number of failures represented by this cluster."""
        return len(self.trace_ids)


def mine_failures(
    store: OpsStore | None = None, *, window_days: float | None = None, at: float | None = None
) -> list[FailureCluster]:
    """Return recent failure clusters ordered by descending weight."""
    store = store or get_ops_store()
    at = at if at is not None else now()
    since = at - (window_days or settings.EVOLUTION_WINDOW_DAYS) * 86_400
    traces = store.query(
        "SELECT t.id, item.id AS item_id, "
        "(SELECT o.name FROM observations AS o WHERE o.trace_id = t.id "
        "AND o.error IS NOT NULL ORDER BY o.position LIMIT 1) AS failing_tool "
        + TRAIN_FAILURE_TRACES
        + " AND t.created_at >= ? ORDER BY t.created_at DESC, t.id",
        (since,),
    )
    clusters: dict[str, FailureCluster] = {}
    seen_items = set()
    for trace in traces:
        if trace["item_id"] in seen_items:
            continue
        seen_items.add(trace["item_id"])
        tool = trace["failing_tool"]
        label = "tool_failure" if tool else "evaluation_failure"
        key = f"{label}:{tool or '-'}"
        cluster = clusters.setdefault(key, FailureCluster(key, label, tool))
        cluster.trace_ids.append(trace["id"])
        cluster.weight += 1
    return sorted(clusters.values(), key=lambda c: c.weight, reverse=True)


def actionable(clusters: list[FailureCluster]) -> list[FailureCluster]:
    """Require the minimum cluster size so one complaint cannot trigger a change."""
    return [c for c in clusters if c.size >= settings.EVOLUTION_MIN_CLUSTER_SIZE]
