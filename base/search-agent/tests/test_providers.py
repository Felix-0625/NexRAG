import pytest

from search_agent.config import settings
from search_agent.providers import (
    _create_provider,
    get_provider,
    init_provider,
    set_provider_for_testing,
)
from search_agent.providers.searxng import SearxngProvider
from search_agent.providers.staan import StaanProvider


class TestCreateProvider:
    def test_searxng(self):
        assert isinstance(_create_provider("searxng"), SearxngProvider)

    def test_staan_with_key(self, monkeypatch):
        monkeypatch.setattr(settings, "staan_api_key", "test-staan-key")
        assert isinstance(_create_provider("staan"), StaanProvider)

    def test_staan_without_key_raises(self, monkeypatch):
        monkeypatch.setattr(settings, "staan_api_key", "")
        with pytest.raises(RuntimeError, match="SEARCH_AGENT_STAAN_API_KEY"):
            _create_provider("staan")


class TestRegistry:
    def test_lazy_default_is_searxng(self):
        # conftest pins SEARCH_AGENT_SEARCH_PROVIDER=searxng
        assert isinstance(get_provider(), SearxngProvider)

    def test_get_provider_returns_same_instance(self):
        assert get_provider() is get_provider()

    def test_init_provider_resolves_from_settings(self, monkeypatch):
        monkeypatch.setattr(settings, "search_provider", "staan")
        monkeypatch.setattr(settings, "staan_api_key", "test-staan-key")
        init_provider()
        assert isinstance(get_provider(), StaanProvider)

    def test_init_provider_fails_fast_without_staan_key(self, monkeypatch):
        monkeypatch.setattr(settings, "search_provider", "staan")
        monkeypatch.setattr(settings, "staan_api_key", "")
        with pytest.raises(RuntimeError):
            init_provider()

    def test_set_provider_for_testing_roundtrip(self):
        fake = StaanProvider()
        set_provider_for_testing(fake)
        assert get_provider() is fake
        set_provider_for_testing(None)
        assert isinstance(get_provider(), SearxngProvider)
