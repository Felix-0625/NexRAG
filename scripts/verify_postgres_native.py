"""针对一次性的本地合成数据库验证 PostgreSQL 连接器。

通过本地文件提供隔离回环测试服务器的管理员 DSN。脚本会创建名称唯一的
角色/数据库，绝不打开已有业务表；除非为单独的浏览器验收指定 --keep-fixture，
否则只删除这些自建 fixture。凭据不会进入公开报告。
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.midplatform.adapters import PostgreSQLAdapter, PostgreSQLSourceProfile, ViewSpec  # noqa: E402
from src.midplatform.readonly import ensure_postgres_reader  # noqa: E402
from src.midplatform.safety import UnsafeQueryError  # noqa: E402
from src.midplatform.workspace import discover  # noqa: E402


def assert_local_dsn(dsn: str) -> None:
    """拒绝命名 service 配置，且 host 与 hostaddr 必须是字面回环 IP，否则抛出 ValueError。"""
    values = conninfo_to_dict(dsn)
    if values.get("service"):
        raise ValueError("Named service profiles are not allowed for isolated verification")
    for host in (values.get("host", ""), values.get("hostaddr", values.get("host", ""))):
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise ValueError("The verifier only accepts literal loopback test-server addresses")


def fingerprint(connection) -> str:
    """返回合成 invoices 表全部行的 SHA256，用于确认数据源内容未被改动。"""
    rows = connection.execute(
        "SELECT id,company_id,amount_cents FROM public.invoices ORDER BY id"
    ).fetchall()
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def rejected(callable_, expected=(ValueError,)) -> bool:
    """调用 callable_，抛出 expected 中的异常返回 True，正常返回则为 False。"""
    try:
        callable_()
    except expected:
        return True
    return False


def profile(dsn: str) -> PostgreSQLSourceProfile:
    """构造只暴露 invoice_summary 视图的数据源配置，租户列取自 company_id。"""
    return PostgreSQLSourceProfile(
        dsn,
        "SELECT 1 FROM public.companies WHERE company_id=%s",
        {
            "invoice_summary": ViewSpec(
                {"id": "TEXT", "amount_cents": "INTEGER"},
                "SELECT id,amount_cents,company_id AS __tenant_id FROM public.invoices",
                ("id",),
                "invoice",
            )
        },
    )


def verify(admin_dsn: str, directory: Path, *, keep: bool) -> dict:
    """在回环 PostgreSQL 上创建唯一命名的合成库与角色，核查只读角色、租户隔离和快照行为并返回报告；除非 keep 且通过，结束时删除所建库与角色。"""
    assert_local_dsn(admin_dsn)
    directory.mkdir(parents=True, exist_ok=False)
    suffix = uuid4().hex[:12]
    database = f"nexrag_acceptance_{suffix}"
    roles = {kind: f"nexrag_{kind}_{suffix}" for kind in ("reader", "writer", "manager")}
    passwords = {kind: secrets.token_urlsafe(32) for kind in roles}
    admin_target = make_conninfo(admin_dsn, dbname=database)
    credentials = {
        kind: make_conninfo(admin_target, user=name, password=passwords[kind])
        for kind, name in roles.items()
    }
    created_roles, created_database, succeeded = [], False, False
    checks = {}
    try:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            server_version = admin.info.server_version
            for kind, name in roles.items():
                admin.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB "
                        "{} NOREPLICATION NOBYPASSRLS"
                    ).format(
                        sql.Identifier(name),
                        sql.Literal(passwords[kind]),
                        sql.SQL("CREATEROLE" if kind == "manager" else "NOCREATEROLE"),
                    )
                )
                created_roles.append(name)
            admin.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(database))
            )
            created_database = True
        with psycopg.connect(admin_target, autocommit=True) as owner:
            owner.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            owner.execute("CREATE TABLE public.companies(company_id text PRIMARY KEY)")
            owner.execute(
                "CREATE TABLE public.invoices(id text PRIMARY KEY, company_id text NOT NULL "
                "REFERENCES public.companies, amount_cents integer NOT NULL)"
            )
            owner.execute("INSERT INTO public.companies VALUES ('company-a'),('company-b')")
            owner.execute(
                "INSERT INTO public.invoices VALUES ('SYN-A-001','company-a',1000),"
                "('SYN-A-002','company-a',250),('SYN-B-001','company-b',9900)"
            )
            for kind, name in roles.items():
                owner.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                        sql.Identifier(database), sql.Identifier(name)
                    )
                )
                owner.execute(
                    sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(name))
                )
                owner.execute(
                    sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(
                        sql.Identifier(name)
                    )
                )
                if kind == "writer":
                    owner.execute(
                        sql.SQL("GRANT INSERT,UPDATE,DELETE ON public.invoices TO {}").format(
                            sql.Identifier(name)
                        )
                    )
            before = fingerprint(owner)
            with psycopg.connect(credentials["reader"]) as reader:
                ensure_postgres_reader(reader)
                checks["dedicated_reader_accepted"] = True
            for kind, dsn in {
                "superuser": admin_target,
                **{key: credentials[key] for key in ("writer", "manager")},
            }.items():
                with psycopg.connect(dsn) as candidate:
                    checks[f"{kind}_rejected"] = rejected(lambda: ensure_postgres_reader(candidate))
                    candidate.rollback()
            checks["reader_source_update_rejected"] = rejected(
                lambda: raw_update(credentials["reader"]), (psycopg.errors.InsufficientPrivilege,)
            )
            adapter = PostgreSQLAdapter(profile=profile(credentials["reader"]))
            total_sql = "SELECT SUM(amount_cents) AS total FROM invoice_summary"
            a = adapter.execute_query("SELECT * FROM invoice_summary ORDER BY id", "company-a")
            b = adapter.execute_query("SELECT * FROM invoice_summary ORDER BY id", "company-b")
            checks["tenant_a_exact_rows"] = a == [
                {"id": "SYN-A-001", "amount_cents": 1000},
                {"id": "SYN-A-002", "amount_cents": 250},
            ]
            checks["tenant_b_exact_rows"] = b == [{"id": "SYN-B-001", "amount_cents": 9900}]
            checks["tenant_totals"] = adapter.execute_query(total_sql, "company-a") == [
                {"total": 1250}
            ] and adapter.execute_query(total_sql, "company-b") == [{"total": 9900}]
            checks["other_tenant_predicate_cannot_expand"] = (
                adapter.execute_query(
                    "SELECT * FROM invoice_summary WHERE id='SYN-B-001'", "company-a"
                )
                == []
            )
            checks["source_table_not_exposed"] = rejected(
                lambda: adapter.execute_query("SELECT * FROM invoices", "company-a"),
                (UnsafeQueryError,),
            )
            for operation in (
                "UPDATE invoice_summary SET amount_cents=0",
                "DELETE FROM invoice_summary",
            ):
                checks[f"agent_{operation.split()[0].lower()}_rejected"] = rejected(
                    lambda operation=operation: adapter.execute_query(operation, "company-a"),
                    (UnsafeQueryError,),
                )
            checks["reader_revocation_effective_next_request"] = reader_revocation(
                owner, adapter, roles["reader"]
            )
            checks["source_logical_hash_unchanged"] = fingerprint(owner) == before
            # 显式修改数据源属主属于合成数据的准备步骤，不是 Agent 的写操作。
            with adapter.open_snapshot("company-a") as snapshot:
                checks["snapshot_initial_total"] = snapshot.execute_query(
                    total_sql, "company-a"
                ) == [{"total": 1250}]
                try:
                    owner.execute(
                        "UPDATE public.invoices SET amount_cents=1500 WHERE id='SYN-A-001'"
                    )
                    checks["same_snapshot_immutable_after_owner_update"] = snapshot.execute_query(
                        total_sql, "company-a"
                    ) == [{"total": 1250}]
                    checks["snapshot_cannot_switch_tenant"] = rejected(
                        lambda: snapshot.execute_query(total_sql, "company-b")
                    )
                    checks["source_owner_update_visible_next_request"] = adapter.execute_query(
                        total_sql, "company-a"
                    ) == [{"total": 1750}]
                finally:
                    owner.execute(
                        "UPDATE public.invoices SET amount_cents=1000 WHERE id='SYN-A-001'"
                    )
            checks["source_restored"] = fingerprint(owner) == before
            checks["connection_discovery_reader"] = {
                item["name"] for item in discover("postgresql", credentials["reader"])
            } == {"companies", "invoices"}
            checks["reader_discovers_actual_primary_keys"] = {
                item["name"]: item["primary_key"]
                for item in discover("postgresql", credentials["reader"])
            } == {"companies": ["company_id"], "invoices": ["id"]}
            checks["connection_discovery_writer_denied"] = rejected(
                lambda: discover("postgresql", credentials["writer"])
            )
        if not all(checks.values()):
            raise AssertionError(
                "Synthetic PostgreSQL checks failed: "
                + ", ".join(key for key, value in checks.items() if not value)
            )
        if keep:
            (directory / "connection-secrets.json").write_text(
                json.dumps({"admin": admin_target, **credentials}, indent=2), encoding="utf-8"
            )
        report = {
            "date": datetime.now(timezone.utc).isoformat(),
            "synthetic_only": True,
            "postgresql_server_version": server_version,
            "checks": checks,
            "source_logical_sha256": before,
            "passed": all(checks.values()),
            "fixture_retained_for_browser": keep,
            "limits": [
                "Not browser acceptance",
                "No production database accessed",
                "Immutable per-turn snapshot with next-turn freshness, not event streaming or CDC",
            ],
        }
        (directory / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        succeeded = True
        return report
    finally:
        if not keep or not succeeded:
            with psycopg.connect(admin_dsn, autocommit=True) as admin:
                if created_database:
                    admin.execute(
                        sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database))
                    )
                for name in created_roles:
                    admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))


def raw_update(dsn):
    """以给定凭据直接对 invoices 表执行 UPDATE，用来证明只读角色会被数据库拒绝。"""
    with psycopg.connect(dsn) as reader:
        reader.execute("UPDATE public.invoices SET amount_cents=0")


def reader_revocation(owner, adapter, role):
    """撤销读取角色对 invoices 的 SELECT 后，确认下一次查询因权限不足被拒绝，并在返回前恢复授权。"""
    owner.execute(sql.SQL("REVOKE SELECT ON public.invoices FROM {}").format(sql.Identifier(role)))
    try:
        return rejected(
            lambda: adapter.execute_query("SELECT * FROM invoice_summary", "company-a"),
            (psycopg.errors.InsufficientPrivilege,),
        )
    finally:
        owner.execute(sql.SQL("GRANT SELECT ON public.invoices TO {}").format(sql.Identifier(role)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-dsn-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--keep-fixture", action="store_true")
    args = parser.parse_args()
    report = verify(
        args.admin_dsn_file.read_text(encoding="utf-8").strip(),
        args.output_dir.resolve(),
        keep=args.keep_fixture,
    )
    print(json.dumps(report, indent=2))
