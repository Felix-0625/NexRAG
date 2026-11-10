"""Import public Olist CSVs into a local SQLite business database.

Preserve original columns and add integer-cent amounts to avoid floating-point errors.
Build and validate a temporary database before replacing the destination.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "data" / "source" / "olist"
DEFAULT_DATABASE = ROOT / "data" / "sqlite" / "business.db"

# Map each public CSV to a table with its original columns.
# Retain original Olist spelling for provenance and auditing.
TABLES = {
    "customers": (
        "olist_customers_dataset.csv",
        "customer_id TEXT PRIMARY KEY, customer_unique_id TEXT, "
        "customer_zip_code_prefix TEXT, customer_city TEXT, customer_state TEXT",
        (),
    ),
    "geolocation": (
        "olist_geolocation_dataset.csv",
        "geolocation_zip_code_prefix TEXT, geolocation_lat REAL, "
        "geolocation_lng REAL, geolocation_city TEXT, geolocation_state TEXT",
        (),
    ),
    "order_items": (
        "olist_order_items_dataset.csv",
        "order_id TEXT, order_item_id INTEGER, product_id TEXT, seller_id TEXT, "
        "shipping_limit_date TEXT, price TEXT, freight_value TEXT, "
        "price_cents INTEGER NOT NULL, freight_value_cents INTEGER NOT NULL, "
        "PRIMARY KEY (order_id, order_item_id)",
        ("price", "freight_value"),
    ),
    "order_payments": (
        "olist_order_payments_dataset.csv",
        "order_id TEXT, payment_sequential INTEGER, payment_type TEXT, "
        "payment_installments INTEGER, payment_value TEXT, "
        "payment_value_cents INTEGER NOT NULL, PRIMARY KEY (order_id, payment_sequential)",
        ("payment_value",),
    ),
    "order_reviews": (
        "olist_order_reviews_dataset.csv",
        "review_id TEXT, order_id TEXT, review_score INTEGER, "
        "review_comment_title TEXT, review_comment_message TEXT, "
        "review_creation_date TEXT, review_answer_timestamp TEXT",
        (),
    ),
    "orders": (
        "olist_orders_dataset.csv",
        "order_id TEXT PRIMARY KEY, customer_id TEXT, order_status TEXT, "
        "order_purchase_timestamp TEXT, order_approved_at TEXT, "
        "order_delivered_carrier_date TEXT, order_delivered_customer_date TEXT, "
        "order_estimated_delivery_date TEXT",
        (),
    ),
    "products": (
        "olist_products_dataset.csv",
        "product_id TEXT PRIMARY KEY, product_category_name TEXT, "
        "product_name_lenght INTEGER, product_description_lenght INTEGER, "
        "product_photos_qty INTEGER, product_weight_g INTEGER, "
        "product_length_cm INTEGER, product_height_cm INTEGER, product_width_cm INTEGER",
        (),
    ),
    "sellers": (
        "olist_sellers_dataset.csv",
        "seller_id TEXT PRIMARY KEY, seller_zip_code_prefix TEXT, "
        "seller_city TEXT, seller_state TEXT",
        (),
    ),
    "product_category_name_translation": (
        "product_category_name_translation.csv",
        "product_category_name TEXT PRIMARY KEY, product_category_name_english TEXT",
        (),
    ),
}

INDEXES = (
    "CREATE INDEX idx_items_seller ON order_items(seller_id)",
    "CREATE INDEX idx_items_product ON order_items(product_id)",
    "CREATE INDEX idx_items_order ON order_items(order_id)",
    "CREATE INDEX idx_orders_customer ON orders(customer_id)",
    "CREATE INDEX idx_orders_purchase ON orders(order_purchase_timestamp)",
    "CREATE INDEX idx_payments_order ON order_payments(order_id)",
    "CREATE INDEX idx_reviews_order ON order_reviews(order_id)",
    "CREATE INDEX idx_geo_zip ON geolocation(geolocation_zip_code_prefix)",
)


def cents(value: str, *, filename: str, row_number: int, column: str) -> int:
    """Convert public BRL amounts exactly to cents, rejecting malformed rows."""
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{filename}:{row_number}: invalid {column}={value!r}") from exc
    scaled = amount * 100
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{filename}:{row_number}: {column} has sub-cent precision")
    return int(scaled)


def import_table(
    connection: sqlite3.Connection,
    source: Path,
    table: str,
    ddl: str,
    money_columns: tuple[str, ...],
) -> tuple[int, str]:
    """Load one source file and return its row count and SHA-256."""
    if not source.is_file():
        raise FileNotFoundError(source)
    connection.execute(f"CREATE TABLE {table} ({ddl})")

    digest = hashlib.sha256()
    with source.open("rb") as raw:
        for block in iter(lambda: raw.read(1024 * 1024), b""):
            digest.update(block)

    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = tuple(reader.fieldnames or ())
        if not columns:
            raise ValueError(f"{source.name}: missing CSV header")
        expected_columns = tuple(
            part.strip().split(" ", 1)[0]
            for part in ddl.split(",")
            if not part.strip().startswith("PRIMARY KEY")
        )
        source_columns = expected_columns[: len(columns)]
        if columns != source_columns:
            raise ValueError(f"{source.name}: unexpected columns {columns!r}")

        insert_columns = columns + tuple(f"{name}_cents" for name in money_columns)
        placeholders = ", ".join("?" for _ in insert_columns)
        statement = f"INSERT INTO {table} ({', '.join(insert_columns)}) VALUES ({placeholders})"
        batch: list[tuple[object, ...]] = []
        count = 0
        for row_number, row in enumerate(reader, start=2):
            if None in row:
                raise ValueError(f"{source.name}:{row_number}: unexpected extra CSV fields")
            values: tuple[object, ...] = tuple(row[name] or None for name in columns)
            values += tuple(
                cents(row[name] or "", filename=source.name, row_number=row_number, column=name)
                for name in money_columns
            )
            batch.append(values)
            if len(batch) == 5_000:
                connection.executemany(statement, batch)
                count += len(batch)
                batch.clear()
        if batch:
            connection.executemany(statement, batch)
            count += len(batch)

    if count == 0:
        raise ValueError(f"{source.name}: empty source file")
    return count, digest.hexdigest()


def import_dataset(source_dir: Path, database_path: Path) -> dict[str, object]:
    """Build and validate SQLite before replacing the destination."""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix="nexrag-olist-", suffix=".db", dir=database_path.parent, delete=False
    ) as temporary:
        staging_path = Path(temporary.name)

    try:
        connection = sqlite3.connect(staging_path)
        try:
            tables: dict[str, dict[str, object]] = {}
            for table, (filename, ddl, money_columns) in TABLES.items():
                count, sha256 = import_table(
                    connection, source_dir / filename, table, ddl, money_columns
                )
                tables[table] = {"source": filename, "rows": count, "sha256": sha256}
                connection.commit()
            for statement in INDEXES:
                connection.execute(statement)
            connection.commit()

            # Seller identity lives at order-item grain and anchors authorization.
            missing_sellers = connection.execute(
                "SELECT COUNT(*) FROM order_items i LEFT JOIN sellers s "
                "ON s.seller_id = i.seller_id WHERE s.seller_id IS NULL"
            ).fetchone()[0]
            if missing_sellers:
                raise ValueError(f"{missing_sellers} order items lack a matching seller")
            seller_count = connection.execute("SELECT COUNT(*) FROM sellers").fetchone()[0]
            multiseller_orders = connection.execute(
                "SELECT COUNT(*) FROM (SELECT order_id FROM order_items "
                "GROUP BY order_id HAVING COUNT(DISTINCT seller_id) > 1)"
            ).fetchone()[0]
        finally:
            connection.close()

        os.replace(staging_path, database_path)
    finally:
        if staging_path.exists():
            staging_path.unlink()

    return {
        "source": "https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce",
        "version": 2,
        "license": "CC BY-NC-SA 4.0",
        "database": str(database_path),
        "seller_count": seller_count,
        "multiseller_orders": multiseller_orders,
        "tables": tables,
    }


def main() -> None:
    """Parse source and database options, import Olist, and print the JSON summary."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    args = parser.parse_args()
    result = import_dataset(args.source_dir.resolve(), args.database.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
