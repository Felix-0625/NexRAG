"""Authorize tenant views using signed Open WebUI identity.

Administrators manage tenants and grants in operations storage. TENANT_ACCESS_PATH seeds
fresh stores once, preserving YAML deployments and fixtures without manual migration.
"""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass, field
from pathlib import Path

import jwt
import yaml
from fastapi import HTTPException, Request, status

from src.config import settings
from src.midplatform.ops_store import OpsStore, get_ops_store, now


@dataclass(frozen=True)
class Principal:
    """Verified identity forwarded by the Open WebUI backend."""

    user_id: str
    email: str
    role: str
    knowledge_scopes: tuple[str, ...] = ()
    knowledge_labels: dict = field(default_factory=dict)
    session_version: int | None = None


def get_principal(request: Request) -> Principal:
    """Require a service key and separately signed Open WebUI JWT.

    Plain user headers and browser tenant IDs are not authentication. Open WebUI signs the
    JWT after validating its own login.
    """
    service_key = settings.NEXRAG_SHARED_API_KEY
    jwt_secret = settings.NEXRAG_USER_JWT_SECRET
    if not service_key or len(service_key) < 32 or not jwt_secret or len(jwt_secret) < 32:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "NexRAG identity is not configured"
        )

    authorization = request.headers.get("Authorization", "")
    supplied_key = (
        authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
    )
    if not hmac.compare_digest(supplied_key, service_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid service credential")

    token = request.headers.get(settings.NEXRAG_USER_JWT_HEADER)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing signed user identity")
    try:
        claims = jwt.decode(
            token,
            jwt_secret,
            algorithms=["HS256"],
            issuer="open-webui",
            options={"require": ["sub", "email", "role", "iat", "exp"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid signed user identity") from exc
    scopes = claims.get("knowledge_scopes", [])
    if (
        not isinstance(scopes, list)
        or len(scopes) > 530
        or any(
            not isinstance(scope, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", scope)
            for scope in scopes
        )
    ):
        raise HTTPException(401, "Invalid knowledge authorization")
    labels = claims.get("knowledge_labels", {})
    if not isinstance(labels, dict) or set(labels) - set(scopes):
        raise HTTPException(401, "Invalid knowledge classification")
    for label in labels.values():
        if (
            not isinstance(label, dict)
            or label.get("classification") not in {"public", "internal", "secret"}
            or any(
                not isinstance(label.get(key, False), bool)
                for key in ("external_models", "external_search", "external_tools")
            )
        ):
            raise HTTPException(401, "Invalid knowledge classification")
    session_version = claims.get("session_version")
    if session_version is not None and (type(session_version) is not int or session_version < 0):
        raise HTTPException(401, "Invalid session generation")
    return Principal(
        str(claims["sub"]),
        str(claims["email"]).lower(),
        str(claims["role"]),
        tuple(scopes),
        labels,
        session_version,
    )


def seed_from_yaml(store: OpsStore | None = None, path: str | Path | None = None) -> int:
    """Seed tenants and grants only while the tables remain empty.

    Run once per fresh store. Management then owns the data and future calls are no-ops.
    """
    store = store or get_ops_store()
    if store.query_one("SELECT 1 FROM tenants LIMIT 1"):
        return 0
    policy_path = Path(path or settings.TENANT_ACCESS_PATH)
    if not policy_path.is_file():
        return 0
    policy = yaml.safe_load(policy_path.read_text(encoding="utf-8")) or {}
    tenants = policy.get("tenants") or {}
    if not isinstance(tenants, dict):
        return 0
    at = now()
    added = 0
    with store.transaction() as conn:
        for tenant_id, label in tenants.items():
            conn.execute(
                "INSERT OR IGNORE INTO tenants VALUES (?,?,?)", (str(tenant_id), str(label), at)
            )
            added += 1
        for email, grants in (policy.get("users") or {}).items():
            for tenant_id in grants or []:
                conn.execute(
                    "INSERT OR IGNORE INTO tenant_grants VALUES (?,?,?,?)",
                    (str(email).lower(), str(tenant_id), "seed:tenant-access.yaml", at),
                )
    return added


def list_tenants(store: OpsStore | None = None) -> dict[str, str]:
    """Return the complete administrator-visible tenant catalog."""
    store = store or get_ops_store()
    seed_from_yaml(store)
    return {
        row["tenant_id"]: row["label"]
        for row in store.query("SELECT * FROM tenants ORDER BY label")
    }


def allowed_tenants(principal: Principal, store: OpsStore | None = None) -> dict[str, str]:
    """Return only tenants assigned to the verified user."""
    store = store or get_ops_store()
    tenants = list_tenants(store)
    if not tenants:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Tenant policy is unavailable")
    if principal.role == "admin":
        return tenants
    rows = store.query("SELECT tenant_id FROM tenant_grants WHERE email = ?", (principal.email,))
    return {
        row["tenant_id"]: tenants[row["tenant_id"]] for row in rows if row["tenant_id"] in tenants
    }


def create_tenant(tenant_id: str, label: str, store: OpsStore | None = None) -> None:
    """Create a tenant, rejecting duplicate IDs."""
    store = store or get_ops_store()
    seed_from_yaml(store)
    if store.query_one("SELECT 1 FROM tenants WHERE tenant_id = ?", (tenant_id,)):
        raise ValueError(f"Tenant {tenant_id!r} already exists")
    store.execute("INSERT INTO tenants VALUES (?,?,?)", (tenant_id, label, now()))


def delete_tenant(tenant_id: str, store: OpsStore | None = None) -> None:
    """Atomically delete a tenant and its grants; ignore missing tenants."""
    store = store or get_ops_store()
    with store.transaction() as conn:
        conn.execute("DELETE FROM tenant_grants WHERE tenant_id = ?", (tenant_id,))
        conn.execute("DELETE FROM tenants WHERE tenant_id = ?", (tenant_id,))


def set_grant(
    email: str, tenant_id: str, granted: bool, granted_by: str, store: OpsStore | None = None
) -> None:
    """Grant or revoke one user's access to one tenant."""
    store = store or get_ops_store()
    seed_from_yaml(store)
    email = email.lower()
    if granted:
        if not store.query_one("SELECT 1 FROM tenants WHERE tenant_id = ?", (tenant_id,)):
            raise ValueError(f"Unknown tenant {tenant_id!r}")
        store.execute(
            "INSERT OR IGNORE INTO tenant_grants VALUES (?,?,?,?)",
            (email, tenant_id, granted_by, now()),
        )
    else:
        store.execute(
            "DELETE FROM tenant_grants WHERE email = ? AND tenant_id = ?", (email, tenant_id)
        )


def list_grants(store: OpsStore | None = None) -> dict[str, list[str]]:
    """Return all tenant grants keyed by email."""
    store = store or get_ops_store()
    seed_from_yaml(store)
    grants: dict[str, list[str]] = {}
    for row in store.query("SELECT email, tenant_id FROM tenant_grants ORDER BY email"):
        grants.setdefault(row["email"], []).append(row["tenant_id"])
    return grants
