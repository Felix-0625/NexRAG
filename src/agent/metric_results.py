"""独立于模型所选措辞，渲染受信任的指标值。"""

from dataclasses import replace
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class BusinessReport(BaseModel):
    """Commit references to verified results; numerical values are server-owned."""

    model_config = ConfigDict(extra="forbid")
    metric_ids: list[str] = Field(default_factory=list, max_length=10)
    detail_evidence_ids: list[str] = Field(default_factory=list, max_length=10)
    knowledge_evidence_ids: list[str] = Field(default_factory=list, max_length=10)
    other_evidence_ids: list[str] = Field(default_factory=list, max_length=10)


def business_report_ready(ledger):
    """所有已绑定的交付物（包括导出/计算）都必须有依据。"""
    from src.agent.derived_calculation import validated_quotes
    from src.agent.detail_contract import bounded_detail_only_ready
    from src.agent.indexed_returns import completed_comparisons
    from src.agent.temporal_report import temporal_results

    has_result = (
        any(e["kind"] == "metric" for e in ledger.entries)
        or validated_quotes(ledger.entries)
        or temporal_results(ledger)
        or completed_comparisons(ledger)
        or bounded_detail_only_ready(ledger)
    )
    return bool(has_result) and not ledger.missing()


async def prepare_business_output(ctx, definitions):
    """仅在目录指标值真实存在之后，才暴露结构化报告。"""
    return definitions if business_report_ready(ctx.deps.ledger) else []


def _export_phase_tools(ledger, definitions):
    """文档报告阶段的可用工具：先检索，再进行显式授权的写入。"""
    # 文档报告需要先检索，再进行显式授权的写入。
    # 导出参数校验失败时，不能重新放开记忆或文件系统读取。
    has_documents = any(entry["kind"] == "kb" and entry["data"] for entry in ledger.entries)
    allowed = {"export_analysis"} if has_documents else {"search_knowledge_documents"}
    if not ledger.missing():
        allowed = set()
    return [tool for tool in definitions if tool.name in allowed]


def _indexed_return_allowed(ledger):
    """指数化收益差任务的可用工具名：证据齐备后只放开计算，计算完成后不再开放。"""
    from src.agent.indexed_returns import prepare_comparison

    try:
        ready = prepare_comparison(ledger.prompt, ledger.entries)
    except ValueError:
        ready = None
    if ready is None:
        return {"search_knowledge_documents", "calculate_indexed_return_difference"}
    return {"calculate_indexed_return_difference"} if ledger.missing() else set()


def _percent_change_override(ledger):
    """期间百分比变化任务：来源事实足够时返回收窄后的工具名集合，否则返回 None。"""
    from src.agent.source_facts import period_calculation_inputs

    try:
        ready = period_calculation_inputs(ledger.prompt, ledger.entries)
    except ValueError:
        return None
    if ready is None:
        return None
    # 收集 -> 计算 -> 回答：参数出错后的重试不能重新打开已经足够的知识库检索。
    return {"calculate_evidence_arithmetic"} if ledger.missing() else set()


def _knowledge_only_allowed(ledger):
    """显式纯文档任务的可用工具名集合。"""
    # 显式的纯文档任务只有一种收集能力。
    # 查询构造和答案生成仍由 Agent 负责，不暴露无关的 schema、文件系统或外部工具。
    allowed = {"search_knowledge_documents"}
    if any(e["kind"] == "kb" and e["data"] for e in ledger.entries):
        allowed.add("calculate_evidence_arithmetic")
        if ledger.required == {"kb", "arithmetic:indexed_return_difference"}:
            allowed = _indexed_return_allowed(ledger)
        if ledger.required == {"kb", "arithmetic:percent_change"}:
            narrowed = _percent_change_override(ledger)
            if narrowed is not None:
                allowed = narrowed
    return allowed


async def prepare_business_tools(ctx, definitions):
    """类型化报告就绪后直接收尾，不再重新打开数据收集。

    使用 PydanticAI 支持的逐步工具准备机制，对不强制指定 tool_choice 的 Ollama 同样有效；
    最终输出仍由模型产出，常规的授权和报告校验器照常运行。
    """
    from src.agent.task_intent import knowledge_only_request, prohibited_tools

    blocked = prohibited_tools(ctx.deps.ledger.prompt)
    definitions = [tool for tool in definitions if tool.name not in blocked]
    if ctx.deps.ledger.required == {"web"}:
        return [tool for tool in definitions if tool.name == "search_web_engine"]

    if ctx.deps.ledger.registered_metric_ids == frozenset():
        definitions = [tool for tool in definitions if tool.name != "query_business_metrics"]
    ledger = ctx.deps.ledger
    if ledger.required == {"kb", "export"}:
        return _export_phase_tools(ledger, definitions)
    if knowledge_only_request(ctx.deps.ledger.prompt):
        if all(item == "kb" or item.startswith("arithmetic:") for item in ctx.deps.ledger.required):
            allowed = _knowledge_only_allowed(ledger)
            return [tool for tool in definitions if tool.name in allowed]
        definitions = [
            tool
            for tool in definitions
            if tool.name not in {"query_database", "query_business_metrics"}
        ]
    from src.agent.derived_calculation import CALCULATION, quote_from_evidence

    ledger = ctx.deps.ledger
    missing = ledger.missing()
    if CALCULATION in missing and not (missing - {CALCULATION, "export"}):
        # 仅当现有证据充分且无歧义时才进入计算阶段。
        # 实际工具仍会重新授权、计算并记录来源；这里的就绪检查并不代表任务完成。
        try:
            quote_from_evidence(ledger.entries, ledger.prompt)
        except ValueError:
            pass
        else:
            calculators = [tool for tool in definitions if tool.name == "calculate_discount_quote"]
            if calculators:
                return calculators
    return [] if business_report_ready(ctx.deps.ledger) else definitions


def normalize_report_mode(request_context):
    """可选的输出工具被隐藏时，让普通聊天保持文本模式。"""
    params = request_context.model_request_parameters
    if not params.output_tools and params.allow_text_output:
        return replace(
            request_context, model_request_parameters=replace(params, output_mode="text")
        )
    return request_context


def report_from_evidence(ledger):
    """直接提交成功的证据引用，不再让模型重复抄写。

    部分支持工具的本地模型仍会以文本收尾，而该文本不得提供业务数值：
    服务器只选取真实的类型化指标和已认证的明细证据，再套用同一个报告校验器。
    """
    from src.agent.detail_contract import completes_detail_request

    return BusinessReport(
        metric_ids=list(
            dict.fromkeys(e["data"]["metric_id"] for e in ledger.entries if e["kind"] == "metric")
        ),
        detail_evidence_ids=[
            e["id"]
            for e in ledger.entries
            if e["kind"] == "db" and completes_detail_request(e, ledger.prompt)
        ],
        knowledge_evidence_ids=[e["id"] for e in ledger.entries if e["kind"] == "kb"],
        other_evidence_ids=[
            e["id"] for e in ledger.entries if e["kind"] in {"tool", "file", "web", "export"}
        ],
    )


def _escape_cell(value):
    """转义 Markdown 表格单元格中的竖线与换行。"""
    return str(value).replace("|", "\\|").replace("\n", " ")


def _render_without_metrics(report, ledger):
    """既无实际指标也无已验证报价时，尝试按收益率比较或期间报告渲染。

    返回 None 表示仅凭受限明细即可继续后续的通用渲染；其余情况抛出 ValueError。
    """
    from src.agent.detail_contract import bounded_detail_only_ready
    from src.agent.indexed_return_tool import render_comparison
    from src.agent.indexed_returns import completed_comparisons
    from src.agent.temporal_report import render_temporal_report, temporal_results

    if completed_comparisons(ledger):
        return render_comparison(report, ledger)
    if temporal_results(ledger):
        return render_temporal_report(report, ledger)
    if not bounded_detail_only_ready(ledger):
        raise ValueError("报告缺少实际指标或已验证的计算结果")
    return None


def _check_report_references(report, ledger, by_id, actual_metrics):
    """校验报告引用的指标、各类证据ID以及请求的明细/知识/字段/连接依据。"""
    if set(report.metric_ids) != actual_metrics:
        raise ValueError("报告须引用本轮实际返回的全部指标ID，不能添加、替换或省略指标")
    for ids, kinds in [
        (report.detail_evidence_ids, {"db"}),
        (report.knowledge_evidence_ids, {"kb"}),
        (report.other_evidence_ids, {"tool", "file", "web", "export"}),
    ]:
        if any(
            identifier not in by_id or by_id[identifier]["kind"] not in kinds for identifier in ids
        ):
            raise ValueError("报告引用了不存在或类型不符的证据ID")
    if "db_details" in ledger.required and not report.detail_evidence_ids:
        raise ValueError("请求的业务明细缺少实际查询依据")
    if "kb" in ledger.required and not report.knowledge_evidence_ids:
        raise ValueError("请求的知识结论缺少本轮检索依据")
    from src.agent.business_contract import completed_catalog_requirements

    detail_requirements = {
        item for item in ledger.required if item.startswith(("db_join:", "db_field:"))
    }
    selected = [by_id[identifier] for identifier in report.detail_evidence_ids]
    missing = detail_requirements - completed_catalog_requirements(selected, ledger.required)
    if missing:
        raise ValueError("报告选定的明细缺少请求字段或关联依据：" + ", ".join(sorted(missing)))


def _detail_sections(report, ledger, by_id):
    """把选定的明细证据渲染为 Markdown 表格段落，并校验其满足所请求的行粒度。"""
    from src.agent.detail_contract import completes_detail_request, requested_detail_limit

    sections = []
    for identifier in dict.fromkeys(report.detail_evidence_ids):
        entry = by_id[identifier]
        if not completes_detail_request(entry, ledger.prompt):
            raise ValueError("明细证据未证明完整的业务行粒度")
        rows = entry["data"]
        if not rows:
            sections.append(f"[{identifier}] 本次明细查询返回0行。")
            continue
        keys = list(rows[0])
        sections.extend(
            [
                f"\n**业务明细 [{identifier}]**\n",
                "| " + " | ".join(map(_escape_cell, keys)) + " |",
                "| " + " | ".join("---" for _ in keys) + " |",
            ]
        )
        sections.extend(
            "| " + " | ".join(_escape_cell(row.get(key, "")) for key in keys) + " |" for row in rows
        )
        if limit := requested_detail_limit(ledger.prompt):
            sections.append(
                f"按请求限制为{limit}条明细；本次查询返回{len(rows)}条，不代表业务总数或总额。"
            )
        else:
            sections.append(f"查询返回行数：{len(rows)}。业务指标另列，二者口径独立。")
    return sections


def _other_evidence_sections(report, by_id):
    """渲染折扣报价，其余工具/文件/网页/导出证据交给台账的安全回答。"""
    from src.agent.derived_calculation import validated_quotes
    from src.agent.grounding import EvidenceLedger

    sections = []
    other = [by_id[key] for key in report.other_evidence_ids]
    quotes = validated_quotes(other)
    for entry in quotes:
        result, binding = entry["data"], entry["binding"]
        sections.append(
            f"\n**折扣报价 [{entry['id']}]**\n\n"
            f"总金额：{result['original_amount']}。\n\n"
            f"折扣比例：{result['discount_percent']}%。\n\n"
            f"优惠金额：{result['discount_amount']}。\n\n"
            f"折后金额：{result['quote_amount']}。\n\n"
            f"金额依据：{binding['basis_evidence_id']}.{binding['amount_column']}；"
            f"比例依据：{binding['rate_evidence_id']}。"
        )
    remaining = [entry for entry in other if entry not in quotes]
    if remaining:
        sections.append(EvidenceLedger(entries=remaining).safe_answer())
    return sections


def render_business_body(report, ledger):
    """把模型提交的业务报告渲染为正文；引用必须存在于本轮账本，数值取自账本证据而非报告文本。"""
    from src.agent.derived_calculation import validated_quotes

    by_id = {entry["id"]: entry for entry in ledger.entries}
    actual_metrics = {
        entry["data"]["metric_id"] for entry in ledger.entries if entry["kind"] == "metric"
    }
    if not actual_metrics and not validated_quotes(ledger.entries):
        special = _render_without_metrics(report, ledger)
        if special is not None:
            return special
    _check_report_references(report, ledger, by_id, actual_metrics)
    sections = ["已在当前授权范围内核验以下结果。"]
    sections.extend(_detail_sections(report, ledger, by_id))
    if report.other_evidence_ids:
        sections.extend(_other_evidence_sections(report, by_id))
    return "\n".join(sections)


def metric_stats(entries):
    """汇总指标证据中有数值的计数类与金额类结果，返回 (计数集合, 金额集合)。"""
    counts, amounts = set(), set()
    for entry in entries:
        if entry["kind"] != "metric" or entry["data"]["value"] is None:
            continue
        metric = entry["data"]
        value = Decimal(str(metric["value"]))
        if metric["kind"] == "count":
            counts.add(value)
        elif metric["kind"] == "amount":
            amounts.add(value)
    return counts, amounts


def render_metric_results(entries):
    """把指标证据渲染为 Markdown 表格，相同指标、范围、快照和过滤条件只保留一行；无指标时返回空串。"""
    rows, seen = [], set()
    for entry in entries:
        if entry["kind"] != "metric":
            continue
        metric = entry["data"]
        identity = (
            metric["metric_id"],
            metric["scope_id"],
            metric["snapshot_id"],
            str(metric["filters"]),
        )
        if identity in seen:
            continue
        seen.add(identity)

        def escape(value):
            return str(value).replace("|", "\\|").replace("\n", " ")

        value = "未取得数值" if metric["value"] is None else metric["value"]
        rows.append(
            "| "
            + " | ".join(
                map(
                    escape,
                    [
                        metric["label"],
                        value,
                        metric["unit"] or "未定义",
                        metric["metric_id"],
                        metric["view"],
                        entry["query_evidence_id"],
                    ],
                )
            )
            + " |"
        )
    if not rows:
        return ""
    return (
        "\n\n**已核验业务指标**\n\n| 指标 | 数值 | 单位 | 指标ID | 业务视图 | 依据 |\n| --- | --- | --- | --- | --- | --- |\n"
        + "\n".join(rows)
    )
