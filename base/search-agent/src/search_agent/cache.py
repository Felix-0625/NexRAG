"""Shared cache with a pluggable backend.

Production uses `RedisBackend` so state is shared across all pods. `InMemoryBackend`
is only for tests and local dev — it fragments per process and would defeat hit
rates if used in production.

All operations fail open: on any backend error the caller sees a miss and carries
on with the underlying work. A dead or slow cache must never break search.
"""

import contextvars
import hashlib
import json
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Protocol

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from search_agent.config import settings

logger = logging.getLogger(__name__)

# Namespace version. Bump when a cached value's shape changes instead of
# flushing the backend. Keys look like `fetch:v1:<sha256>`.
_NAMESPACE_VERSION = "v1"


def make_key(namespace: str, *parts: Any) -> str:
    """Build a versioned, hashed cache key for `namespace` from `parts`."""
    joined = "|".join(str(p) for p in parts)
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()
    return f"{namespace}:{_NAMESPACE_VERSION}:{digest}"


class CacheBackend(Protocol):
    async def get_json(self, key: str) -> Any | None: ...
    async def set_json(self, key: str, value: Any, ttl: int) -> None: ...
    async def close(self) -> None: ...


class DisabledBackend:
    """No-op backend — every get is a miss, every set is a drop."""

    async def get_json(self, key: str) -> Any | None:
        return None

    async def set_json(self, key: str, value: Any, ttl: int) -> None:
        return None

    async def close(self) -> None:
        return None


class InMemoryBackend:
    """Process-local cache. Test and dev use only."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[Any, float]] = {}

    async def get_json(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if time.monotonic() > expires_at:
            self._store.pop(key, None)
            return None
        return value

    async def set_json(self, key: str, value: Any, ttl: int) -> None:
        self._store[key] = (value, time.monotonic() + ttl)

    async def close(self) -> None:
        self._store.clear()


class RedisBackend:
    """Redis-backed cache with tight timeouts and fail-open semantics."""

    def __init__(self, client: aioredis.Redis) -> None:
        self._client = client

    @classmethod
    def from_url(cls, url: str) -> "RedisBackend":
        client = aioredis.from_url(
            url,
            socket_connect_timeout=0.3,
            socket_timeout=0.3,
            decode_responses=False,
        )
        return cls(client)

    async def get_json(self, key: str) -> Any | None:
        try:
            raw = await self._client.get(key)
        except (RedisError, TimeoutError, OSError) as exc:
            logger.warning("cache get failed for %s: %s", key, exc)
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning("cache value for %s was not valid JSON; treating as miss", key)
            return None

    async def set_json(self, key: str, value: Any, ttl: int) -> None:
        try:
            payload = json.dumps(value)
        except (TypeError, ValueError) as exc:
            logger.warning("cache set failed to serialize value for %s: %s", key, exc)
            return
        try:
            await self._client.setex(key, ttl, payload)
        except (RedisError, TimeoutError, OSError) as exc:
            logger.warning("cache set failed for %s: %s", key, exc)

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except (RedisError, OSError):
            pass


_backend: CacheBackend | None = None


def init_cache() -> None:
    """Initialize the shared cache backend from settings. Call during lifespan startup."""
    global _backend
    backend_name = settings.cache_backend
    if backend_name == "redis":
        _backend = RedisBackend.from_url(settings.cache_redis_url)
        logger.info("cache backend=redis url=%s", settings.cache_redis_url)
    elif backend_name == "memory":
        _backend = InMemoryBackend()
        # WARNING (not INFO) so this surfaces in log alerting — the memory
        # backend is per-process and silently fragments state in multi-pod
        # deployments, which can mask incidents until a cache-coherence bug
        # appears in prod.
        logger.warning("cache backend=memory (tests/dev only — do NOT use in multi-pod prod)")
    else:
        _backend = DisabledBackend()
        logger.info("cache backend=disabled")


async def close_cache() -> None:
    """Close the shared cache backend. Call during lifespan shutdown."""
    global _backend
    if _backend is not None:
        await _backend.close()
        _backend = None


def get_backend() -> CacheBackend:
    """Return the shared cache backend. Falls back to disabled if not initialized."""
    if _backend is None:
        return DisabledBackend()
    return _backend


def set_backend_for_testing(backend: CacheBackend | None) -> None:
    """Swap the module-level backend (tests only)."""
    global _backend
    _backend = backend


_bypass: contextvars.ContextVar[bool] = contextvars.ContextVar("cache_bypass", default=False)


@contextmanager
def bypass() -> Iterator[None]:
    """Force cache misses and skip writes for the scope of the context.

    Use at request boundaries when the caller asks for fresh results
    (e.g. ``SearchRequest.no_cache=True``).
    """
    token = _bypass.set(True)
    try:
        yield
    finally:
        _bypass.reset(token)


def is_bypassed() -> bool:
    return _bypass.get()


async def get_json(key: str) -> Any | None:
    if is_bypassed():
        return None
    return await get_backend().get_json(key)


async def set_json(key: str, value: Any, ttl: int) -> None:
    if is_bypassed():
        return
    await get_backend().set_json(key, value, ttl)
