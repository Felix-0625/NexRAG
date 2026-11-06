"""基于本轮实际工具证据，对任务完成度与数字做确定性检查。

这不是语义真值判官：缺失的能力一律按失败处理；数值声明和常见合计必须有工具数据支撑，而不是靠模型记忆。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from src.agent.task_intent import (
    database_write_request,  # noqa: F401 - 稳定的公开导入
    requested_capabilities,  # noqa: F401 - 稳定的公开导入
)


class GroundingFailure(ValueError):
    pass


def _returned_links(value):
    """成功证据中的精确 URL 字段，而不是其他 URL 的前缀。"""
    if isinstance(value, str):
        return {value} if value.startswith(("https://", "http://", "/api/")) else set()
    if isinstance(value, dict):
        return set().union(*(_returned_links(item) for item in value.values()))
    if isinstance(value, (list, tuple)):
        return set().union(*(_returned_links(item) for item in value))
    return set()


def _numbers(text: str) -> set[Decimal]:
    found = set()
    for raw, unit in re.findall(r"(?<![A-Za-z0-9_])(-?\d+(?:,\d{3})*(?:\.\d+)?)(万|亿)?", text):
        try:
            found.add(Decimal(raw.replace(",", "")) * {"万": 10000, "亿": 100000000, "": 1}[unit])
        except InvalidOperation:
            pass
    return found


def _plain(text: str) -> str:
    # Markdown 序号只表示排版，不是事实：仅去掉列表标记，条目正文里的数字仍需证据支撑。
    text = re.sub(r"(?m)^\s{0,3}\d+[.)]\s+", "", text)
    text = re.sub(r"\]\([^)]*\)", "]", text)
    text = re.sub(r"https?://\S+|/api/\S+", "", text)
    text = re.sub(r"\[(?:DB|KB|TOOL|FILE|WEB)\d+\]", "", text)
    # 只豁免 SQL 来源；把数字/记录 ID 写成行内代码或 CSV/JSON 块，不能绕过证据检查。
    text = re.sub(r"```sql\b.*?```|`\s*SELECT\b[^`]+`", "", text, flags=re.S | re.I)
    text = re.sub(r"[\w.-]+\.(?:csv|md|json|txt|pdf|docx)", "", text)
    return text.replace("`", "")


def _bounded_detail_hint(detail_limit, missing):
    return (
        f"The user requested a bounded detail sample of {detail_limit} rows. "
        f"Use a plain SELECT of the requested source columns with LIMIT {detail_limit}, "
        "no OFFSET, aggregates or invented predicates. Keep the authorized view names "
        "from the schema. Do not remove or shrink the requested LIMIT to complete a "
        "full-detail contract; the sample does not establish business counts or totals. "
        "Other missing capabilities: " + ", ".join(sorted(missing - {"db_details"}))
    )


def _arithmetic_hint(missing):
    if not any(item.startswith("arithmetic:") for item in missing):
        return ""
    return (
        " Call calculate_evidence_arithmetic for each missing arithmetic operation after retrieval. "
        "Use raw source values for the requested metric and periods; respect paired values marked respectively. "
        "The report need not print the calculated answer. Do not substitute a scalar difference for percent_change."
    )


def _mcp_hint(missing):
    if not any(item.startswith("tool:mcp:") for item in missing):
        return ""
    return (
        " Invoke each requested MCP tool in this turn using its approved schema. "
        "An earlier conversation result or a different tool does not fulfill the request."
    )


def _knowledge_hint(missing):
    if "kb" not in missing:
        return ""
    return (
        " No knowledge retrieval was performed. Your next response must call "
        "search_knowledge_documents with topical keywords from the question, not text. "
        "This is internal authorized retrieval, not an external/MCP tool; it remains available "
        "without a business database. Do not claim no matches or denied access before its result."
    )


def _metric_hint(missing):
    if not any(item.startswith("metric:") for item in missing):
        return ""
    return (
        " Query these exact missing registered metric IDs using query_business_metrics: "
        + ", ".join(
            sorted(item.removeprefix("metric:") for item in missing if item.startswith("metric:"))
        )
        + ". A count from another entity does not fulfill them."
    )


def _join_hint(missing):
    if not any(item.startswith("db_join:") for item in missing):
        return ""
    return " The requested related details need one certified joined detail SELECT across the named catalog views; separate single-view rows do not satisfy the relationship."


def _field_hint(missing, required):
    if not any(item.startswith(("db_field:", "db_join:")) for item in missing):
        return ""
    return (
        " In that SAME detail SELECT, include these direct source columns (aliases are allowed): "
        + ", ".join(
            sorted(
                item.removeprefix("db_field:") for item in required if item.startswith("db_field:")
            )
        )
        + ". Keep the requested filters; do not replace a column with a constant or aggregate."
    )


def _details_hint(missing):
    if "db_details" not in missing:
        return ""
    return (
        " For db_details, call query_database with a separate plain SELECT of row columns, "
        "without COUNT/SUM/AVG/MIN/MAX or GROUP BY. Alternatively use a query with server-certified row-preserving grain. "
        "Do not repeat successful totals, policy retrieval or discount calculation."
    )


def _add_result_row_count(counts, rows, shape, required, mixed_grains):
    """未截断且保持行粒度的明细结果，其行数可作为计数证据。"""
    if (
        shape.get("row_preserving")
        and not shape.get("limited")
        and not shape.get("join_cardinality")
        and not mixed_grains
        # 商品/明细视图不能证明订单数量，即使它恰好是唯一成功的查询。
        and not (
            "db_count" in required
            and shape.get("grain") is not None
            and shape["grain"] not in {"order", "orders"}
        )
    ):
        counts.add(Decimal(len(rows)))


def _add_distinct_order_counts(counts, rows, shape, prompt):
    # 只有显式要求的去重订单数，才可由返回连接结果中完整的强制订单键推导；
    # 它不能证明全部源订单（例如没有明细的订单）。
    if re.search(r"不同订单数|去重.*订单数|distinct.{0,20}order", prompt, re.I):
        for keys in shape.get("distinct_order_keys", ()):
            counts.add(
                Decimal(
                    len(
                        {
                            tuple(row.get(key) for key in keys)
                            for row in rows
                            if all(row.get(key) is not None for key in keys)
                        }
                    )
                )
            )


def _add_column_stats(counts, sums, rows, shape, mixed_grains):
    """逐列判断数值列是否可作为计数或金额合计证据。"""
    for key in rows[0]:
        # 窗口合计会在各行重复出现，对其求和会虚构出更大的金额；其原始值仍可作为证据。
        if key in shape.get("window_fields", ()):
            continue
        values = [
            Decimal(str(row[key]))
            for row in rows
            if isinstance(row.get(key), (int, float)) and not isinstance(row[key], bool)
        ]
        if len(values) != len(rows):
            continue
        lower = key.lower()
        if (
            any(term in lower for term in ("count", "笔数", "订单数", "数量"))
            and len(rows) == 1
            and (shape.get("count_fields") is None or key in shape["count_fields"])
        ):
            counts.add(values[0])
        if any(
            term in lower
            for term in ("amount", "revenue", "price", "total", "金额", "收入", "销售额")
        ):
            if (
                not (shape.get("row_preserving") and shape.get("limited"))
                and (shape.get("sum_fields") is None or key in shape["sum_fields"])
                and not (mixed_grains and shape.get("row_preserving"))
            ):
                sums.add(sum(values))


def _factual_source(entries) -> str:
    """把证据序列化为可检索的事实文本；算术工具的 formula 字段不算事实。"""
    factual_data = [
        {k: v for k, v in e["data"].items() if k != "formula"}
        if e["kind"] == "tool"
        and isinstance(e["data"], dict)
        and e["data"].get("tool") == "calculate_evidence_arithmetic"
        else e["data"]
        for e in entries
    ]
    return json.dumps(factual_data, ensure_ascii=False, default=str)


def _unsupported_claim_issues(plain, source, allowed) -> list[str]:
    """答案中的数字和记录 ID 必须能在证据里找到。"""
    problems = []
    unsupported = _numbers(plain) - allowed
    if unsupported:
        problems.append("Unsupported numeric claims: " + ", ".join(map(str, sorted(unsupported))))
    source_ids = set(re.findall(r"\b[A-Z]+-\d[\w-]*\b", source))
    unknown_ids = set(re.findall(r"\b[A-Z]+-\d[\w-]*\b", plain)) - source_ids
    if unknown_ids:
        problems.append("Unsupported record IDs: " + ", ".join(sorted(unknown_ids)))
    return problems


def _total_mismatch_issues(plain, sums, counts) -> list[str]:
    from src.agent.claim_formatting import is_post_discount_total

    problems = []
    for pattern, expected, label in (
        (
            r"(?:总金额|订单总额|金额合计|合计金额|总收入|总销售额)\s*[：:为是]*\s*([\d,]+(?:\.\d+)?)\s*(万|亿)?",
            sums,
            "amount",
        ),
        (
            r"(?:总笔数|订单总数|总订单数|订单数)\s*[：:为是]*\s*([\d,]+(?:\.\d+)?)",
            counts,
            "count",
        ),
    ):
        for match in re.finditer(pattern, plain.replace("**", "")):
            if label == "amount" and is_post_discount_total(plain.replace("**", ""), match.start()):
                continue
            value = next(iter(_numbers(match.group(0))), None)
            if expected and value not in expected:
                problems.append(f"Total {label} does not match query evidence")
    return problems


def _missing_export_issues(required, entries) -> list[str]:
    if "export" in required and not any(e["kind"] == "export" for e in entries):
        return ["Do not claim that a file was generated without export_analysis"]
    return []


def _web_citation_issues(required, entries, answer) -> list[str]:
    if "web" not in required:
        return []
    actual_web_urls = {
        result["url"]
        for entry in entries
        if entry["kind"] == "web"
        for result in entry["data"]
        if result.get("url")
    }
    if actual_web_urls and not any(url in answer for url in actual_web_urls):
        return [
            "Missing actual web citation: answer the requested public topic and cite a URL returned by search_web_engine"
        ]
    return []


def _invented_link_issues(answer, entries) -> list[str]:
    problems = []
    for label, url in re.findall(r"\[([^\]]+)\]\(([^\s)]+)\)", answer):
        artifact_link = re.search(r"导出|下载|产物|export|download", label, re.I) or (
            re.search(r"/(?:export|exports|download|downloads)(?:/|\?|$)", url, re.I)
            and re.search(r"报告|文件|report|file|\.(?:csv|md|txt|json|pdf|docx|xlsx)", label, re.I)
        )
        if artifact_link and not any(url in _returned_links(entry["data"]) for entry in entries):
            problems.append(
                "Do not invent export/download links; use only URLs actually returned by successful tools"
            )
    return problems


@dataclass
class EvidenceLedger:
    required: set[str] = field(default_factory=set)
    entries: list[dict[str, Any]] = field(default_factory=list)
    prompt: str = ""
    rejections: int = 0
    registered_metric_ids: frozenset[str] | None = None

    def add(self, kind: str, data: Any, **meta) -> str:
        """追加一条证据并返回其 ID（类型名大写加同类序号，如 DB1、KB2）。"""
        tag = kind.upper() + str(1 + sum(item["kind"] == kind for item in self.entries))
        self.entries.append(dict(id=tag, kind=kind, data=data, **meta))
        return tag

    def missing(self) -> set[str]:
        """返回必需证据中尚未被已有条目满足的能力；业务范围拒绝证据会满足数据库类要求。"""
        from src.agent.derived_calculation import CALCULATION, validated_quotes

        completed = {item["kind"] for item in self.entries}
        completed |= {
            "tool:" + item["tool_id"]
            for item in self.entries
            if item["kind"] == "tool" and item.get("tool_id")
        }
        from src.agent.business_contract import completed_catalog_requirements

        completed |= completed_catalog_requirements(self.entries, self.required)
        from src.agent.arithmetic_contract import completed_arithmetic

        completed |= completed_arithmetic(self.entries, self.prompt)
        if "business_denial" in completed:
            completed.update({"db", "db_details", "db_count", "db_total"})
            completed |= {
                item
                for item in self.required
                if item.startswith(("metric:", "db_join:", "db_field:"))
            }
        if validated_quotes(self.entries):
            completed.add(CALCULATION)
        from src.agent.detail_contract import completes_detail_request

        for entry in self.entries:
            if entry["kind"] != "db":
                continue
            if completes_detail_request(entry, self.prompt):
                completed.add("db_details")
        counts, sums = self._stats()
        if counts:
            completed.add("db_count")
        if sums:
            completed.add("db_total")
        return self.required - completed

    def summary(self) -> str:
        """以 JSON 返回已成功证据的 id 与 kind 列表，不含证据内容。"""
        return json.dumps([{"id": e["id"], "kind": e["kind"]} for e in self.entries])

    def retry_hint(self) -> str:
        """要求补齐缺失的能力，而不是重复已成功的 SQL。"""
        from src.agent.derived_calculation import CALCULATION, discount_retry_hint

        missing = self.missing()
        from src.agent.detail_contract import requested_detail_limit

        detail_limit = requested_detail_limit(self.prompt)
        if "db_details" in missing and detail_limit is not None:
            return _bounded_detail_hint(detail_limit, missing)
        if missing == {"export"} and self.required == {"kb", "export"}:
            return (
                "Reuse the successful knowledge evidence and call export_analysis now. "
                "For Markdown/text reports use text with the requested source-grounded facts "
                "and actual KB citations, rows=None. Do not repeat retrieval or recall memory; "
                "only a successful export result establishes a download link."
            )
        if CALCULATION in missing and not (missing - {CALCULATION}):
            return discount_retry_hint(self.entries, self.prompt)
        if missing:
            return (
                "Complete only these missing capabilities: "
                + ", ".join(sorted(missing))
                + ". Reuse successful evidence already collected."
                + _arithmetic_hint(missing)
                + _mcp_hint(missing)
                + _knowledge_hint(missing)
                + _metric_hint(missing)
                + _join_hint(missing)
                + _field_hint(missing, self.required)
                + _details_hint(missing)
            )
        return (
            "Correct the final answer using existing evidence; do not repeat successful SQL/knowledge calls. "
            "For derived arithmetic call calculate_evidence_arithmetic with exact quoted source operands. "
            "Do not invent a computed number or drop the requested calculation."
        )

    def export_issues(self, rows, text):
        """校验导出内容：正文须通过除导出外的证据检查，导出的行必须与已有的结构化查询/工具记录一致；返回问题列表。"""
        check = EvidenceLedger(
            required=self.required - {"export"}, entries=self.entries, prompt=self.prompt
        )
        problems = check.issues(text or "")
        if rows:
            candidates = []

            def collect(value):
                if isinstance(value, dict):
                    candidates.append(value)
                    for child in value.values():
                        collect(child)
                elif isinstance(value, list):
                    for child in value:
                        collect(child)

            for entry in self.entries:
                if entry["kind"] in {"db", "metric", "file", "tool"}:
                    collect(entry["data"])
            if any(
                not any(
                    all(k in actual and actual[k] == v for k, v in row.items())
                    for actual in candidates
                )
                for row in rows
            ):
                if not any(
                    entry["kind"] in {"db", "metric", "file", "tool"} for entry in self.entries
                ):
                    problems.append(
                        "Export rows must match actual structured query/tool records. "
                        "Knowledge prose and user-provided report text are not structured rows. "
                        "For an md/txt export of that prose, retry export_analysis with the "
                        "source-grounded report or Markdown table in text, rows=None. "
                        "Reuse the existing KB evidence with its actual citations, or the user's "
                        "provided text; do not query SQL, recall memory, repeat retrieval or invent fields."
                    )
                else:
                    problems.append(
                        "Export rows must match actual query/tool rows; query the exact projected or aggregated columns first"
                    )
        return problems

    def _stats(self):
        from src.agent.metric_results import metric_stats
        from src.midplatform.query_semantics import evidence_shape

        counts, sums = metric_stats(self.entries)
        grains = {
            evidence_shape(entry).get("grain")
            for entry in self.entries
            if entry["kind"] == "db" and evidence_shape(entry).get("grain")
        }
        mixed_grains = len(grains) > 1
        for entry in self.entries:
            if entry["kind"] != "db":
                continue
            rows = entry["data"]
            shape = evidence_shape(entry)
            if not (shape.get("row_preserving") or shape.get("aggregate")):
                continue
            _add_result_row_count(counts, rows, shape, self.required, mixed_grains)
            if not rows:
                continue
            _add_distinct_order_counts(counts, rows, shape, self.prompt)
            _add_column_stats(counts, sums, rows, shape, mixed_grains)
        return counts, sums

    def issues(self, answer: str) -> list[str]:
        """检查最终答案与账本证据是否一致：证据缺失、算术与折扣公式、无依据的数字、合计不符、伪造链接等；返回问题列表。"""
        problems = []
        if missing := self.missing():
            problems.append("Missing successful evidence: " + ", ".join(sorted(missing)))
        if not self.required and not self.entries:
            return problems
        from src.agent.arithmetic_claims import arithmetic_percentage_issues
        from src.agent.claim_formatting import (
            discount_formula_issues,
            normalize_arithmetic_presentation,
            normalize_verified_formulas,
            prompt_reference_text,
        )
        from src.agent.derived_calculation import validated_quotes

        quotes = validated_quotes(self.entries)
        # 答案中出现的取整说明同样属于格式；既不能凭空造出事实性的 0，也不能因此拒绝正确结果。
        presentation = prompt_reference_text(_plain(answer))
        problems.extend(arithmetic_percentage_issues(presentation, self.entries))
        from src.agent.arithmetic_contract import arithmetic_answer_issues

        problems.extend(arithmetic_answer_issues(presentation, self.required, self.entries))
        problems.extend(discount_formula_issues(_plain(answer), quotes))
        plain = normalize_verified_formulas(presentation, quotes)
        plain = normalize_arithmetic_presentation(plain, self.entries)
        source = _factual_source(self.entries)
        allowed = _numbers(source)
        counts, sums = self._stats()
        allowed |= counts | sums
        result_row_counts = {
            Decimal(len(entry["data"]))
            for entry in self.entries
            if entry["kind"] == "db" and isinstance(entry["data"], list)
        }
        allowed |= result_row_counts
        from src.agent.answer_contract import summary_issues

        problems.extend(
            summary_issues(
                answer,
                self.required,
                counts,
                sums,
                result_row_counts=result_row_counts,
                rendered_metrics=self.rendered_metric_capabilities(),
            )
        )
        # 问题中显式给出的日期/条数上限可以重复出现，但它们不满足 DB 完成度，也不满足下面的合计检查。
        allowed |= _numbers(prompt_reference_text(self.prompt))
        problems.extend(_unsupported_claim_issues(plain, source, allowed))
        problems.extend(_total_mismatch_issues(plain, sums, counts))
        problems.extend(_missing_export_issues(self.required, self.entries))
        problems.extend(_web_citation_issues(self.required, self.entries, answer))
        from src.agent.derived_calculation import quote_answer_issues

        problems.extend(_invented_link_issues(answer, self.entries))

        problems.extend(quote_answer_issues(plain.replace("**", ""), self.entries, self.prompt))
        return problems

    def safe_answer(self) -> str:
        """渲染实际证据，而不是让模型编造修补内容。"""
        from src.agent.evidence_fallback import render_safe_answer

        return render_safe_answer(self)

    def rendered_metric_capabilities(self):
        """只有成功的、类型化的目录度量才能满足已渲染的汇总。"""
        from src.agent.metric_results import metric_stats

        counts, amounts = metric_stats(self.entries)
        return ({"db_count"} if counts else set()) | ({"db_total"} if amounts else set())
