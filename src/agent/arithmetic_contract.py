"""将显式的文档算术请求与检索、计算和最终输出绑定。

只识别受支持的显式数值意图；这并不能证明模型选对了来源指标、日期或货币单位。
"""

from __future__ import annotations

import re
from decimal import Decimal


def requested_arithmetic(text: str) -> set[str]:
    """匹配运算请求，不硬编码实体或基准数据。"""
    if re.search(
        r"口径|定义|含义|公式是什么|计算方法|definition|how to calculate|what is the formula",
        text,
        re.I,
    ):
        return set()
    from src.agent.indexed_returns import requested_comparison

    try:
        indexed = requested_comparison(text) is not None
    except ValueError:
        # 显式但未解析的比较仍需要绑定来源的契约，不能退化为无类型的货币相减。
        indexed = True
    if indexed:
        return {"arithmetic:indexed_return_difference"}
    operations = set()
    for operation, pattern in (
        ("percent_change", r"(?:percent(?:age)?\s+change|growth\s+rate|同比|增长率|变动率|变化率)"),
        ("percentage", r"(?:what\s+(?:percent(?:age)?|portion)|占比|占.{0,20}百分之几)"),
        (
            "subtract",
            r"(?:net\s+change|what\s+was\s+the\s+change|difference\s+in|计算差(?:值|额)|求差(?:值|额))",
        ),
    ):
        if re.search(pattern, text, re.I):
            operations.add("arithmetic:" + operation)
    return operations


def completed_arithmetic(entries, prompt: str = "") -> set[str]:
    """返回账本中已完成的算术义务；百分比变化须与请求的期间绑定一致才算完成。"""
    from src.agent.source_facts import period_request

    try:
        temporal_request = period_request(prompt)
    except ValueError:
        temporal_request = True  # 显式但未解析的期间不属于纯算术任务。
    completed = {
        "arithmetic:" + entry["data"]["operation"]
        for entry in entries
        if entry["kind"] == "tool"
        and isinstance(entry["data"], dict)
        and entry["data"].get("tool") == "calculate_evidence_arithmetic"
        and (
            entry["data"].get("operation") != "percent_change"
            or temporal_request is None
            or (
                temporal_request is not True
                and entry["data"].get("temporal_binding", {}).get("request")
                == temporal_request.model_dump()
            )
        )
    }
    from src.agent.indexed_returns import verified_comparisons

    if verified_comparisons(entries, prompt):
        completed.add("arithmetic:indexed_return_difference")
    return completed


def arithmetic_answer_issues(answer: str, required, entries) -> list[str]:
    """执行了计算却没有给出结果，不算完成。"""
    from src.agent.arithmetic_claims import _percentages
    from src.agent.grounding import _numbers

    problems = []
    for obligation in sorted(item for item in required if item.startswith("arithmetic:")):
        operation = obligation.split(":", 1)[1]
        calculations = [
            entry["data"]
            for entry in entries
            if entry["kind"] == "tool"
            and isinstance(entry["data"], dict)
            and entry["data"].get("tool") == "calculate_evidence_arithmetic"
            and entry["data"].get("operation") == operation
        ]
        if not calculations:
            continue  # 缺失的运算由 ledger 另行报告。
        actual = set()
        for calculation in calculations:
            actual.add(Decimal(calculation["result"]))
            actual.add(Decimal(calculation["rounded_result"]))
        presented = (
            _percentages(answer)
            if operation in {"percentage", "percent_change"}
            else _numbers(answer)
        )
        if not actual & presented:
            problems.append(
                f"Missing requested arithmetic result: {operation}. State the actual rounded_result "
                "with its source unit and formula; a source excerpt or an unknown/zero placeholder is not the answer."
            )
    return problems


def _export_instruction() -> str:
    """显式授权报告导出时附加的 export_analysis 使用说明（固定文本）。"""
    return (
        " The user explicitly authorized a report export. export_analysis is an allowed "
        "server-managed report write, not arbitrary filesystem access. After retrieving "
        "the requested document facts, call export_analysis(format='md' or 'txt', "
        "text=the evidence-grounded report including actual KB citations, rows=None). "
        "For a Markdown table extracted from prose, put the table in text, not rows: "
        "rows requires exact structured query/tool records. If export rejects rows, "
        "reuse the successful KB evidence and retry with text; do not query SQL, "
        "recall memory or repeat retrieval. A completed export must return a real download URL. "
    )


def _indexed_return_instruction(ledger) -> str:
    """指数收益差异：来源事实已就绪时返回预备参数说明，否则返回空串。"""
    import json

    from src.agent.indexed_returns import prepare_comparison

    try:
        inputs = prepare_comparison(ledger.prompt, ledger.entries)
    except ValueError:
        inputs = None
    if inputs is None:
        return ""
    return (
        " Server-verified indexed investment facts are ready. Call "
        "calculate_indexed_return_difference using its prepared tool_arguments. "
        "The common initial investment is source evidence, not a default. "
        "The difference between normalized returns is measured in percentage points; "
        "raw end-value subtraction alone does not complete it. "
        + json.dumps(inputs, ensure_ascii=False, default=str)
    )


def _percent_change_instruction(ledger) -> str:
    """百分比变化：时间维度来源事实已就绪时返回预备参数说明，否则返回空串。"""
    import json

    from src.agent.source_facts import period_calculation_inputs

    try:
        inputs = period_calculation_inputs(ledger.prompt, ledger.entries)
    except ValueError:
        inputs = None
    if inputs is None:
        return ""
    return (
        " Server-verified temporal source facts are ready. Use these exact role-bound "
        "arguments for calculate_evidence_arithmetic(operation='percent_change'); "
        "do not reopen retrieval or reuse an earlier turn's calculation. "
        + json.dumps(inputs, ensure_ascii=False)
    )


def _prepared_instruction(ledger) -> str:
    """汇总导出与预备算术参数说明；后出现的预备参数说明会覆盖先前的内容。"""
    prepared = ""
    if "export" in ledger.required:
        prepared += _export_instruction()
    if "arithmetic:indexed_return_difference" in ledger.required:
        indexed = _indexed_return_instruction(ledger)
        if indexed:
            prepared = indexed
    if "arithmetic:percent_change" in ledger.required:
        percent = _percent_change_instruction(ledger)
        if percent:
            prepared = percent
    return prepared


def document_instructions(ledger, collection_count: int) -> str:
    """生成仅文档请求的完整指令：固定正文、必需证据、已成功证据摘要和预备算术参数。"""
    prepared = _prepared_instruction(ledger)
    return (
        _document_only_instruction(collection_count)
        + "Required evidence: "
        + ", ".join(sorted(ledger.required))
        + ". Successful evidence: "
        + ledger.summary()
        + prepared
    )


def _document_only_instruction(collection_count: int) -> str:
    """仅文档请求的固定指令正文（不含必需证据与已成功证据摘要）。"""
    return (
        "This request is explicitly document-only. SQL, filesystem, web and external tools are not part of it. "
        f"Authorized knowledge collections: {collection_count}. "
        "Use search_knowledge_documents first, then answer every requested fact with its actual source and evidence ID. "
        "For an explicitly requested numerical calculation, call calculate_evidence_arithmetic after retrieval; "
        "a document need not state the derived percentage verbatim. Use the exact requested metric, periods and units. "
        "Read paired dates and amounts in their stated order, including 'respectively'; do not substitute an unrelated row. "
        "For tables, match each value to BOTH its row label and column header. Use the requested ending period, "
        "not an earlier column. A normalized investment value is not itself a percentage return: use the stated "
        "initial investment as the denominator before comparing returns. For maturity schedules, distinguish "
        "amounts due within the next reporting year from total debt; explain this interpretation instead of "
        "treating a missing literal 'current' row as a factual zero. Do not invent dates, balances or classifications. "
        "percent_change uses raw new and old values: (new-old)/old*100. percentage is part/whole*100. "
        "For dated growth comparisons, source metric, period and units must bind to each operand. "
        "If the tool reports a wrong period operand, correct it from the stated source facts; do not repeat the same arguments. "
        "Complete missing operations before answering. Keep the final answer concise, in the user's language: "
        "requested result, metric/period, source values, source unit, formula and document citation. "
        "Use only the tool's rounded_result and original operands in the final calculation; "
        "copy its numeric formula instead of composing additional symbolic formulas. "
        "do not transcribe long intermediate decimals or introduce additional rounded ratios. "
        "Do not claim that derived arithmetic is impossible merely because the report "
        "does not print the final answer. If source values really are missing, report the specific missing evidence. "
        "Do not repeat successful retrieval or calculations without a concrete missing fact. "
    )
