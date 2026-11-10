"""Company directory and explicit grant templates in operations storage.

Reporting lines never imply access. Templates copy grants; later edits do not expand
existing accounts without explicit administrator review and application.
"""

from __future__ import annotations

import json

from src.midplatform.data_policy import account_policy, ensure_policy_schema
from src.midplatform.ops_store import get_ops_store, now
from src.midplatform.tenant_access import list_grants, list_tenants, seed_from_yaml


def store():
    """Return operations storage after ensuring directory tables exist."""
    database = get_ops_store()
    with database.transaction() as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS company_departments (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, parent_id TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS company_accounts (
            user_id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
            department_id TEXT NOT NULL DEFAULT '', job_level TEXT NOT NULL DEFAULT '',
            manager_id TEXT NOT NULL DEFAULT '', updated_by TEXT NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS company_grant_templates (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, config TEXT NOT NULL
        );
        """)
    return database


def _valid_parent(identifier, parent, rows, key="parent_id"):
    mapping = {row["id"]: row[key] for row in rows}
    if parent and parent not in mapping:
        raise ValueError("上级组织或主管账号不存在")
    visited = {identifier}
    while parent:
        if parent in visited:
            raise ValueError("上下级关系不能成环")
        visited.add(parent)
        parent = mapping.get(parent, "")


def department(identifier, name, parent_id):
    """Save a department, rejecting missing parents and hierarchy cycles."""
    database = store()
    _valid_parent(identifier, parent_id, database.query("SELECT * FROM company_departments"))
    database.execute(
        "INSERT OR REPLACE INTO company_departments VALUES (?,?,?)", (identifier, name, parent_id)
    )


def validate_assignment(body):
    """Validate scopes, departments, and reporting lines before account assignment."""
    database = store()
    seed_from_yaml(database)
    tenants = list_tenants(database)
    if set(body["tenant_ids"]) - set(tenants):
        raise ValueError("授权中包含不存在的业务范围")
    if body["department_id"] and not database.query_one(
        "SELECT id FROM company_departments WHERE id=?", (body["department_id"],)
    ):
        raise ValueError("部门不存在")
    rows = [
        dict(id=row["user_id"], parent_id=row["manager_id"])
        for row in database.query("SELECT user_id,manager_id FROM company_accounts")
    ]
    _valid_parent(body.get("user_id", ""), body["manager_id"], rows)
    return database


def assign(body, changed_by):
    """Atomically assign account details and replace business and egress grants.

    Validate first; reject changed emails for existing user IDs rather than inheriting grants.
    """
    database = validate_assignment(body)
    ensure_policy_schema()
    # Identity is stable: email changes require explicit reconciliation
    # rather than inheriting another account's grants by name.
    previous = database.query_one(
        "SELECT email FROM company_accounts WHERE user_id=?", (body["user_id"],)
    )
    if previous and previous["email"] != body["email"].lower():
        raise ValueError("账号邮箱已变更，请先核对原账号授权后重新分发")
    email = body["email"].lower()
    with database.transaction() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO company_accounts VALUES (?,?,?,?,?,?,?,?)",
            (
                body["user_id"],
                email,
                body["name"],
                body["department_id"],
                body["job_level"],
                body["manager_id"],
                changed_by,
                now(),
            ),
        )
        connection.execute("DELETE FROM tenant_grants WHERE email=?", (email,))
        connection.executemany(
            "INSERT INTO tenant_grants VALUES (?,?,?,?)",
            [(email, tenant, changed_by, now()) for tenant in sorted(set(body["tenant_ids"]))],
        )
        connection.execute(
            "INSERT OR REPLACE INTO account_egress VALUES (?,?,?,?)",
            (email, json.dumps(body["egress"]), changed_by, now()),
        )


def overview():
    """Return accounts, grants, departments, templates, scopes, and grant relationships."""
    database = store()
    grants = list_grants(database)
    accounts = [
        dict(row, tenant_ids=grants.get(row["email"], []), egress=account_policy(row["email"]))
        for row in database.query(
            "SELECT * FROM company_accounts ORDER BY department_id, job_level, name"
        )
    ]
    templates = [
        dict(id=row["id"], name=row["name"], **json.loads(row["config"]))
        for row in database.query("SELECT * FROM company_grant_templates ORDER BY name")
    ]
    return dict(
        accounts=accounts,
        departments=database.query("SELECT * FROM company_departments ORDER BY name"),
        templates=templates,
        tenants=list_tenants(database),
        grants=grants,
    )


def save_template(identifier, name, config):
    """Save a template with known scopes without changing assigned accounts."""
    if set(config["tenant_ids"]) - set(list_tenants()):
        raise ValueError("模板中包含不存在的业务范围")
    store().execute(
        "INSERT OR REPLACE INTO company_grant_templates VALUES (?,?,?)",
        (identifier, name, json.dumps(config)),
    )
