"""只对比提案中确切的候选版本与其前任版本。

prompt 与参数实验重叠时，仅凭 `canary` 标志不够，两者的样本人群可能不同。
因此要匹配版本元数据，并要求对照组和实验组都有样本，才算观察完成。
"""

from __future__ import annotations

import statistics

from src.midplatform.ops_store import now

MIN_SAMPLES_PER_GROUP = 20


def metrics_for_proposal(store, proposal: dict) -> dict[str, float]:
    """统计灰度启动以来候选版本与基线版本各自的线上指标（成功率、用户评分、工具错误数均值及 trace 数），键形如 canary_*、control_*。"""
    since = proposal["canary_started_at"] or now()
    path = "$." + proposal["kind"]
    result = {}
    for group, version in (
        ("canary", proposal["candidate_id"]),
        ("control", proposal["baseline_id"]),
    ):
        predicate = "t.created_at >= ? AND json_extract(t.artifact_versions, ?) = ?"
        args = (since, path, version)
        rows = store.query(
            "SELECT s.name,s.value FROM scores s JOIN traces t ON t.id=s.trace_id WHERE "
            + predicate
            + " AND s.name IN ('run_success','user_rating','tool_error_count')",
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
    """灰度组和对照组的 trace 数是否都达到每组最少样本数。"""
    return all(
        metrics.get(f"{group}_traces", 0) >= MIN_SAMPLES_PER_GROUP
        for group in ("canary", "control")
    )


def regressed_metrics(metrics, max_drop: float) -> list[str]:
    """返回灰度组相对对照组下降超过 max_drop 的指标名（仅比较 run_success 与 user_rating）。"""
    return [
        name
        for name in ("run_success", "user_rating")
        if f"canary_{name}" in metrics
        and f"control_{name}" in metrics
        and metrics[f"control_{name}"] - metrics[f"canary_{name}"] > max_drop
    ]
