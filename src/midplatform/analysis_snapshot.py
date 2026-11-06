"""一个 Agent 轮次共用的有界、不可变租户快照。

返回该对象时源连接已关闭。SQL 只在私有副本上执行；工具参数和并行调用都无法
替换其租户。锁允许工作线程按顺序使用。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from uuid import uuid4

from src.midplatform.query_semantics import inspect_query
from src.midplatform.safety import SQLSafetyGuard

ALLOWED_FUNCTIONS = frozenset(
    {
        "abs",
        "avg",
        "coalesce",
        "count",
        "date",
        "ifnull",
        "julianday",
        "lower",
        "max",
        "min",
        "round",
        "strftime",
        "substr",
        "sum",
        "total",
        "upper",
    }
)


def authorize_analysis(action, _arg1, arg2, _database, _source):
    """SQLite 授权回调：只放行 SELECT 与读取，函数仅限白名单，其余操作一律拒绝。"""
    if action == sqlite3.SQLITE_FUNCTION:
        return (
            sqlite3.SQLITE_OK if (arg2 or "").lower() in ALLOWED_FUNCTIONS else sqlite3.SQLITE_DENY
        )
    return (
        sqlite3.SQLITE_OK
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ)
        else sqlite3.SQLITE_DENY
    )


def validate_query(sql, views, max_rows=100, timeout_seconds=2.0):
    """校验行数上限（1 至 1000）和超时（至多 30 秒），并用 SQL 安全守卫与语义检查限定在给定视图内，返回安全的 SQL；不合规抛出异常。"""
    if not 1 <= max_rows <= 1_000:
        raise ValueError("max_rows must be between 1 and 1000")
    if not 0 < timeout_seconds <= 30:
        raise ValueError("timeout_seconds must be positive and at most 30")
    safe_sql = SQLSafetyGuard.validate(sql, allowed_tables=set(views))
    inspect_query(safe_sql, views)
    return safe_sql


class AnalysisSnapshot:
    def __init__(self, connection: sqlite3.Connection, tenant_id: str, views):
        self.id = uuid4().hex
        self.tenant_id = tenant_id
        self.views = views
        self._connection = connection
        self._lock = threading.RLock()
        self._closed = False
        connection.commit()
        connection.execute("PRAGMA query_only = ON")
        connection.set_authorizer(authorize_analysis)

    def execute_query(self, sql, tenant_id, *, max_rows=100, timeout_seconds=2.0):
        """在本快照上执行一条只读查询并返回行字典列表。

        租户与快照不一致、快照已关闭、结果超过 max_rows 均抛出 ValueError，超时抛出 TimeoutError。
        """
        if tenant_id != self.tenant_id:
            raise ValueError("A snapshot cannot be reused for another tenant")
        safe_sql = validate_query(sql, self.views, max_rows, timeout_seconds)
        with self._lock:
            if self._closed:
                raise ValueError("The business snapshot has been closed")
            deadline = time.monotonic() + timeout_seconds
            self._connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1_000)
            try:
                cursor = self._connection.execute(safe_sql)
                rows = cursor.fetchmany(max_rows + 1)
            except sqlite3.DatabaseError as exc:
                if time.monotonic() > deadline:
                    raise TimeoutError("Business query exceeded its time limit") from exc
                if "not authorized" in str(exc).lower():
                    raise ValueError("Business query uses an unavailable operation") from exc
                raise
            finally:
                self._connection.set_progress_handler(None, 0)
            if len(rows) > max_rows:
                raise ValueError("Query result exceeds max_rows; aggregate or narrow it")
            return [dict(row) for row in rows]

    def close(self):
        """关闭底层连接；可重复调用，之后不能再执行查询。"""
        with self._lock:
            if not self._closed:
                self._closed = True
                self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
