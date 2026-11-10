"""Propose artifacts from synthetic training failures only.

Prompt proposals reflect on redacted failures without editing the safety prefix. Parameter
proposals search bounded neighbors and select winners using train scores only.
"""

from __future__ import annotations

import difflib
import json
import re
from typing import Any, Awaitable, Callable

from src.evaluation.synthetic import TRAIN_FAILURE_TRACES
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
    """Return redacted representative traces for reports and reflection prompts."""
    store = store or get_ops_store()
    evidence = []
    for trace_id in cluster.trace_ids[:limit]:
        trace = store.query_one(
            "SELECT t.id, t.input_text, t.output_text, t.status "
            + TRAIN_FAILURE_TRACES
            + " AND t.id = ?",
            (trace_id,),
        )
        if not trace:
            continue
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
    """Ask the model to revise guidance from redacted cluster evidence.

    Return revised guidance and rationale, or None on failure, empty or unchanged output,
    or excessive growth.
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
    except Exception:  # noqa: BLE001 - Generator failures produce no candidate.
        return None
    guidance = str(data.get("guidance", "")).strip()
    if not guidance or guidance == current.strip() or len(guidance) > 3 * len(current) + 2_000:
        return None
    return guidance, str(data.get("rationale", ""))[:600]


def param_neighbours(
    current: dict[str, Any], focus: list[str] | None = None
) -> list[dict[str, float]]:
    """Generate one bounded step in each direction for every tunable parameter."""
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
    """Return parameters plausibly relevant to this failure type."""
    if cluster.tool == "search_knowledge_documents":
        return ["retrieval_min_relevance", "knowledge_top_k", "max_tool_retries"]
    if cluster.tool in {"query_database", "get_business_schema"}:
        return ["prune_max_tables", "max_tool_retries"]
    return list(PARAM_BOUNDS)


def prompt_diff(old: str, new: str) -> str:
    """Return a unified diff between production and candidate guidance."""
    return "\n".join(
        difflib.unified_diff(
            old.splitlines(), new.splitlines(), "production", "candidate", lineterm=""
        )
    )
