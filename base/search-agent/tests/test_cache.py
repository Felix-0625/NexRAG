from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from search_agent import cache
from search_agent.cache import (
    DisabledBackend,
    InMemoryBackend,
    RedisBackend,
    make_key,
    set_backend_for_testing,
)
from search_agent.fetch import _fetch_one
from search_agent.providers.searxng import SearxngProvider
from tests.conftest import make_stream_mock

search = SearxngProvider().search


@pytest.fixture
def in_memory_backend():
    backend = InMemoryBackend()
    set_backend_for_testing(backend)
    yield backend
    set_backend_for_testing(None)


class TestMakeKey:
    def test_includes_namespace_and_version(self):
        key = make_key("fetch", "https://example.com")
        assert key.startswith("fetch:v1:")

    def test_same_inputs_same_key(self):
        assert make_key("fetch", "a", 1) == make_key("fetch", "a", 1)

    def test_different_inputs_different_keys(self):
        assert make_key("fetch", "a") != make_key("fetch", "b")

    def test_different_namespaces_different_keys(self):
        assert make_key("fetch", "a") != make_key("searxng", "a")


class TestDisabledBackend:
    async def test_get_is_miss(self):
        backend = DisabledBackend()
        assert await backend.get_json("anything") is None

    async def test_set_is_noop(self):
        backend = DisabledBackend()
        await backend.set_json("k", {"v": 1}, ttl=60)
        assert await backend.get_json("k") is None


class TestInMemoryBackend:
    async def test_roundtrip(self):
        backend = InMemoryBackend()
        await backend.set_json("k", {"v": 1}, ttl=60)
        assert await backend.get_json("k") == {"v": 1}

    async def test_miss_on_unknown_key(self):
        backend = InMemoryBackend()
        assert await backend.get_json("missing") is None

    async def test_expired_entry_is_evicted(self):
        backend = InMemoryBackend()
        # Force an already-expired entry and verify lazy eviction on get.
        backend._store["k"] = ("stale", 0.0)
        assert await backend.get_json("k") is None
        assert "k" not in backend._store


class TestRedisBackendWithFakeRedis:
    @pytest.fixture
    def backend(self):
        fakeredis = pytest.importorskip("fakeredis")
        client = fakeredis.FakeAsyncRedis(decode_responses=False)
        return RedisBackend(client)

    async def test_roundtrip(self, backend):
        await backend.set_json("k", {"v": 1}, ttl=60)
        assert await backend.get_json("k") == {"v": 1}

    async def test_miss(self, backend):
        assert await backend.get_json("missing") is None

    async def test_get_fail_open_on_redis_error(self, backend):
        # Simulate Redis failure on GET
        from redis.exceptions import ConnectionError as RedisConnectionError

        backend._client.get = AsyncMock(side_effect=RedisConnectionError("boom"))
        assert await backend.get_json("k") is None  # fails open

    async def test_set_fail_open_on_redis_error(self, backend):
        from redis.exceptions import ConnectionError as RedisConnectionError

        backend._client.setex = AsyncMock(side_effect=RedisConnectionError("boom"))
        # Should not raise
        await backend.set_json("k", "v", ttl=60)


class TestBypass:
    async def test_bypass_returns_miss_even_when_cached(self, in_memory_backend):
        await cache.set_json("k", "hit", ttl=60)
        assert await cache.get_json("k") == "hit"
        with cache.bypass():
            assert await cache.get_json("k") is None

    async def test_bypass_skips_writes(self, in_memory_backend):
        with cache.bypass():
            await cache.set_json("k", "hit", ttl=60)
        # After exiting, nothing was written
        assert await cache.get_json("k") is None


class TestSearXNGCaching:
    async def test_search_caches_and_reuses(self, in_memory_backend):
        result_body = {
            "results": [
                {
                    "title": "Test",
                    "url": "https://example.com",
                    "content": "Snippet",
                    "engine": "google",
                }
            ]
        }
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.stream = MagicMock(side_effect=lambda *a, **kw: make_stream_mock(result_body))

        r1 = await search(mock_client, "hello")
        r2 = await search(mock_client, "hello")

        assert mock_client.stream.call_count == 1  # second call hit cache
        assert len(r1) == 1 == len(r2)
        assert r1[0].url == r2[0].url

    async def test_search_cache_is_case_insensitive(self, in_memory_backend):
        result_body = {
            "results": [
                {
                    "title": "Test",
                    "url": "https://example.com",
                    "content": "Snippet",
                    "engine": "google",
                }
            ]
        }
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.stream = MagicMock(side_effect=lambda *a, **kw: make_stream_mock(result_body))

        await search(mock_client, "Hello World")
        await search(mock_client, "hello world  ")  # trailing spaces + case
        assert mock_client.stream.call_count == 1


class TestFetchCaching:
    async def test_positive_result_cached(self, in_memory_backend):
        from search_agent import fetch as fetch_mod

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_fetch = AsyncMock(return_value="extracted")
        with patch.object(fetch_mod, "_fetch_one_uncached", mock_fetch):
            t1 = await _fetch_one(mock_client, "https://example.com/a", 10.0, 5000, 2_000_000)
            t2 = await _fetch_one(mock_client, "https://example.com/a", 10.0, 5000, 2_000_000)

        assert t1 == "extracted" == t2
        assert mock_fetch.call_count == 1  # second call served from cache

    async def test_negative_result_cached_too(self, in_memory_backend):
        from search_agent import fetch as fetch_mod

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        with patch.object(fetch_mod, "_fetch_one_uncached", AsyncMock(return_value=None)) as m:
            t1 = await _fetch_one(mock_client, "https://dead.example.com", 10.0, 5000, 2_000_000)
            t2 = await _fetch_one(mock_client, "https://dead.example.com", 10.0, 5000, 2_000_000)

        assert t1 is None
        assert t2 is None
        assert m.call_count == 1  # negative hit on second call

    async def test_bypass_forces_fresh_fetch(self, in_memory_backend):
        from search_agent import fetch as fetch_mod

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        with patch.object(fetch_mod, "_fetch_one_uncached", AsyncMock(return_value="ok")) as m:
            await _fetch_one(mock_client, "https://example.com/x", 10.0, 5000, 2_000_000)
            with cache.bypass():
                await _fetch_one(mock_client, "https://example.com/x", 10.0, 5000, 2_000_000)

        assert m.call_count == 2


class TestPlannerDateBucket:
    async def test_different_day_is_a_miss(self, in_memory_backend):
        # Keys for two different dates must not collide.
        k_today = make_key("planner", "q", "", "2026-04-23")
        k_tomorrow = make_key("planner", "q", "", "2026-04-24")
        assert k_today != k_tomorrow

        await cache.set_json(k_today, ["q today"], ttl=3600)
        assert await cache.get_json(k_today) == ["q today"]
        assert await cache.get_json(k_tomorrow) is None
