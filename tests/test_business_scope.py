"""Seller isolation over Olist's real multi-seller order structure."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import yaml

from src.midplatform.adapters import SQLiteAdapter, load_source_profile
from src.midplatform.safety import UnsafeQueryError


@pytest.fixture
def adapter(tmp_path):
    """Provide adapter test data or behavior for this scenario."""
    database = tmp_path / "business.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(
            """
            CREATE TABLE sellers (seller_id TEXT, seller_city TEXT, seller_state TEXT);
            CREATE TABLE customers (customer_id TEXT, customer_city TEXT, customer_state TEXT);
            CREATE TABLE orders (
                order_id TEXT, customer_id TEXT, order_status TEXT,
                order_purchase_timestamp TEXT, order_delivered_customer_date TEXT,
                order_estimated_delivery_date TEXT
            );
            CREATE TABLE order_items (
                order_id TEXT, order_item_id INTEGER, seller_id TEXT,
                product_id TEXT, shipping_limit_date TEXT,
                price_cents INTEGER, freight_value_cents INTEGER
            );
            CREATE TABLE products (
                product_id TEXT, product_category_name TEXT,
                product_photos_qty INTEGER, product_weight_g INTEGER
            );
            CREATE TABLE product_category_name_translation (
                product_category_name TEXT, product_category_name_english TEXT
            );
            CREATE TABLE order_payments (order_id TEXT, payment_value_cents INTEGER);
            INSERT INTO sellers VALUES ('seller-a', 'Sao Paulo', 'SP');
            INSERT INTO sellers VALUES ('seller-b', 'Rio', 'RJ');
            INSERT INTO customers VALUES ('buyer', 'Sao Paulo', 'SP');
            INSERT INTO orders VALUES ('shared-order', 'buyer', 'delivered',
                '2018-01-01', '2018-01-05', '2018-01-06');
            INSERT INTO order_items VALUES
                ('shared-order', 1, 'seller-a', 'product-a', '2018-01-02', 1000, 100),
                ('shared-order', 2, 'seller-b', 'product-b', '2018-01-02', 9000, 900),
                ('shared-order', 3, 'seller-a', 'product-a', '2018-01-02', 500, 50);
            INSERT INTO products VALUES ('product-a', 'books', 1, 100);
            INSERT INTO products VALUES ('product-b', 'electronics', 1, 200);
            INSERT INTO product_category_name_translation VALUES ('books', 'books');
            INSERT INTO product_category_name_translation VALUES ('electronics', 'electronics');
            INSERT INTO order_payments VALUES ('shared-order', 11000);
            """
        )
    return SQLiteAdapter(database)


def test_shared_order_exposes_only_selected_seller_items(adapter):
    rows = adapter.execute_query(
        "SELECT i.seller_id, SUM(i.price_cents) AS revenue_cents, "
        "COUNT(*) AS item_count, o.order_status "
        "FROM seller_items i JOIN seller_orders o USING (order_id) "
        "GROUP BY i.seller_id, o.order_status",
        "seller-a",
    )
    assert rows == [
        {
            "seller_id": "seller-a",
            "revenue_cents": 1500,
            "item_count": 2,
            "order_status": "delivered",
        }
    ]
    assert adapter.execute_query(
        "SELECT COUNT(*) AS n FROM seller_items WHERE seller_id = 'seller-b'", "seller-a"
    ) == [{"n": 0}]


def test_source_and_order_level_payment_are_unavailable(adapter):
    for sql in ("SELECT * FROM order_items", "SELECT * FROM order_payments"):
        with pytest.raises(UnsafeQueryError):
            adapter.execute_query(sql, "seller-a")
    with pytest.raises(UnsafeQueryError):
        adapter.execute_query("SELECT * FROM seller_items; DELETE FROM sellers", "seller-a")
    with pytest.raises(ValueError, match="unavailable operation"):
        adapter.execute_query("SELECT randomblob(1000000) FROM seller_items", "seller-a")


def test_unknown_seller_and_unbounded_output_fail_closed(adapter):
    with pytest.raises(ValueError, match="does not exist"):
        adapter.execute_query("SELECT * FROM seller_items", "seller-unknown")
    with pytest.raises(ValueError, match="max_rows"):
        adapter.execute_query("SELECT * FROM seller_items", "seller-a", max_rows=0)
    with pytest.raises(ValueError, match="exceeds max_rows"):
        adapter.execute_query("SELECT * FROM seller_items", "seller-a", max_rows=1)


def test_business_metadata_matches_queryable_schema():
    root = Path(__file__).resolve().parents[1]
    metadata_path = root / "examples" / "olist" / "metadata.yaml"
    profile = load_source_profile(root / "examples" / "olist" / "business-source.yaml")
    metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
    assert set(metadata["tables"]) == set(profile.views)
    for table, view in profile.views.items():
        assert set(metadata["tables"][table]["columns"]) == set(view.columns)
