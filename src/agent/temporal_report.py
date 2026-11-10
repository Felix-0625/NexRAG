"""Render certified period calculations with server-controlled units.

Handle explicit document-only comparisons after retrieval and validated arithmetic. Render
only actual evidence references; values, roles, units, and formulas come from run-local proofs.
"""

from __future__ import annotations

import html

from src.agent.source_facts import period_request


def temporal_results(ledger) -> list[dict]:
    """Return request-bound period results only for complete knowledge-plus-percentage-change
    tasks.
    """
    if ledger.required != {"kb", "arithmetic:percent_change"} or ledger.missing():
        return []
    try:
        request = period_request(ledger.prompt)
    except ValueError:
        return []
    if request is None:
        return []
    return [
        entry
        for entry in ledger.entries
        if entry["kind"] == "tool"
        and isinstance(entry["data"], dict)
        and entry["data"].get("tool") == "calculate_evidence_arithmetic"
        and entry["data"].get("operation") == "percent_change"
        and entry["data"].get("temporal_binding", {}).get("request") == request.model_dump()
    ]


def render_temporal_report(report, ledger) -> str:
    """Render certified period results; reject missing references, sources, or inconsistent units."""
    available = {entry["id"]: entry for entry in temporal_results(ledger)}
    known_knowledge = {entry["id"] for entry in ledger.entries if entry["kind"] == "kb"}
    if report.metric_ids or report.detail_evidence_ids:
        raise ValueError("期间文档计算不能引用不存在的业务指标或明细")
    if not set(report.knowledge_evidence_ids) <= known_knowledge:
        raise ValueError("期间计算报告引用了不存在的知识证据")
    if not report.other_evidence_ids or not set(report.other_evidence_ids) <= available.keys():
        raise ValueError("期间计算报告须引用本轮实际完成且期间绑定一致的计算证据")
    entry = available[report.other_evidence_ids[-1]]
    result = entry["data"]
    binding = result["temporal_binding"]
    new, old = binding["facts"]
    if {new["evidence_id"], old["evidence_id"]} - set(report.knowledge_evidence_ids):
        raise ValueError("期间计算报告缺少计算参数的知识来源")
    if new["unit"] != old["unit"] or not new["unit"]:
        raise ValueError("期间计算缺少一致且明确的来源单位")

    def literal(value):
        """Escape a value for safe literal Markdown rendering."""
        return html.escape(str(value)).replace("|", "\\|").replace("\n", " ")

    return (
        f"**{literal(binding['request']['metric'])}**\n\n"
        "| 期间 | 来源数值 | 来源单位 | 依据 |\n| --- | --- | --- | --- |\n"
        f"| {literal(old['period'])} | {literal(old['value'])} | {literal(old['unit'])} | [{old['evidence_id']}] |\n"
        f"| {literal(new['period'])} | {literal(new['value'])} | {literal(new['unit'])} | [{new['evidence_id']}] |\n\n"
        f"变化率：{result['rounded_result']}%。\n\n"
        f"计算式：`{result['formula']}`。\n\n计算依据：[{entry['id']}]。\n\n"
        f"FINAL_ANSWER: {result['rounded_result']}%"
    )
