"""区分显式限定条数的明细样本与完整的业务结果。"""

import re
import unicodedata

import sqlglot
from sqlglot import exp

from src.agent.task_intent import positive_request_text
from src.midplatform.query_semantics import evidence_shape


def requested_detail_limit(prompt: str) -> int | None:
    """只识别现有的显式中文行数写法，不识别任意数字。"""
    text = unicodedata.normalize("NFKC", positive_request_text(prompt))
    if re.search(
        r"全部|所有|全量|完整|最多|至多|不超过|排名|排序|最高|最低|最大|最小|最新|最早|升序|降序|从高到低|从低到高",
        text,
    ):
        return None
    counts = {
        int(value)
        for value in re.findall(
            r"(?:列出|展示|显示|返回|提供|查询|查出|取出|读取|前|任意)\s*(?:前|任意)?\s*(\d+)\s*(?:条|行|笔)",
            text,
        )
    }
    return next(iter(counts)) if len(counts) == 1 and 1 <= next(iter(counts)) <= 50 else None


def completes_detail_request(entry: dict, prompt: str) -> bool:
    """带 LIMIT 的 SELECT 只能证明所请求的样本，绝不能代表聚合结果。"""
    shape = evidence_shape(entry)
    if not shape.get("row_preserving"):
        return False
    limit = requested_detail_limit(prompt)
    if limit is None:
        return not shape.get("limited")
    try:
        query = sqlglot.parse_one(entry.get("sql", ""), read="sqlite")
    except (ValueError, sqlglot.errors.ParseError):
        return False
    if not isinstance(query, exp.Select) or any(
        query.args.get(key) for key in ("offset", "group", "having", "distinct", "with_", "joins")
    ):
        return False
    sql_limit = query.args.get("limit")
    literal = sql_limit.expression if sql_limit else None
    if not isinstance(literal, exp.Literal) or not literal.is_int or int(literal.this) != limit:
        return False
    if query.find(exp.AggFunc, exp.Window, exp.Subquery):
        return False
    tables = list(query.find_all(exp.Table))
    if len(tables) != 1:
        return False
    columns = {}
    for projection in query.expressions:
        column = projection.this if isinstance(projection, exp.Alias) else projection
        if not isinstance(column, exp.Column) or column.is_star:
            return False
        columns[column.name.casefold()] = projection.alias_or_name
    # 保留显式点名的源标识符（如 order_id），包括结果中的别名；常量/表达式不能冒充该列。
    text = unicodedata.normalize("NFKC", positive_request_text(prompt)).casefold()
    named = set(re.findall(r"\b[a-z][a-z0-9]*_[a-z0-9_]+\b", text))
    named -= {table.name.casefold() for table in tables}
    if not named <= columns.keys():
        return False
    rows = entry.get("data")
    return (
        isinstance(rows, list)
        and len(rows) <= limit
        and all(
            isinstance(row, dict) and all(output in row for output in columns.values())
            for row in rows
        )
    )


def bounded_detail_only_ready(ledger) -> bool:
    """仅需数据库明细且请求了条数上限的任务，在已有查询结果满足该明细要求时返回 True。"""
    return (
        ledger.required == {"db", "db_details"}
        and requested_detail_limit(ledger.prompt) is not None
        and any(
            entry["kind"] == "db" and completes_detail_request(entry, ledger.prompt)
            for entry in ledger.entries
        )
    )
