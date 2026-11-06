"""在 Agent 无法给出有依据的答案时，呈现已收集的证据。

这里只负责格式化；授权、证据完整性和数值校验仍留在 ledger 与工具适配层。
不会调用模型或外部服务。
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.agent.grounding import EvidenceLedger

# 渲染顺序：数据库 → 知识库 → 其他工具 → 导出链接。
_KIND_ORDER = {"db": 0, "kb": 1, "export": 3}

MISSING_LABELS = {
    "db": "业务数据库查询",
    "db_details": "业务明细查询",
    "db_count": "业务记录计数",
    "db_total": "业务金额汇总",
    "kb": "知识库检索",
    "web": "联网搜索",
    "export": "文件导出",
    "discount_quote": "基于数据库金额与制度比例的优惠计算（需获授权的计算工具）",
}


def _escape(value) -> str:
    """转义 Markdown 表格单元格：竖线和换行会破坏表格结构。"""
    return str(value).replace("|", "\\|").replace("\n", " ")


def _db_sections(entry, ledger, seen_queries: set) -> list[str]:
    """渲染一次已核验的数据库查询：SQL、结果表，以及可无歧义得出的计数与合计。"""
    # 仅在渲染时导入；证据 ledger 类型归 grounding 所有。
    from src.agent.grounding import EvidenceLedger

    rows = entry["data"]
    query_key = (
        re.sub(r"\s+", " ", entry.get("sql", "")).strip(),
        json.dumps(rows, sort_keys=True, ensure_ascii=False, default=str),
    )
    if query_key in seen_queries:
        return []
    seen_queries.add(query_key)
    sections = [f"\n[{entry['id']}] 已核验数据库查询：\n```sql\n{entry.get('sql', '')}\n```"]
    if not rows:
        return sections + ["本次 SQL 返回 0 条；这不代表整个数据源没有数据。"]
    keys = list(rows[0])
    sections.extend(
        [
            "| " + " | ".join(map(_escape, keys)) + " |",
            "| " + " | ".join("---" for _ in keys) + " |",
        ]
    )
    sections.extend("| " + " | ".join(_escape(row.get(k, "")) for k in keys) + " |" for row in rows)
    summary = EvidenceLedger(entries=[entry], required=ledger.required, prompt=ledger.prompt)
    counts, sums = summary._stats()
    sections.append(f"查询结果行数（包含聚合行）：{len(rows)}。")
    if len(counts) == 1:
        distinct_orders = (entry.get("query_semantics") or {}).get(
            "distinct_order_keys"
        ) and re.search(r"不同订单数|去重.*订单数|distinct.{0,20}order", ledger.prompt, re.I)
        label = "结果中的不同订单数" if distinct_orders else "记录计数"
        sections.append(f"{label}：{next(iter(counts))}。")
    if len(sums) == 1:
        sections.append(f"金额列合计：{next(iter(sums))}。")
    return sections


def _kb_sections(entry, seen_documents: set) -> list[str]:
    """渲染一次知识检索；无结果时区分"已展示或超预算"与"确实没找到"。"""
    if not entry["data"]:
        if entry.get("omitted_count"):
            return [
                f"\n[{entry['id']}] 检索匹配的父块已在前文展示或超出本轮上下文预算；"
                "此处不重复展示，也不能据此推断没有匹配文档。"
            ]
        return [f"\n[{entry['id']}] 检索已执行，但未找到有权限且支持本问题的文档；相关事实未确认。"]
    sections = []
    for document in entry["data"]:
        content = str(document.get("content", ""))[:1800]
        identity = (document.get("source"), content)
        if identity in seen_documents:
            continue
        seen_documents.add(identity)
        sections.append(
            f"\n[{entry['id']}] 文档原文摘录（{document.get('source', '知识文档')}）：\n"
            + "\n".join("> " + line for line in content.splitlines())
        )
    return sections


def render_safe_answer(ledger: EvidenceLedger) -> str:
    """渲染实际证据，而不是让模型编造修补内容。"""
    sections = ["以下内容由本轮实际工具结果生成。"]
    seen_documents: set = set()
    seen_queries: set = set()
    for e in sorted(ledger.entries, key=lambda entry: _KIND_ORDER.get(entry["kind"], 2)):
        if e["kind"] == "db":
            sections.extend(_db_sections(e, ledger, seen_queries))
        elif e["kind"] == "kb":
            sections.extend(_kb_sections(e, seen_documents))
        elif e["kind"] == "export":
            sections.append(f"\n[{e['data']['filename']}]({e['data']['download_url']})")
        elif e["kind"] == "business_denial":
            sections.append(e["data"]["message"])
        elif e["kind"] in {"tool", "file", "web"}:
            dumped = json.dumps(e["data"], ensure_ascii=False, default=str)[:3000]
            sections.append(f"\n[{e['id']}] 工具返回：\n```json\n{dumped}\n```")
    if missing := ledger.missing():
        sections.append(
            "\n未完成："
            + "、".join(MISSING_LABELS.get(kind, kind) for kind in sorted(missing))
            + "。没有用推测补齐这些结果。"
        )
    from src.agent.metric_results import render_metric_results

    return "\n".join(sections) + render_metric_results(ledger.entries)
