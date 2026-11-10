"""Validate business SQL before it reaches isolated SQLite."""

from __future__ import annotations

import sqlglot
from sqlglot import exp


class UnsafeQueryError(ValueError):
    """Reject queries outside the tenant read-only contract."""


class SQLSafetyGuard:
    """Allow one SELECT over configured tenant views only.

    Adapters copy authorized rows into isolated databases. Parsing limits mistakes and abuse
    but is not the tenant boundary; never execute user SQL on source connections.
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
        """Return a validated SELECT using the source or sandbox dialect."""
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
        # WITH aliases are local, but their source tables must still pass validation.
        cte_names = {cte.alias.lower() for cte in statement.find_all(exp.CTE)}
        for table in statement.find_all(exp.Table):
            # Trusted PostgreSQL configuration may name source schemas; agent SQL
            # always targets unqualified public sandbox views.
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
