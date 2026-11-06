"""将显式的业务交付物绑定到运营方的语义目录。

名称和别名由目录负责，而不是模型。这里刻意只做有边界的名称解析，不声称能理解所有自然语言查询；
未知的改写说法仍沿用常规证据契约，需要靠评测覆盖。
"""

from __future__ import annotations

import re
import unicodedata

from src.agent.task_intent import positive_request_text


def _named(text: str, name: str) -> bool:
    name = unicodedata.normalize("NFKC", name).casefold().strip()
    if not name:
        return False
    if name.isascii():
        return bool(re.search(rf"(?<![a-z0-9_]){re.escape(name)}(?![a-z0-9_])", text))
    return name in text


def catalog_requirements(prompt: str, definitions: dict, required: set[str]) -> set[str]:
    """解析每个显式请求的指标；一个实体不能满足另一个实体的要求。"""
    if "db" not in required:
        return set()
    text = unicodedata.normalize("NFKC", positive_request_text(prompt)).casefold()
    matched = {
        identifier: spec
        for identifier, spec in definitions.items()
        if isinstance(spec, dict)
        and any(
            _named(text, str(name))
            for name in [identifier, spec.get("description", ""), *spec.get("aliases", [])]
        )
    }
    obligations = {"metric:" + identifier for identifier in matched}
    # 显式的关联请求需要一个经认证的连接结果，而不是两个互不相关的单视图查询。
    # 表名只来自目录。
    tables = {spec["table"] for spec in matched.values() if spec.get("table")}
    if (
        "db_details" in required
        and len(tables) > 1
        and any(word in text for word in ("关联", "联查", "join"))
    ):
        obligations.add("db_join:" + ",".join(sorted(tables)))
        # 完整的关联明细必须包含已声明的指标输入，例如明细行旁的 orders.amount。
        # 这里只校验结果，从不新增查询、改写投影或丢弃用户过滤条件。
        import sqlglot
        from sqlglot import exp

        for spec in matched.values():
            if spec.get("kind") == "count":
                # 关联实体的键可以合法地从连接的任一侧投影，它不是金额明细字段。
                continue
            expression = sqlglot.parse_one(spec["calculation"], read="sqlite")
            obligations |= {
                f"db_field:{spec['table']}.{column.name}".casefold()
                for column in expression.find_all(exp.Column)
            }
    return obligations


def bind_catalog_requirements(ledger, evaluation=None) -> None:
    """在执行之前，使用与生产环境指标工具相同的目录。"""
    if "db" not in ledger.required:
        return
    if evaluation is not None:
        definitions = evaluation.business_metrics or {}
    else:
        from src.config import settings
        from src.midplatform.catalog import MetadataCatalog
        from src.midplatform.workspace import active_connection

        active = active_connection()
        definitions = MetadataCatalog(
            active["metadata_path"] if active else settings.METADATA_PATH
        ).metadata.get("metrics", {})
    ledger.registered_metric_ids = frozenset(definitions)
    ledger.required |= catalog_requirements(ledger.prompt, definitions, ledger.required)


def completed_catalog_requirements(entries, required=()) -> set[str]:
    """只有真实的类型化指标和经认证的连接才能满足目录义务。"""
    import sqlglot
    from sqlglot import exp

    from src.midplatform.query_semantics import evidence_shape

    completed = {
        "metric:" + entry["data"]["metric_id"] for entry in entries if entry["kind"] == "metric"
    }
    for entry in entries:
        if entry["kind"] != "db":
            continue
        shape = evidence_shape(entry)
        if not (
            shape.get("row_preserving")
            and not shape.get("limited")
            and shape.get("join_cardinality")
        ):
            continue
        try:
            query = sqlglot.parse_one(entry.get("sql", ""), read="sqlite")
        except (ValueError, sqlglot.errors.ParseError):
            continue
        tables = sorted({table.name for table in query.find_all(exp.Table)})
        # 要求这些字段出现在同一个连接结果中；ledger 里无关的查询不能掩盖所选明细表的不完整。
        fields = projected_detail_fields(query, entry["data"])
        field_requirements = {
            item.removeprefix("db_field:") for item in required if item.startswith("db_field:")
        }
        completed |= {"db_field:" + name for name in fields}
        if field_requirements <= fields:
            completed.add("db_join:" + ",".join(tables))
    return completed


def projected_detail_fields(query, rows) -> set[str]:
    """用 SQLGlot 解析直接选取的 SELECT 列，包括输出别名。

    amount*0 之类的表达式和无关常量不能代替源列；有歧义的未限定列保持未认证。
    """
    from sqlglot import exp

    aliases = {
        table.alias_or_name.casefold(): table.name.casefold() for table in query.find_all(exp.Table)
    }
    keys = {str(key).casefold() for key in rows[0]} if rows else None
    fields = set()
    for projection in query.expressions:
        node = projection.this if isinstance(projection, exp.Alias) else projection
        if not isinstance(node, exp.Column) or node.is_star:
            continue
        if keys is not None and projection.alias_or_name.casefold() not in keys:
            continue
        table = aliases.get(node.table.casefold()) if node.table else None
        if table is None and len(aliases) == 1:
            table = next(iter(aliases.values()))
        if table:
            fields.add(f"{table}.{node.name.casefold()}")
    return fields
