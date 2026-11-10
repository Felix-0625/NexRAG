"""Own one governed business snapshot per production or evaluation turn, never per user.

ContextVar propagation reaches asyncio worker threads without a global tenant cache. Lock
shared access because tools may run concurrently; close resources on every exit path.
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from contextvars import ContextVar

_current: ContextVar[BusinessReadSession | None] = ContextVar("business_read_session", default=None)


class BusinessReadSession:
    """Serialize concurrent tools over a turn-local read-only snapshot that cannot span turns."""

    def __init__(self):
        self._lock = threading.RLock()
        self.snapshot = None
        self.adapter = None
        self._closed = False

    def read(self, sql, tenant_id, db_path=None, *, adapter=None, max_rows=50):
        """Validate SQL before reading the turn's sole snapshot; reject closed turns or source
        changes.
        """
        from src.midplatform.adapters import create_business_adapter

        with self._lock:
            if self._closed:
                raise ValueError("This business query turn has ended")
            if self.adapter is None:
                self.adapter = adapter or create_business_adapter(db_path)
            elif adapter is not None and adapter is not self.adapter:
                raise ValueError("A turn cannot replace its business source")
            # Validate even the first query before opening or copying the source.
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
        """End this turn and close its snapshot so subsequent queries are refused."""
        with self._lock:
            self._closed = True
            if self.snapshot is not None:
                self.snapshot.close()


def current_business_session():
    """Return the current async context's business session, or None outside a turn."""
    return _current.get()


@asynccontextmanager
async def business_read_session():
    """Register a turn-local session and close it in a worker thread without blocking the event
    loop.
    """
    session = BusinessReadSession()
    token = _current.set(session)
    try:
        yield session
    finally:
        _current.reset(token)
        # A cancelled to_thread query may still hold the snapshot lock; cleanup must not block the loop.
        await asyncio.to_thread(session.close)
