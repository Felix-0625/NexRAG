"""恶意分析查询之后，真实数据源的哈希必须保持不变。"""

import hashlib
import sqlite3
from unittest.mock import Mock

import pytest

from src.midplatform.adapters import SQLiteAdapter, SQLiteSourceProfile, ViewSpec
from src.midplatform.readonly import ensure_postgres_reader


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE sales SET amount=1",
        "DELETE FROM sales",
        "INSERT INTO sales VALUES ('x', 1)",
        "DROP TABLE sales",
        "SELECT * INTO stolen FROM sales",
        "ATTACH DATABASE 'evil.db' AS evil",
        "PRAGMA query_only=OFF",
        "SELECT load_extension('evil')",
        "SELECT writefile('evil', 'x')",
        "SELECT * FROM sales; DELETE FROM sales",
        "SELECT * FROM sales FOR UPDATE",
    ],
)
def test_business_source_stays_readonly(tmp_path, sql):
    path = tmp_path / "business.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sales (tenant_id TEXT, order_id TEXT, amount REAL)")
        conn.executemany(
            "INSERT INTO sales VALUES (?,?,?)", [("b", "B-001", 9000), ("a", "A-001", 1250)]
        )
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    profile = SQLiteSourceProfile(
        path,
        None,
        {
            "sales": ViewSpec(
                {"order_id": "TEXT", "amount": "REAL"},
                "SELECT tenant_id AS __tenant_id, order_id, amount FROM sales",
            )
        },
    )
    adapter = SQLiteAdapter(profile=profile)
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        adapter.execute_query(sql, "b")
    assert adapter.execute_query("SELECT * FROM sales", "b") == [
        {"order_id": "B-001", "amount": 9000.0}
    ]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("admin,writes", [(True, False), (False, True), (True, True)])
def test_postgresql_privileged_credentials_rejected(admin, writes):
    connection = Mock()
    connection.execute.side_effect = [
        Mock(fetchone=lambda: (admin,)),
        Mock(fetchone=lambda: (writes,)),
    ]
    with pytest.raises(ValueError, match="只读"):
        ensure_postgres_reader(connection)


def test_postgresql_reader_checked_without_grant_mutation():
    connection = Mock()
    connection.execute.side_effect = [
        Mock(fetchone=lambda: (False,)),
        Mock(fetchone=lambda: (False,)),
    ]
    ensure_postgres_reader(connection)
    assert all(call.args[0].startswith("SELECT") for call in connection.execute.call_args_list)
