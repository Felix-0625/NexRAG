"""验证克隆后的 NexRAG 能查询另一套按租户划分的业务库结构。"""

import sqlite3
import time
from contextlib import closing

import jwt
import yaml
from fastapi.testclient import TestClient

from src.agent.tools import business_schema
from src.config import settings
from src.main import app
from src.midplatform.adapters import SQLiteAdapter
from src.midplatform.safety import UnsafeQueryError


def test_custom_database_profile_and_http_permission(tmp_path, monkeypatch):
    database = tmp_path / "company.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(
            """
            CREATE TABLE invoices (id TEXT, company_id TEXT, amount INTEGER);
            INSERT INTO invoices VALUES ('invoice-a', 'company-a', 1250);
            INSERT INTO invoices VALUES ('invoice-b', 'company-b', 9900);
            """
        )

    profile = tmp_path / "business-source.yaml"
    profile.write_text(
        yaml.safe_dump(
            {
                "type": "sqlite",
                "database_path": "company.db",
                "views": {
                    "invoice_summary": {
                        "columns": {"id": "TEXT", "amount": "INTEGER"},
                        "source_sql": (
                            "SELECT id, amount, company_id AS __tenant_id FROM invoices"
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
    metadata = tmp_path / "metadata.yaml"
    metadata.write_text(
        yaml.safe_dump(
            {
                "tables": {
                    "invoice_summary": {
                        "description": "Tenant invoice amounts",
                        "columns": {
                            "id": {"type": "TEXT", "description": "Invoice ID"},
                            "amount": {"type": "INTEGER", "description": "Amount in cents"},
                        },
                    }
                },
                "metrics": {
                    "revenue": {
                        "calculation": "SUM(invoice_summary.amount) / 100.0",
                        "description": "Tenant invoice revenue",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "BUSINESS_SOURCE_PATH", str(profile))
    monkeypatch.setattr(settings, "BUSINESS_DB_PATH", None)
    monkeypatch.setattr(settings, "METADATA_PATH", str(metadata))
    monkeypatch.setattr(settings, "TENANT_ACCESS_PATH", str(policy))
    monkeypatch.setattr(settings, "NEXRAG_SHARED_API_KEY", "test-service-key-with-32-characters")
    monkeypatch.setattr(settings, "NEXRAG_USER_JWT_SECRET", "b" * 32)

    schema = business_schema(settings.METADATA_PATH)
    assert "invoice_summary" in schema
    assert "seller_items" not in schema

    adapter = SQLiteAdapter(profile_path=profile)
    assert adapter.execute_query(
        "SELECT SUM(amount) AS total FROM invoice_summary", "company-a"
    ) == [{"total": 1250}]
    try:
        adapter.execute_query("SELECT * FROM invoices", "company-a")
    except UnsafeQueryError:
        pass
    else:
        raise AssertionError("A source table escaped the configured business view")

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
    assert client.get("/v1/tenants", headers=headers).json()["data"] == [
        {"tenant_id": "company-a", "label": "Company A"}
    ]
    own = client.post(
        "/v1/business/query",
        headers=headers,
        json={"tenant_id": "company-a", "sql": "SELECT SUM(amount) AS total FROM invoice_summary"},
    )
    other = client.post(
        "/v1/business/query",
        headers=headers,
        json={"tenant_id": "company-b", "sql": "SELECT SUM(amount) AS total FROM invoice_summary"},
    )
    assert own.status_code == 200
    assert own.json()["rows"] == [{"total": 1250}]
    assert other.status_code == 403
