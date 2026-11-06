"""公司通讯录与显式授权模板，存放在现有 ops 数据库中。

汇报关系只是组织信息，绝不隐含数据权限。应用模板会复制其授权；之后修改模板
不会悄悄扩大已有账号的权限，管理员需显式审阅并应用变更。
"""

from __future__ import annotations

import json

from src.midplatform.data_policy import account_policy, ensure_policy_schema
from src.midplatform.ops_store import get_ops_store, now
from src.midplatform.tenant_access import list_grants, list_tenants, seed_from_yaml


def store():
    """返回 ops 数据库，并确保通讯录相关表（部门、账号、授权模板）已创建。"""
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
    """新增或覆盖一个部门；上级部门不存在或上下级成环时抛出 ValueError。"""
    database = store()
    _valid_parent(identifier, parent_id, database.query("SELECT * FROM company_departments"))
    database.execute(
        "INSERT OR REPLACE INTO company_departments VALUES (?,?,?)", (identifier, name, parent_id)
    )


def validate_assignment(body):
    """校验账号分配请求：业务范围必须存在、部门必须存在、主管关系不能成环；不合法抛出 ValueError，通过则返回数据库。"""
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
    """在同一事务中写入账号，并整体替换该邮箱的业务范围授权与外发策略。

    先做 validate_assignment 校验；同一 user_id 的邮箱若与已有记录不同则拒绝（ValueError）。
    """
    database = validate_assignment(body)
    ensure_policy_schema()
    # 身份是稳定的：别处改了邮箱必须显式对账，
    # 而不是按名字继承他人原有的授权。
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
    """返回通讯录总览：账号（含授权与外发策略）、部门、授权模板、业务范围及授权关系。"""
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
    """新增或覆盖授权模板；包含不存在的业务范围时抛出 ValueError。不影响已分配的账号。"""
    if set(config["tenant_ids"]) - set(list_tenants()):
        raise ValueError("模板中包含不存在的业务范围")
    store().execute(
        "INSERT OR REPLACE INTO company_grant_templates VALUES (?,?,?)",
        (identifier, name, json.dumps(config)),
    )
