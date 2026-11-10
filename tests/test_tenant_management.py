"""Administrator tenant catalogs and grants through YAML seeds and HTTP CRUD."""

from __future__ import annotations

import time

import jwt
import pytest
from fastapi.testclient import TestClient

from src.config import settings
from src.main import app
from src.midplatform import tenant_access
from src.midplatform.tenant_access import Principal

KEY = "test-service-key-with-32-characters"
SECRET = "a" * 32


@pytest.fixture
def seeded_yaml(tmp_path, monkeypatch):
    """Provide seeded yaml test data or behavior for this scenario."""
    policy = tmp_path / "tenant-access.yaml"
    policy.write_text(
        "tenants:\n  t-a: Seller A\n  t-b: Seller B\nusers:\n  viewer@example.local:\n    - t-a\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "TENANT_ACCESS_PATH", str(policy))
    return policy


def test_seed_from_yaml_runs_once(isolated_ops, seeded_yaml):
    assert tenant_access.seed_from_yaml() == 2
    assert tenant_access.seed_from_yaml() == 0  # Existing table contents make seeding a no-op.
    assert tenant_access.list_tenants() == {"t-a": "Seller A", "t-b": "Seller B"}
    assert tenant_access.list_grants() == {"viewer@example.local": ["t-a"]}


def test_allowed_tenants_matches_seeded_grants(isolated_ops, seeded_yaml):
    viewer = Principal("u1", "viewer@example.local", "user")
    admin = Principal("u2", "admin@example.local", "admin")
    assert tenant_access.allowed_tenants(viewer) == {"t-a": "Seller A"}
    assert tenant_access.allowed_tenants(admin) == {"t-a": "Seller A", "t-b": "Seller B"}


def test_create_delete_tenant_and_grant_revoke(isolated_ops):
    tenant_access.create_tenant("t-x", "Tenant X")
    with pytest.raises(ValueError, match="already exists"):
        tenant_access.create_tenant("t-x", "dup")

    tenant_access.set_grant("A@Example.com", "t-x", True, "admin@example.local")
    assert tenant_access.allowed_tenants(Principal("u", "a@example.com", "user")) == {
        "t-x": "Tenant X"
    }

    with pytest.raises(ValueError, match="Unknown tenant"):
        tenant_access.set_grant("a@example.com", "missing", True, "admin@example.local")

    tenant_access.set_grant("a@example.com", "t-x", False, "")
    assert tenant_access.allowed_tenants(Principal("u", "a@example.com", "user")) == {}

    tenant_access.delete_tenant("t-x")
    assert tenant_access.list_tenants() == {}


def test_revoking_a_tenant_also_drops_its_grants(isolated_ops):
    tenant_access.create_tenant("t-x", "Tenant X")
    tenant_access.set_grant("a@example.com", "t-x", True, "admin@example.local")
    tenant_access.delete_tenant("t-x")
    assert tenant_access.list_grants() == {}


def headers(role="admin"):
    """Build signed request headers for the test identity."""
    claims = {
        "sub": "u1",
        "email": "admin@example.local",
        "role": role,
        "iss": "open-webui",
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
    }
    return {
        "Authorization": f"Bearer {KEY}",
        "X-OpenWebUI-User-Jwt": jwt.encode(claims, SECRET, algorithm="HS256"),
    }


@pytest.fixture
def client(monkeypatch):
    """Provide an isolated HTTP client for the test application."""
    monkeypatch.setattr(settings, "NEXRAG_SHARED_API_KEY", KEY)
    monkeypatch.setattr(settings, "NEXRAG_USER_JWT_SECRET", SECRET)
    return TestClient(app)


def test_tenant_admin_api_round_trip(client, isolated_ops):
    assert (
        client.post(
            "/v1/ops/tenants", json={"tenant_id": "t-a", "label": "A"}, headers=headers(role="user")
        ).status_code
        == 403
    )

    created = client.post(
        "/v1/ops/tenants", json={"tenant_id": "t-a", "label": "A"}, headers=headers()
    )
    assert created.status_code == 200
    dup = client.post("/v1/ops/tenants", json={"tenant_id": "t-a", "label": "A"}, headers=headers())
    assert dup.status_code == 409

    granted = client.post(
        "/v1/ops/tenants/grant", json={"email": "u@x.com", "tenant_id": "t-a"}, headers=headers()
    )
    assert granted.status_code == 200

    listed = client.get("/v1/ops/tenants", headers=headers()).json()
    assert listed == {"tenants": {"t-a": "A"}, "grants": {"u@x.com": ["t-a"]}}

    client.post(
        "/v1/ops/tenants/revoke", json={"email": "u@x.com", "tenant_id": "t-a"}, headers=headers()
    )
    assert client.get("/v1/ops/tenants", headers=headers()).json()["grants"] == {}

    client.delete("/v1/ops/tenants/t-a", headers=headers())
    assert client.get("/v1/ops/tenants", headers=headers()).json()["tenants"] == {}
