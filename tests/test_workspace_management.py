import sqlite3
import time

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from src.agent.exports import export_analysis, list_exports
from src.agent.tools import business_schema, search_knowledge
from src.config import settings
from src.main import app
from src.midplatform import workspace
from src.midplatform.adapters import create_business_adapter
from src.midplatform.tenant_access import Principal, allowed_tenants, create_tenant, set_grant


@pytest.fixture
def api(monkeypatch):
    """Provide an isolated test API client."""
    monkeypatch.setattr(settings, "NEXRAG_SHARED_API_KEY", "k" * 32)
    monkeypatch.setattr(settings, "NEXRAG_USER_JWT_SECRET", "s" * 32)
    return TestClient(app)


def headers(role="admin", scopes=None):
    """Build signed request headers for the test identity."""
    claims = {
        "sub": "workspace-test",
        "email": "workspace@example.local",
        "role": role,
        "iss": "open-webui",
        "iat": int(time.time()),
        "exp": int(time.time()) + 60,
    }
    if scopes is not None:
        claims["knowledge_scopes"] = scopes
    return {
        "Authorization": "Bearer " + "k" * 32,
        "X-OpenWebUI-User-Jwt": jwt.encode(claims, "s" * 32, algorithm="HS256"),
    }


def test_only_admin_can_connect_database_or_bind_machine_folder(api, tmp_path):
    folder = tmp_path / "not-created"
    response = api.post(
        "/v1/workspace/exports-folder", json={"path": str(folder)}, headers=headers("user")
    )
    assert response.status_code == 403
    assert not folder.exists()
    response = api.post(
        "/v1/workspace/connections/discover",
        json={"kind": "sqlite", "address": str(tmp_path / "private.db")},
        headers=headers("user"),
    )
    assert response.status_code == 403


def test_missing_sqlite_connection_never_creates_database(api, tmp_path):
    path = tmp_path / "missing.db"
    response = api.post(
        "/v1/workspace/connections/discover",
        json={"kind": "sqlite", "address": str(path)},
        headers=headers(),
    )
    assert response.status_code == 400
    assert not path.exists()


def test_wrong_database_role_has_safe_actionable_guidance(api, monkeypatch):
    from src.midplatform.readonly import ReadOnlyRoleRequired

    def denied(*args):
        """Provide denied test data or behavior for this scenario."""
        raise ReadOnlyRoleRequired("业务账号仍有写入或建表权限，请由数据库管理员提供专用只读账号")

    monkeypatch.setattr(workspace, "discover", denied)
    response = api.post(
        "/v1/workspace/connections/discover",
        json={"kind": "postgresql", "address": "synthetic-private-dsn"},
        headers=headers(),
    )
    assert response.status_code == 400
    assert "专用只读账号" in response.json()["detail"]
    assert "synthetic-private-dsn" not in response.text


def test_driver_failure_does_not_expose_dsn_or_password(api, monkeypatch):
    def failed(*args):
        """Provide failed test data or behavior for this scenario."""
        raise RuntimeError("Driver failure: password=synthetic-secret")

    monkeypatch.setattr(workspace, "discover", failed)
    response = api.post(
        "/v1/workspace/connections/discover",
        json={"kind": "postgresql", "address": "synthetic-private-dsn"},
        headers=headers(),
    )
    assert response.status_code == 400
    assert "synthetic-secret" not in response.text
    assert "Driver" not in response.text


def test_switching_business_source_requires_fresh_grants_and_policy(api, isolated_ops):
    from src.midplatform.data_policy import resource_policy, save_resource_policy

    create_tenant("sales", "Sales")
    set_grant("employee@local", "sales", True, "admin")
    save_resource_policy("tenant", "sales", "public", True, True, True, "admin")
    workspace.set_setting("active_connection", "old-source")
    response = api.post("/v1/workspace/connections/default/activate", headers=headers())
    assert response.status_code == 200 and response.json()["grants_reset"]
    assert allowed_tenants(Principal("employee", "employee@local", "user")) == {}
    assert resource_policy("tenant", "sales")["classification"] == "internal"
    assert workspace.get_setting("business_source_generation") == 1
    # Idempotent UI retries must not revoke grants assigned after activation.
    set_grant("employee@local", "sales", True, "admin")
    assert not api.post("/v1/workspace/connections/default/activate", headers=headers()).json()[
        "grants_reset"
    ]
    assert allowed_tenants(Principal("employee", "employee@local", "user")) == {"sales": "Sales"}


def test_connection_uses_real_tenant_isolation_and_matching_metadata(api, tmp_path, monkeypatch):
    path = tmp_path / "business.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sales (tenant_id TEXT, amount INTEGER)")
        conn.executemany("INSERT INTO sales VALUES (?,?)", [("A", 10), ("B", 90)])
    monkeypatch.setattr(settings, "OPS_DB_PATH", str(tmp_path / "ops.db"))
    response = api.post(
        "/v1/workspace/connections",
        headers=headers(),
        json={
            "name": "Sales",
            "kind": "sqlite",
            "address": str(path),
            "tables": [{"name": "sales", "tenant_column": "tenant_id"}],
        },
    )
    assert response.status_code == 200, response.text
    identifier = response.json()["id"]
    assert (
        api.post(f"/v1/workspace/connections/{identifier}/activate", headers=headers()).status_code
        == 200
    )
    assert create_business_adapter().execute_query("SELECT amount FROM sales", "A") == [
        {"amount": 10}
    ]
    assert create_business_adapter().execute_query("SELECT amount FROM sales", "B") == [
        {"amount": 90}
    ]
    assert "sales" in business_schema(settings.METADATA_PATH)
    public = api.get("/v1/workspace/connections", headers=headers()).json()
    assert str(path) not in str(public)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 2


def test_bound_folder_is_used_by_export_writer_and_reader(api, tmp_path):
    folder = tmp_path / "artifacts"
    response = api.post(
        "/v1/workspace/exports-folder", headers=headers(), json={"path": str(folder)}
    )
    assert response.status_code == 200
    result = export_analysis(user_id="u1", chat_id="c1", title="Report", text="analysis")
    assert (folder / result["relative_path"]).is_file()
    assert len(list_exports("u1")) == 1
    assert list_exports("u2") == []
    assert workspace.export_root() == folder


@pytest.mark.asyncio
async def test_no_knowledge_grant_does_not_query_shared_or_other_users_collections():
    called = []

    async def handler(request):
        """Provide handler test data or behavior for this scenario."""
        called.append(request)
        raise AssertionError("No retrieval call is allowed")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert (
            await search_knowledge(
                "secret",
                base_url="http://retrieval",
                api_key="k",
                collection_name=[],
                client=client,
            )
            == []
        )
    assert called == []


def test_invalid_signed_knowledge_scope_is_rejected(api):
    response = api.get("/v1/exports", headers=headers(scopes=["../another-user"]))
    assert response.status_code == 401


def test_credentials_are_not_reflected_in_connection_errors(api):
    password = "must-not-be-reflected"
    response = api.post(
        "/v1/workspace/connections/discover",
        headers=headers(),
        json={"kind": "postgresql", "address": f"postgresql://user:{password}@127.0.0.1:1/test"},
    )
    assert response.status_code == 400
    assert password not in response.text


def test_connection_retains_real_composite_key_not_client_claim(api, tmp_path):
    import json

    from src.midplatform.adapters import load_source_profile

    path = tmp_path / "keyed.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE orders(tenant TEXT,id TEXT,amount REAL,PRIMARY KEY(tenant,id))")
        conn.execute("INSERT INTO orders VALUES('a','O1',100)")
    structure = workspace.discover("sqlite", str(path))
    assert structure[0]["primary_key"] == ["tenant", "id"]
    saved = workspace.save_connection(
        "Keyed",
        "sqlite",
        str(path),
        [{"name": "orders", "tenant_column": "tenant", "primary_key": ["amount"]}],
    )
    config = json.loads(
        workspace.get_ops_store().query_one(
            "SELECT config FROM business_connections WHERE id=?", (saved["id"],)
        )["config"]
    )
    view = load_source_profile(config["source_path"]).views["orders"]
    assert view.primary_key == ("tenant", "id")
    assert view.grain == "orders"
