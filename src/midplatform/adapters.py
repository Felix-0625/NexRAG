"""基于运维方持有的业务数据源配置，执行租户范围内的分析。

每个已配置的源 SELECT 必须暴露 ``__tenant_id``。适配器先套用所选租户的谓词，
再把行复制到私有只读分析库；用户 SQL 绝不在源连接上执行。
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import psycopg
import yaml

from src.config import settings
from src.midplatform.analysis_snapshot import AnalysisSnapshot, authorize_analysis, validate_query
from src.midplatform.readonly import ensure_postgres_reader
from src.midplatform.safety import SQLSafetyGuard

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SQLITE_TYPES = frozenset({"TEXT", "INTEGER", "REAL", "NUMERIC", "BLOB"})


@dataclass(frozen=True)
class ViewSpec:
    """单个视图对外公开的分析列，以及受信任的源 SELECT。"""

    columns: dict[str, str]
    source_sql: str
    primary_key: tuple[str, ...] = ()
    grain: str | None = None


@dataclass(frozen=True)
class SQLiteSourceProfile:
    """已校验的 SQLite 数据源及其租户范围内的公开视图。"""

    database_path: Path
    tenant_exists_sql: str | None
    views: dict[str, ViewSpec]


@dataclass(frozen=True)
class PostgreSQLSourceProfile:
    """已校验的 PostgreSQL 数据源；凭据来自环境变量。"""

    database_dsn: str = field(repr=False)
    tenant_exists_sql: str | None
    views: dict[str, ViewSpec]


def _read_profile(profile_path: Path) -> tuple[dict, str]:
    """读取配置文件并校验数据源类型，返回原始配置和类型。"""
    if not profile_path.is_file():
        raise FileNotFoundError(profile_path)
    raw = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
    source_type = raw.get("type") if isinstance(raw, dict) else None
    if source_type not in {"sqlite", "postgresql"}:
        raise ValueError("Business source profile type must be sqlite or postgresql")
    return raw, source_type


def _resolve_sqlite_path(raw: dict, profile_path: Path, db_path_override) -> Path:
    """确定 SQLite 数据库路径：显式覆盖优先，相对路径以配置文件目录为基准。"""
    database_name = db_path_override or raw.get("database_path")
    if not isinstance(database_name, (str, Path)) or not str(database_name):
        raise ValueError("Business source database_path is required")
    database_path = Path(database_name)
    if db_path_override is not None:
        database_path = database_path.resolve()
    elif not database_path.is_absolute():
        database_path = profile_path.parent / database_path
    return database_path


def _parse_view(name, configured, source_type: str, dialect: str) -> ViewSpec:
    """校验单个视图的列、源 SELECT、主键和粒度，生成 ViewSpec。"""
    if not isinstance(name, str) or not IDENTIFIER.fullmatch(name):
        raise ValueError(f"Invalid business view name: {name!r}")
    if not isinstance(configured, dict) or not isinstance(configured.get("columns"), dict):
        raise ValueError(f"Business view {name!r} requires columns")
    columns = configured["columns"]
    if not columns or any(
        not isinstance(column, str)
        or not IDENTIFIER.fullmatch(column)
        or not isinstance(kind, str)
        or kind.upper() not in SQLITE_TYPES
        for column, kind in columns.items()
    ):
        raise ValueError(f"Business view {name!r} has invalid columns")
    source_sql = configured.get("source_sql")
    if (
        not isinstance(source_sql, str)
        or (source_type == "sqlite" and "?" in source_sql)
        or "%s" in source_sql
    ):
        raise ValueError(f"Business view {name!r} requires a parameter-free source SELECT")
    primary_key = configured.get("primary_key", [])
    if (
        not isinstance(primary_key, list)
        or any(not isinstance(key, str) or key not in columns for key in primary_key)
        or len(set(primary_key)) != len(primary_key)
    ):
        raise ValueError(f"Business view {name!r} has an invalid primary_key")
    grain = configured.get("grain")
    if grain is not None and (not isinstance(grain, str) or not grain.strip()):
        raise ValueError(f"Business view {name!r} has an invalid grain")
    return ViewSpec(
        {column: kind.upper() for column, kind in columns.items()},
        SQLSafetyGuard.validate(
            source_sql,
            allowed_tables=None,
            dialect=dialect,
            allow_qualified_tables=source_type == "postgresql",
        ),
        tuple(primary_key),
        grain,
    )


def _parse_tenant_exists_sql(raw: dict, source_type: str, dialect: str) -> str | None:
    """校验租户存在性查询必须恰好含一个租户参数，并通过 SQL 安全检查。"""
    tenant_exists_sql = raw.get("tenant_exists_sql")
    if tenant_exists_sql is not None:
        placeholder = "?" if source_type == "sqlite" else "%s"
        if not isinstance(tenant_exists_sql, str) or tenant_exists_sql.count(placeholder) != 1:
            raise ValueError("tenant_exists_sql must contain exactly one tenant parameter")
        # SQLGlot 校验时用字面量替代驱动占位符；
        # 原始查询仍保留参数绑定。
        SQLSafetyGuard.validate(
            tenant_exists_sql.replace(placeholder, "NULL"),
            allowed_tables=None,
            dialect=dialect,
            allow_qualified_tables=source_type == "postgresql",
        )
        if source_type == "sqlite":
            tenant_exists_sql = SQLSafetyGuard.validate(tenant_exists_sql, allowed_tables=None)
    return tenant_exists_sql


def load_source_profile(
    path: str | Path,
    db_path_override: str | Path | None = None,
    db_dsn_override: str | None = None,
) -> SQLiteSourceProfile | PostgreSQLSourceProfile:
    """加载受信任的部署配置，并拒绝格式错误的定义。"""
    profile_path = Path(path).resolve()
    raw, source_type = _read_profile(profile_path)
    dialect = "postgres" if source_type == "postgresql" else "sqlite"
    if source_type == "sqlite":
        database_path = _resolve_sqlite_path(raw, profile_path, db_path_override)
    else:
        database_dsn = db_dsn_override or settings.BUSINESS_DB_DSN
        if not database_dsn:
            raise ValueError("BUSINESS_DB_DSN is required for a PostgreSQL source")

    configured_views = raw.get("views")
    if not isinstance(configured_views, dict) or not configured_views:
        raise ValueError("Business source must declare at least one view")
    views: dict[str, ViewSpec] = {
        name: _parse_view(name, configured, source_type, dialect)
        for name, configured in configured_views.items()
    }
    tenant_exists_sql = _parse_tenant_exists_sql(raw, source_type, dialect)
    if source_type == "sqlite":
        return SQLiteSourceProfile(database_path.resolve(), tenant_exists_sql, views)
    return PostgreSQLSourceProfile(database_dsn, tenant_exists_sql, views)


def _populate_view(
    sandbox: sqlite3.Connection, name: str, view: ViewSpec, source_rows: Iterable[tuple]
) -> None:
    """仅把经租户过滤的源数据行复制到共享的 SQLite 沙箱。"""
    schema = ", ".join(
        f'"{column}" {kind}' + (" NOT NULL" if column in view.primary_key else "")
        for column, kind in view.columns.items()
    )
    if view.primary_key:
        schema += ", PRIMARY KEY (" + ", ".join(f'"{key}"' for key in view.primary_key) + ")"
    sandbox.execute(f'CREATE TABLE "{name}" ({schema})')
    placeholders = ", ".join("?" for _ in view.columns)

    def adapted_rows():
        for row in source_rows:
            # PostgreSQL 驱动把日期、UUID、Decimal 暴露为 Python 对象；
            # SQLite 在列亲和性下接受它们的字符串形式。
            yield tuple(
                value
                if value is None or isinstance(value, (str, int, float, bytes))
                else str(value)
                for value in row
            )

    sandbox.executemany(f'INSERT INTO "{name}" VALUES ({placeholders})', adapted_rows())


class SQLiteAdapter:
    """在已配置视图的租户过滤副本上执行有界 SQL。

    ``tenant_id`` 必须来自已认证且已授权的调用方；仅凭浏览器端的选择
    不能证明有权访问数据库。
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        profile_path: str | Path | None = None,
        profile: SQLiteSourceProfile | None = None,
    ):
        loaded = profile or load_source_profile(
            profile_path or settings.BUSINESS_SOURCE_PATH, db_path
        )
        if not isinstance(loaded, SQLiteSourceProfile):
            raise ValueError("SQLiteAdapter requires a SQLite source profile")
        self.profile = loaded

    def execute_query(
        self, sql: str, tenant_id: str, *, max_rows: int = 100, timeout_seconds: float = 2.0
    ) -> list[dict[str, Any]]:
        """Agent 轮次之外的便捷读取：每次取一份全新的源快照。"""
        safe_sql = validate_query(sql, self.profile.views, max_rows, timeout_seconds)
        with self.open_snapshot(tenant_id) as snapshot:
            return snapshot.execute_query(
                safe_sql, tenant_id, max_rows=max_rows, timeout_seconds=timeout_seconds
            )

    def open_snapshot(self, tenant_id: str) -> AnalysisSnapshot:
        """在同一个读事务中复制所有选中的视图，随后关闭数据源。"""
        if not tenant_id:
            raise ValueError("A validated tenant ID is required")

        if not self.profile.database_path.is_file():
            raise FileNotFoundError(self.profile.database_path)
        source_uri = self.profile.database_path.as_uri() + "?mode=ro"
        with closing(sqlite3.connect(source_uri, uri=True)) as source:
            with closing(source.execute("PRAGMA query_only = ON")):
                pass
            source.execute("BEGIN")
            source.set_authorizer(authorize_analysis)
            if self.profile.tenant_exists_sql:
                with closing(
                    source.execute(self.profile.tenant_exists_sql, (tenant_id,))
                ) as exists:
                    if not exists.fetchone():
                        raise ValueError("Selected tenant does not exist")
            sandbox = sqlite3.connect(":memory:", check_same_thread=False)
            try:
                sandbox.row_factory = sqlite3.Row
                for name, view in self.profile.views.items():
                    columns = ", ".join(f'"{column}"' for column in view.columns)
                    scoped_sql = (
                        f"SELECT {columns} FROM ({view.source_sql}) AS scoped "
                        'WHERE scoped."__tenant_id" = ?'
                    )
                    with closing(source.execute(scoped_sql, (tenant_id,))) as source_rows:
                        _populate_view(sandbox, name, view, source_rows)

                return AnalysisSnapshot(sandbox, tenant_id, self.profile.views)
            except BaseException:
                sandbox.close()
                raise


class PostgreSQLAdapter:
    """把已授权的 PostgreSQL 行复制到现有的 SQLite SQL 沙箱。"""

    def __init__(self, *, profile: PostgreSQLSourceProfile):
        self.profile = profile

    def execute_query(
        self, sql: str, tenant_id: str, *, max_rows: int = 100, timeout_seconds: float = 2.0
    ) -> list[dict[str, Any]]:
        """Agent 轮次之外的便捷读取：每次取一份全新的源快照。"""
        safe_sql = validate_query(sql, self.profile.views, max_rows, timeout_seconds)
        with self.open_snapshot(tenant_id) as snapshot:
            return snapshot.execute_query(
                safe_sql, tenant_id, max_rows=max_rows, timeout_seconds=timeout_seconds
            )

    def open_snapshot(self, tenant_id: str) -> AnalysisSnapshot:
        """使用专用只读凭据，在 REPEATABLE READ 隔离级别下读取租户副本。"""
        if not tenant_id:
            raise ValueError("A validated tenant ID is required")

        try:
            with psycopg.connect(self.profile.database_dsn, connect_timeout=5) as source:
                source.read_only = True
                source.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
                ensure_postgres_reader(source)
                source.execute("SET LOCAL statement_timeout = '30000ms'")
                if self.profile.tenant_exists_sql:
                    with source.cursor() as exists:
                        exists.execute(self.profile.tenant_exists_sql, (tenant_id,))
                        if not exists.fetchone():
                            raise ValueError("Selected tenant does not exist")
                sandbox = sqlite3.connect(":memory:", check_same_thread=False)
                try:
                    sandbox.row_factory = sqlite3.Row
                    for name, view in self.profile.views.items():
                        columns = ", ".join(f'"{column}"' for column in view.columns)
                        scoped_sql = (
                            f"SELECT {columns} FROM ({view.source_sql}) AS scoped "
                            'WHERE scoped."__tenant_id" = %s'
                        )
                        with source.cursor(name=f"nexrag_{name}") as source_rows:
                            source_rows.execute(scoped_sql, (tenant_id,))
                            _populate_view(sandbox, name, view, source_rows)
                    return AnalysisSnapshot(sandbox, tenant_id, self.profile.views)
                except BaseException:
                    sandbox.close()
                    raise
        except psycopg.errors.QueryCanceled as exc:
            raise TimeoutError("Business source query exceeded 30 seconds") from exc
        except psycopg.OperationalError as exc:
            raise ConnectionError("PostgreSQL business source is unavailable") from exc


def create_business_adapter(
    db_path: str | Path | None = None, *, profile_path: str | Path | None = None
) -> SQLiteAdapter | PostgreSQLAdapter:
    """选择数据源连接器，同时保持统一受管控的查询接口。"""
    from src.midplatform.workspace import active_connection

    active = active_connection() if profile_path is None else None
    profile = load_source_profile(
        active["source_path"] if active else profile_path or settings.BUSINESS_SOURCE_PATH,
        None if active else db_path,
        active["dsn"] if active else settings.BUSINESS_DB_DSN,
    )
    if isinstance(profile, SQLiteSourceProfile):
        return SQLiteAdapter(profile=profile)
    return PostgreSQLAdapter(profile=profile)
