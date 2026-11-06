"""在业务 SQL 到达隔离的 SQLite 连接之前先行校验。"""

from __future__ import annotations

import sqlglot
from sqlglot import exp


class UnsafeQueryError(ValueError):
    """查询超出租户范围内的只读约定时抛出。"""


class SQLSafetyGuard:
    """只允许针对已配置租户视图的单条 SELECT。

    数据库适配器只把已授权的行复制到独立的内存数据库。这里的解析检查用于减少
    失误和资源滥用，其本身不是租户边界。校验过的 SQL 也绝不能在数据源上执行。
    """

    @classmethod
    def validate(
        cls,
        sql: str,
        *,
        allowed_tables: set[str] | None,
        dialect: str = "sqlite",
        allow_qualified_tables: bool = False,
    ) -> str:
        """返回一条已校验的 SELECT，使用数据源或沙箱的方言。"""
        if not sql.strip():
            raise UnsafeQueryError("SQL query is empty")
        if len(sql) > 20_000:
            raise UnsafeQueryError("SQL query exceeds the length limit")

        try:
            statements = sqlglot.parse(sql, read=dialect)
        except sqlglot.errors.ParseError as exc:
            raise UnsafeQueryError("SQL query could not be parsed") from exc

        if len(statements) != 1 or not isinstance(statements[0], exp.Select):
            raise UnsafeQueryError("Only one read-only SELECT statement is allowed")

        statement = statements[0]
        # WITH 子句有局部别名，但它引用的源表仍要通过下面的检查。
        cte_names = {cte.alias.lower() for cte in statement.find_all(exp.CTE)}
        for table in statement.find_all(exp.Table):
            # 受信任的 PostgreSQL 配置可以指定源 schema；Agent 的 SQL
            # 始终针对沙箱内不带限定的公开视图。
            if (table.db or table.catalog) and not allow_qualified_tables:
                raise UnsafeQueryError("Qualified database names are not allowed")
            if (
                allowed_tables is not None
                and table.name.lower() not in {name.lower() for name in allowed_tables} | cte_names
            ):
                raise UnsafeQueryError(f"Table {table.name!r} is not available")

        for forbidden in (
            exp.Insert,
            exp.Update,
            exp.Delete,
            exp.Create,
            exp.Drop,
            exp.Alter,
            exp.Into,
            exp.Lock,
        ):
            if statement.find(forbidden):
                raise UnsafeQueryError("SQL query contains a write operation")

        return statement.sql(dialect=dialect)
