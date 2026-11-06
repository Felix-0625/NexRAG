from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def client():
    """Create an async test client with mocked lifespan dependencies."""
    with (
        patch("search_agent.main.init_shared_clients"),
        patch("search_agent.main.close_shared_clients", new_callable=AsyncMock),
        patch("search_agent.main.mcp") as mock_mcp,
    ):
        # Make the MCP session manager a no-op async context manager
        mock_session_mgr = MagicMock()
        mock_run = MagicMock()
        mock_run.__aenter__ = AsyncMock(return_value=None)
        mock_run.__aexit__ = AsyncMock(return_value=None)
        mock_session_mgr.run.return_value = mock_run
        mock_mcp.session_manager = mock_session_mgr

        from search_agent.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


class TestHealthEndpoint:
    async def test_basic_health(self, client):
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    @patch("search_agent.main.get_http_client")
    @patch("search_agent.main.get_provider")
    async def test_deep_health_backend_ok(self, mock_get_provider, mock_get_client, client):
        provider = MagicMock()
        provider.name = "searxng"
        provider.health = AsyncMock(return_value=True)
        mock_get_provider.return_value = provider
        mock_get_client.return_value = AsyncMock(spec=httpx.AsyncClient)

        response = await client.get("/health?deep=true")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["provider"] == "searxng"
        assert data["search_backend"] == "reachable"

    @patch("search_agent.main.get_http_client")
    @patch("search_agent.main.get_provider")
    async def test_deep_health_backend_down(self, mock_get_provider, mock_get_client, client):
        provider = MagicMock()
        provider.name = "searxng"
        provider.health = AsyncMock(return_value=False)
        mock_get_provider.return_value = provider
        mock_get_client.return_value = AsyncMock(spec=httpx.AsyncClient)

        response = await client.get("/health?deep=true")
        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "degraded"
        assert data["provider"] == "searxng"
        assert data["search_backend"] == "unreachable"

    @patch("search_agent.main.get_http_client")
    @patch("search_agent.main.get_provider")
    async def test_deep_health_uninitialized_clients(
        self, mock_get_provider, mock_get_client, client
    ):
        provider = MagicMock()
        provider.name = "searxng"
        mock_get_provider.return_value = provider
        mock_get_client.side_effect = RuntimeError("Call init_shared_clients() first")

        response = await client.get("/health?deep=true")
        assert response.status_code == 503
        assert response.json()["status"] == "degraded"
