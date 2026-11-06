"""基于真实只读租户快照的典型扇出（fanout）示例。"""

import sqlite3

import pytest

from src.agent.grounding import EvidenceLedger
from src.midplatform.adapters import SQLiteAdapter, SQLiteSourceProfile, ViewSpec
from src.midplatform.query_semantics import QuerySemanticsError, inspect_query


@pytest.fixture
def adapter(tmp_path):
    path = tmp_path / "source.db"
    with sqlite3.connect(path) as owner:
        owner.executescript(
            "CREATE TABLE orders(tenant TEXT,id TEXT,amount REAL); CREATE TABLE items(tenant TEXT,item_id TEXT,order_id TEXT,price REAL); INSERT INTO orders VALUES('a','O1',100),('a','O2',200),('b','X1',9999); INSERT INTO items VALUES('a','I1','O1',40),('a','I2','O1',60),('a','I3','O2',200),('b','J1','X1',9999);"
        )
    return SQLiteAdapter(
        profile=SQLiteSourceProfile(
            path,
            None,
            {
                "orders": ViewSpec(
                    {"id": "TEXT", "amount": "REAL"},
                    "SELECT tenant AS __tenant_id,id,amount FROM orders",
                    ("id",),
                    "order",
                ),
                "items": ViewSpec(
                    {"item_id": "TEXT", "order_id": "TEXT", "price": "REAL"},
                    "SELECT tenant AS __tenant_id,item_id,order_id,price FROM items",
                    ("item_id",),
                    "order_item",
                ),
            },
        )
    )


def test_legal_fact_to_lookup_join_keeps_item_amount_and_distinct_order_count(adapter):
    sql = "SELECT SUM(i.price) AS total_amount, COUNT(DISTINCT o.id) AS order_count FROM items i JOIN orders o ON i.order_id=o.id"
    assert adapter.execute_query(sql, "a") == [{"total_amount": 300, "order_count": 2}]
    shape = inspect_query(
        "SELECT i.item_id,i.price,o.amount FROM items i LEFT JOIN orders o ON i.order_id=o.id",
        adapter.profile.views,
    )
    assert shape.row_preserving and shape.grain == "order_item"
    assert shape.join_cardinality == "fact_to_unique_lookup"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT SUM(o.amount) FROM orders o JOIN items i ON o.id=i.order_id",
        "SELECT SUM(o.amount) FROM items i JOIN orders o ON i.order_id=o.id",
        "SELECT SUM(DISTINCT o.amount) FROM items i JOIN orders o ON i.order_id=o.id",
        "SELECT TOTAL(o.amount) FROM items i JOIN orders o ON i.order_id=o.id",
        "SELECT AVG(o.amount) FROM items i JOIN orders o ON i.order_id=o.id",
        "SELECT COUNT(o.id) FROM items i JOIN orders o ON i.order_id=o.id",
        "SELECT SUM(o.amount) OVER () FROM items i JOIN orders o ON i.order_id=o.id",
        "SELECT SUM(i.price) FROM items i CROSS JOIN orders o",
        "WITH inflated AS (SELECT SUM(o.amount) total FROM orders o JOIN items i ON o.id=i.order_id) SELECT total FROM inflated",
    ],
)
def test_unsafe_fanout_is_refused_before_it_can_become_evidence(adapter, sql):
    with pytest.raises(QuerySemanticsError):
        adapter.execute_query(sql, "a")


def test_key_grouping_is_bound_to_root_alias_not_same_named_other_column(adapter):
    assert adapter.execute_query(
        "SELECT i.item_id,i.price,o.amount,SUM(i.price) total FROM items i JOIN orders o ON i.order_id=o.id GROUP BY i.item_id",
        "a",
    )
    with pytest.raises(QuerySemanticsError):
        adapter.execute_query(
            "SELECT i.item_id,i.price,SUM(i.price) total FROM items i JOIN orders o ON i.order_id=o.id GROUP BY o.id",
            "a",
        )


def test_lookup_projection_cannot_be_summed_or_counted_as_orders(adapter):
    sql = "SELECT i.item_id,i.price,o.amount FROM items i JOIN orders o ON i.order_id=o.id"
    ledger = EvidenceLedger(required={"db_count", "db_total"})
    ledger.add(
        "db",
        adapter.execute_query(sql, "a"),
        sql=sql,
        query_semantics=inspect_query(sql, adapter.profile.views).as_dict(),
    )
    counts, sums = ledger._stats()
    assert not counts  # 三条明细行不能证明有三个订单。
    assert sums == {300}  # 重复的查找金额合计为 400：永远不被接受。
    assert ledger.missing() == {"db_count"}
    assert ledger.issues("总金额400元。")


def test_non_sum_aggregate_alias_cannot_masquerade_as_total(adapter):
    sql = "SELECT AVG(amount) total_amount FROM orders"
    ledger = EvidenceLedger(required={"db_total"})
    ledger.add(
        "db",
        adapter.execute_query(sql, "a"),
        sql=sql,
        query_semantics=inspect_query(sql, adapter.profile.views).as_dict(),
    )
    assert ledger.missing() == {"db_total"}


def test_separate_item_and_order_details_do_not_mix_implicit_counts_or_amounts(adapter):
    ledger = EvidenceLedger(required={"db_count", "db_total"})
    for sql in ["SELECT item_id,price FROM items", "SELECT id,amount FROM orders"]:
        ledger.add(
            "db",
            adapter.execute_query(sql, "a"),
            sql=sql,
            query_semantics=inspect_query(sql, adapter.profile.views).as_dict(),
        )
    assert ledger.missing() == {"db_count", "db_total"}
    sql = "SELECT COUNT(*) order_count,SUM(amount) total_amount FROM orders"
    ledger.add(
        "db",
        adapter.execute_query(sql, "a"),
        sql=sql,
        query_semantics=inspect_query(sql, adapter.profile.views).as_dict(),
    )
    assert ledger._stats() == ({2}, {300})
    assert not ledger.missing()


def test_only_successful_item_query_cannot_complete_requested_order_count(adapter):
    sql = "SELECT item_id,order_id,price FROM items"
    ledger = EvidenceLedger(required={"db_count"})
    ledger.add(
        "db",
        adapter.execute_query(sql, "a"),
        sql=sql,
        query_semantics=inspect_query(sql, adapter.profile.views).as_dict(),
    )
    assert ledger.missing() == {"db_count"}


def test_explicit_distinct_order_count_uses_enforced_order_keys_not_amounts(adapter):
    sql = "SELECT i.item_id,i.price,o.id AS order_id,o.amount FROM items i JOIN orders o ON i.order_id=o.id"
    shape = inspect_query(sql, adapter.profile.views).as_dict()
    ledger = EvidenceLedger(required={"db_count"}, prompt="统计结果中的不同订单数")
    ledger.add("db", adapter.execute_query(sql, "a"), sql=sql, query_semantics=shape)
    assert ledger._stats()[0] == {2}
    assert not ledger.missing()
    assert not ledger.issues("不同订单数：2。记录数：3。")
    assert ledger.issues("订单数：3。记录数：3。")
    ledger.prompt = "统计总订单数，包括没有商品行的订单"
    assert ledger.missing() == {"db_count"}
    assert not inspect_query(sql + " LIMIT 1", adapter.profile.views).distinct_order_keys
    missing_key = sql.replace("o.id AS order_id,", "")
    assert not inspect_query(missing_key, adapter.profile.views).distinct_order_keys
