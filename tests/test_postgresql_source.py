"""Exercise real PostgreSQL only with a dedicated test DSN.

Require companies(company_id) and invoices(id, company_id, amount_cents) with company-a
and company-b fixtures. Setup is documented in docs/connect-business-database.md.
"""

import os
import time

import jwt
import pytest
import yaml
from fastapi.testclient import TestClient

from src.config import settings
from src.main import app
from src.midplatform.adapters import PostgreSQLAdapter, create_business_adapter
from src.midplatform.safety import UnsafeQueryError


def test_postgresql_tenant_scope_and_http_authorization(tmp_path, monkeypatch):
    dsn = os.getenv("NEXRAG_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set NEXRAG_TEST_POSTGRES_DSN to run against a dedicated fixture database")

    profile = tmp_path / "business-source.yaml"
    profile.write_text(
        yaml.safe_dump(
            {
                "type": "postgresql",
                "tenant_exists_sql": "SELECT 1 FROM public.companies WHERE company_id = %s",
                "views": {
                    "invoice_summary": {
                        "columns": {"id": "TEXT", "amount_cents": "INTEGER"},
                        "source_sql": (
                            "SELECT id, amount_cents, company_id AS __tenant_id FROM public.invoices"
                        ),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    policy = tmp_path / "tenant-access.yaml"
    policy.write_text(
        yaml.safe_dump(
            {
                "tenants": {"company-a": "Company A", "company-b": "Company B"},
                "users": {"viewer@example.local": ["company-a"]},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "BUSINESS_SOURCE_PATH", str(profile))
    monkeypatch.setattr(settings, "BUSINESS_DB_DSN", dsn)
    monkeypatch.setattr(settings, "TENANT_ACCESS_PATH", str(policy))
    monkeypatch.setattr(settings, "NEXRAG_SHARED_API_KEY", "test-service-key-with-32-characters")
    monkeypatch.setattr(settings, "NEXRAG_USER_JWT_SECRET", "b" * 32)

    adapter = create_business_adapter()
    assert isinstance(adapter, PostgreSQLAdapter)
    sql = "SELECT SUM(amount_cents) AS total FROM invoice_summary"
    assert adapter.execute_query(sql, "company-a") == [{"total": 1250}]
    assert adapter.execute_query(sql, "company-b") == [{"total": 9900}]
    with pytest.raises(UnsafeQueryError):
        adapter.execute_query("SELECT * FROM invoices", "company-a")

    now = int(time.time())
    identity = jwt.encode(
        {
            "sub": "viewer-1",
            "email": "viewer@example.local",
            "role": "user",
            "iss": "open-webui",
            "iat": now,
            "exp": now + 300,
        },
        settings.NEXRAG_USER_JWT_SECRET,
        algorithm="HS256",
    )
    headers = {
        "Authorization": "Bearer test-service-key-with-32-characters",
        "X-OpenWebUI-User-Jwt": identity,
    }
    client = TestClient(app)
    own = client.post(
        "/v1/business/query",
        headers=headers,
        json={"tenant_id": "company-a", "sql": sql},
    )
    other = client.post(
        "/v1/business/query",
        headers=headers,
        json={"tenant_id": "company-b", "sql": sql},
    )
    assert own.status_code == 200
    assert own.json()["rows"] == [{"total": 1250}]
    assert other.status_code == 403
