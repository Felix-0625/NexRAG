"""每个生产/评测轮次一个受治理的业务快照，绝不按用户共享。

ContextVar 会传播到 asyncio 工作线程，无需全局租户缓存。对象本身加锁，因为 PydanticAI 可能并行运行互相独立的工具；
正常输出、拒绝、失败和超时时都会关闭所有资源。
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from contextvars import ContextVar

_current: ContextVar[BusinessReadSession | None] = ContextVar("business_read_session", default=None)


class BusinessReadSession:
    def __init__(self):
        self._lock = threading.RLock()
        self.snapshot = None
        self.adapter = None
        self._closed = False

    def read(self, sql, tenant_id, db_path=None, *, adapter=None, max_rows=50):
        """在本轮唯一的快照上执行只读查询；先校验 SQL，轮次结束后拒绝，且本轮不得更换业务数据源。"""
        from src.midplatform.adapters import create_business_adapter

        with self._lock:
            if self._closed:
                raise ValueError("This business query turn has ended")
            if self.adapter is None:
                self.adapter = adapter or create_business_adapter(db_path)
            elif adapter is not None and adapter is not self.adapter:
                raise ValueError("A turn cannot replace its business source")
            # 首次请求也要先校验，再打开/复制数据源。
            from src.midplatform.analysis_snapshot import validate_query

            validate_query(sql, self.adapter.profile.views, max_rows)
            if self.snapshot is None:
                self.snapshot = self.adapter.open_snapshot(tenant_id)
            rows = self.snapshot.execute_query(sql, tenant_id, max_rows=max_rows)
            from src.midplatform.query_semantics import inspect_query

            return {
                "row_count": len(rows),
                "rows": rows,
                "snapshot_id": self.snapshot.id,
                "query_semantics": inspect_query(sql, self.snapshot.views).as_dict(),
            }

    def close(self):
        """标记本轮已结束并关闭快照；此后的查询都会被拒绝。"""
        with self._lock:
            self._closed = True
            if self.snapshot is not None:
                self.snapshot.close()


def current_business_session():
    return _current.get()


@asynccontextmanager
async def business_read_session():
    """为一个轮次创建并登记独立的业务读取会话；退出时在工作线程中关闭，不阻塞事件循环。"""
    session = BusinessReadSession()
    token = _current.set(session)
    try:
        yield session
    finally:
        _current.reset(token)
        # 已取消的 to_thread 查询可能仍在收尾；等待其快照锁和资源清理时不要阻塞事件循环。
        await asyncio.to_thread(session.close)
