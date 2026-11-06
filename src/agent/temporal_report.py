"""渲染经过认证的期间计算，单位不由模型选择。

这里只覆盖显式的纯文档期间比较契约；Agent 仍须先检索并执行经过校验的算术工具。
最终正文只选取真实的证据引用，数值、角色、单位和公式都来自服务器持有的当前计算凭证。
"""

from __future__ import annotations

import html

from src.agent.source_facts import period_request


def temporal_results(ledger) -> list[dict]:
    """仅当任务只需知识库加百分比变化且证据齐全时，返回与请求期间绑定一致的计算条目；否则返回空列表。"""
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
    """用账本中已认证的期间计算渲染 Markdown 报告；引用不存在、缺少来源或单位不一致时抛出 ValueError。"""
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
