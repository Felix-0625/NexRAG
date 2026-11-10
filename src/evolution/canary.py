"""Compare only a proposal's exact candidate and predecessor versions.

Overlapping prompt and parameter experiments may serve different cohorts despite canary
flags. Match complete version metadata and require observations in both groups.
"""

from __future__ import annotations

import statistics

from src.midplatform.ops_store import now

MIN_SAMPLES_PER_GROUP = 20


def metrics_for_proposal(store, proposal: dict) -> dict[str, float]:
    """Compute candidate and control success, ratings, tool errors, and trace counts since canary
    start.
    """
    since = proposal["canary_started_at"] or now()
    path = "$." + proposal["kind"]
    result = {}
    for group, version in (
        ("canary", proposal["candidate_id"]),
        ("control", proposal["baseline_id"]),
    ):
        predicate = "t.created_at >= ? AND json_extract(t.artifact_versions, ?) = ?"
        args = (since, path, version)
        # Older versions allowed duplicate ratings; count only the latest vote per trace.
        rows = store.query(
            "SELECT name, value FROM ("
            "SELECT s.name, s.value, ROW_NUMBER() OVER ("
            "PARTITION BY s.trace_id, s.name ORDER BY s.created_at DESC, s.id DESC) AS latest "
            "FROM scores s JOIN traces t ON t.id=s.trace_id WHERE "
            + predicate
            + " AND s.name IN ('run_success','user_rating','tool_error_count')) WHERE latest = 1",
            args,
        )
        grouped = {}
        for row in rows:
            grouped.setdefault(row["name"], []).append(row["value"])
        for name, values in grouped.items():
            result[f"{group}_{name}"] = round(statistics.fmean(values), 4)
        count = store.query_one("SELECT COUNT(*) AS n FROM traces t WHERE " + predicate, args)
        result[f"{group}_traces"] = float(count["n"])
    return result


def enough_samples(metrics) -> bool:
    """Require the minimum trace count in both canary and control groups."""
    return all(
        metrics.get(f"{group}_traces", 0) >= MIN_SAMPLES_PER_GROUP
        for group in ("canary", "control")
    )


def regressed_metrics(metrics, max_drop: float) -> list[str]:
    """Return success or rating metrics whose canary drop exceeds max_drop."""
    return [
        name
        for name in ("run_success", "user_rating")
        if f"canary_{name}" in metrics
        and f"control_{name}" in metrics
        and metrics[f"control_{name}"] - metrics[f"canary_{name}"] > max_drop
    ]
