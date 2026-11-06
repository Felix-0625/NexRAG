"""为失败聚类生成候选产物。

- prompt：由 LLM 针对典型失败和当前指引进行反思，返回修订后的指引
  （OPRO/Reflexion 风格）。只展示 train 条目和线上 trace，holdout 条目绝不会
  到达生成器。不可变的安全前缀不属于可编辑文本。
- params：围绕当前取值做有界邻域搜索，只在 train 划分上打分，
  最佳邻居成为候选。
"""

from __future__ import annotations

import difflib
import json
import re
from typing import Any, Awaitable, Callable

from src.evolution.artifacts import PARAM_BOUNDS, clamp_params
from src.evolution.miner import FailureCluster
from src.midplatform.ops_store import OpsStore, get_ops_store

Completer = Callable[[str, str], Awaitable[str]]

REFLECT_PROMPT = """You improve the operating guidance of a business-analysis agent.
You receive the current guidance and failing interactions of one problem cluster.
Rewrite the guidance so these failures are less likely, keeping every rule that still applies.
Constraints:
- Do not weaken or remove rules about evidence, citations, tenant restrictions, or read-only access.
- Do not add instructions to call tools that are not in the guidance.
- Keep it concise; change only what the failures justify.
Return JSON: {"guidance": "<full revised guidance>", "rationale": "<why, max 3 sentences>"}"""

PARAM_STEPS: dict[str, float] = {
    "retrieval_min_relevance": 0.05,
    "max_tool_retries": 1,
    "prune_max_tables": 1,
    "knowledge_top_k": 1,
}


def cluster_evidence(
    cluster: FailureCluster, store: OpsStore | None = None, limit: int = 5
) -> list[dict[str, Any]]:
    """供报告和反思提示词使用的、经脱敏的典型 trace。"""
    store = store or get_ops_store()
    evidence = []
    for trace_id in cluster.trace_ids[:limit]:
        trace = store.query_one(
            "SELECT id, input_text, output_text, status, artifact_versions FROM traces WHERE id = ?",
            (trace_id,),
        )
        if not trace:
            continue
        if json.loads(trace["artifact_versions"]).get("content_scope") == "private":
            continue
        trace.pop("artifact_versions")
        observations = store.query(
            "SELECT name, error FROM observations WHERE trace_id = ? ORDER BY position", (trace_id,)
        )
        evidence.append(
            {
                **trace,
                "tools": [o["name"] for o in observations],
                "errors": [o["error"] for o in observations if o["error"]],
            }
        )
    return evidence


async def propose_prompt(
    current: str, cluster: FailureCluster, completer: Completer, store: OpsStore | None = None
) -> tuple[str, str] | None:
    """让 LLM 依据失败聚类的脱敏证据重写当前指引，返回 (新指引, 理由)。

    生成失败、内容为空、与原文相同或长度膨胀过大时返回 None，不产生候选。
    """
    evidence = cluster_evidence(cluster, store)
    payload = json.dumps(
        {
            "cluster": {"reason": cluster.reason, "tool": cluster.tool, "size": cluster.size},
            "failures": evidence,
            "current_guidance": current,
        },
        ensure_ascii=False,
    )[:12_000]
    try:
        raw = await completer(REFLECT_PROMPT, payload)
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        data = json.loads(match.group(0)) if match else {}
    except Exception:  # noqa: BLE001 - 生成器失败时不产生候选
        return None
    guidance = str(data.get("guidance", "")).strip()
    if not guidance or guidance == current.strip() or len(guidance) > 3 * len(current) + 2_000:
        return None
    return guidance, str(data.get("rationale", ""))[:600]


def param_neighbours(
    current: dict[str, Any], focus: list[str] | None = None
) -> list[dict[str, float]]:
    """对每个可调参数向上、向下各走一步，并限制在允许范围内。"""
    current = clamp_params(current)
    keys = focus or list(PARAM_BOUNDS)
    neighbours = []
    for key in keys:
        for direction in (-1, 1):
            moved = dict(current)
            moved[key] = (
                float(current.get(key, PARAM_BOUNDS[key][0])) + direction * PARAM_STEPS[key]
            )
            clamped = clamp_params(moved)
            if clamped != clamp_params(current):
                neighbours.append(clamped)
    return neighbours


def focus_params(cluster: FailureCluster) -> list[str]:
    """与某类失败可能相关的参数。"""
    if cluster.tool == "search_knowledge_documents" or cluster.reason == "needed_reflection":
        return ["retrieval_min_relevance", "knowledge_top_k", "max_tool_retries"]
    if cluster.tool in {"query_database", "get_business_schema"}:
        return ["prune_max_tables", "max_tool_retries"]
    return list(PARAM_BOUNDS)


def prompt_diff(old: str, new: str) -> str:
    """生成线上版本与候选版本指引的统一 diff 文本。"""
    return "\n".join(
        difflib.unified_diff(
            old.splitlines(), new.splitlines(), "production", "candidate", lineterm=""
        )
    )
