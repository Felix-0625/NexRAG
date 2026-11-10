"""Dynamic schema pruning in metadata catalogs and agent tools."""

from __future__ import annotations

import pytest

from src.agent.tools import business_schema
from src.config import settings
from src.midplatform.catalog import MetadataCatalog


@pytest.fixture
def catalog():
    """Provide catalog test data or behavior for this scenario."""
    return MetadataCatalog(settings.METADATA_PATH)


def test_prune_schema_matches_product_inquiry(catalog):
    """Product questions retain seller_products."""
    tables = catalog.prune_schema("我想查看售出商品的品类分布和商品重量 weight", max_tables=1)
    assert tables == ["seller_products"]


def test_prune_schema_matches_orders_inquiry(catalog):
    """Delivery, date, and customer-city questions retain seller_orders."""
    tables = catalog.prune_schema("买家所在城市以及订单是否按时送达 delivered", max_tables=1)
    assert tables == ["seller_orders"]


def test_prune_schema_matches_revenue_inquiry(catalog):
    """Revenue and price questions retain seller_items."""
    tables = catalog.prune_schema("统计总销售额 revenue 和每笔订单商品金额 price", max_tables=1)
    assert tables == ["seller_items"]


def test_business_schema_prunes_metrics_and_tables():
    """Schema tools describe only selected tables and their metrics."""
    pruned = business_schema(
        settings.METADATA_PATH, query="seller_profile 卖家地区资料", max_tables=1
    )
    assert "### Table: seller_profile" in pruned
    # seller_profile has no mapped metrics, so seller_items metrics must not leak into its schema.
    assert "item_revenue_brl" not in pruned


def test_business_schema_single_table_override():
    """Explicit table selection returns only that table's schema."""
    output = business_schema(settings.METADATA_PATH, table_name="seller_orders")
    assert "### Table: seller_orders" in output
    assert "### Table: seller_products" not in output
