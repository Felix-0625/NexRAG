"""粒度证明必须放行合法 SQL，并拒绝不安全的假设。"""

import hashlib
import sqlite3

import pytest

from src.agent.grounding import EvidenceLedger
from src.midplatform.adapters import SQLiteAdapter, SQLiteSourceProfile, ViewSpec
from src.midplatform.query_semantics import QuerySemanticsError, inspect_query


def view():
    return ViewSpec(
        {"order_id": "TEXT", "amount": "REAL"},
        "SELECT tenant_id AS __tenant_id, order_id, amount FROM orders",
        ("order_id",),
        "order",
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT order_id, amount FROM sales",
        "SELECT order_id, amount, SUM(amount) OVER () AS total_amount FROM sales",
        "SELECT order_id, amount, COUNT(*) OVER () AS order_count FROM sales",
    ],
)
def test_plain_and_window_queries_preserve_detail_rows(sql):
    assert inspect_query(sql).row_preserving


def test_grouping_is_not_a_blanket_ban_or_a_blanket_detail_proof():
    sql = "SELECT order_id, amount, SUM(amount) AS total FROM sales GROUP BY order_id"
    with pytest.raises(QuerySemanticsError):
        inspect_query(sql)
    shape = inspect_query(sql, {"sales": view()})
    assert shape.row_preserving and shape.grain == "order"
    all_columns = "SELECT order_id, amount, COUNT(*) FROM sales GROUP BY order_id, amount"
    assert not inspect_query(all_columns).row_preserving
    assert inspect_query(all_columns, {"sales": view()}).row_preserving
    assert inspect_query(sql.replace("sales", "SALES"), {"sales": view()}).row_preserving
    assert inspect_query(
        "SELECT DISTINCT order_id, amount FROM sales", {"sales": view()}
    ).row_preserving
    assert not inspect_query("SELECT DISTINCT order_id, amount FROM sales").row_preserving
    assert not inspect_query("SELECT SUM(amount) FROM sales").row_preserving


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT order_id, SUM(amount) FROM sales",
        "SELECT order_id, amount, COUNT(*) FROM sales GROUP BY amount",
        "SELECT * , SUM(amount) FROM sales",
    ],
)
def test_sqlite_bare_aggregate_columns_are_rejected(sql):
    with pytest.raises(QuerySemanticsError):
        inspect_query(sql)


def test_group_expression_alias_and_ordinal_remain_valid():
    for sql in [
        "SELECT LOWER(order_id) AS key, SUM(amount) FROM sales GROUP BY key",
        "SELECT LOWER(order_id), SUM(amount) FROM sales GROUP BY 1",
    ]:
        assert inspect_query(sql).aggregate


def test_unknown_join_and_subquery_grain_is_not_certified():
    for sql in [
        "SELECT a.order_id FROM sales a JOIN items b ON a.order_id=b.order_id",
        "SELECT order_id FROM (SELECT order_id FROM sales)",
    ]:
        shape = inspect_query(sql)
        assert not shape.row_preserving and shape.warning


def test_nested_window_aggregate_and_limited_details_do_not_prove_full_counts():
    assert not inspect_query("SELECT SUM(SUM(amount)) OVER () FROM sales").row_preserving
    assert inspect_query("SELECT order_id, amount FROM sales LIMIT 1").limited
    evidence = EvidenceLedger(required={"db", "db_count", "db_total"})
    evidence.add(
        "db",
        [{"order_id": "A-1", "amount": 1250}],
        sql="SELECT order_id, amount FROM sales LIMIT 1",
    )
    assert evidence.missing() == {"db_count", "db_total"}


def test_uncertified_cte_and_limited_rows_cannot_complete_full_business_request():
    for sql, missing in [
        ("WITH x AS (SELECT amount FROM sales) SELECT amount FROM x", {"db_details", "db_total"}),
        ("SELECT order_id,amount FROM sales LIMIT 1", {"db_details", "db_total"}),
    ]:
        evidence = EvidenceLedger(required={"db_details", "db_total"})
        evidence.add("db", [{"order_id": "A-1", "amount": 1250}], sql=sql)
        assert evidence.missing() == missing


def test_window_total_is_not_summed_again_in_answer_validation():
    evidence = EvidenceLedger(required={"db", "db_details", "db_total"})
    evidence.add(
        "db",
        [
            {"order_id": "A-1", "amount": 1250, "total_amount": 2000},
            {"order_id": "A-2", "amount": 750, "total_amount": 2000},
        ],
        sql="SELECT order_id, amount, SUM(amount) OVER () AS total_amount FROM sales",
    )
    assert not evidence.missing()
    assert not evidence.issues("总金额2000元。")
    assert evidence.issues("总金额4000元。")


def source(tmp_path, *, duplicate=False):
    path = tmp_path / "source.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE orders(tenant_id TEXT, order_id TEXT, amount REAL)")
        connection.executemany(
            "INSERT INTO orders VALUES(?,?,?)",
            [
                ("a", "A-1", 1250),
                ("a", "A-2", 750),
                ("b", "B-1", 9900),
            ],
        )
        if duplicate:
            connection.execute("INSERT INTO orders VALUES('a','A-1',1250)")
    adapter = SQLiteAdapter(profile=SQLiteSourceProfile(path, None, {"sales": view()}))
    return path, adapter


def test_enforced_snapshot_keys_tenant_scope_and_source_bytes(tmp_path):
    path, adapter = source(tmp_path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    rows = adapter.execute_query(
        "SELECT order_id, amount, SUM(amount) AS total FROM sales GROUP BY order_id", "a"
    )
    assert {row["order_id"] for row in rows} == {"A-1", "A-2"}
    for sql in [
        "UPDATE sales SET amount=0",
        "DELETE FROM sales",
        "DROP TABLE sales",
        "INSERT INTO sales VALUES('X',0)",
        "ATTACH DATABASE 'other.db' AS other",
    ]:
        with pytest.raises(ValueError):
            adapter.execute_query(sql, "a")
    with pytest.raises(QuerySemanticsError):
        adapter.execute_query("SELECT order_id, SUM(amount) FROM sales", "a")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_a_false_declared_key_fails_instead_of_silently_collapsing_rows(tmp_path):
    path, adapter = source(tmp_path, duplicate=True)
    before = path.read_bytes()
    with pytest.raises(sqlite3.IntegrityError):
        adapter.execute_query("SELECT order_id, SUM(amount) FROM sales GROUP BY order_id", "a")
    assert path.read_bytes() == before
