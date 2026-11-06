"""验证元数据目录与智能体工具中的动态表结构裁剪。"""

from __future__ import annotations

import pytest

from src.agent.tools import business_schema
from src.config import settings
from src.midplatform.catalog import MetadataCatalog


@pytest.fixture
def catalog():
    return MetadataCatalog(settings.METADATA_PATH)


def test_prune_schema_matches_product_inquiry(catalog):
    """与商品、类目或重量相关的关键词应裁剪到 seller_products。"""
    tables = catalog.prune_schema("我想查看售出商品的品类分布和商品重量 weight", max_tables=1)
    assert tables == ["seller_products"]


def test_prune_schema_matches_orders_inquiry(catalog):
    """与配送、日期或客户城市相关的关键词应裁剪到 seller_orders。"""
    tables = catalog.prune_schema("买家所在城市以及订单是否按时送达 delivered", max_tables=1)
    assert tables == ["seller_orders"]


def test_prune_schema_matches_revenue_inquiry(catalog):
    """营收与价格类指标的询问应裁剪到 seller_items。"""
    tables = catalog.prune_schema("统计总销售额 revenue 和每笔订单商品金额 price", max_tables=1)
    assert tables == ["seller_items"]


def test_business_schema_prunes_metrics_and_tables():
    """business_schema 辅助函数只应描述裁剪后的表及其相关指标。"""
    pruned = business_schema(
        settings.METADATA_PATH, query="seller_profile 卖家地区资料", max_tables=1
    )
    assert "### Table: seller_profile" in pruned
    # seller_profile 没有映射任何指标，所以指标块不应出现 seller_items 的指标
    assert "item_revenue_brl" not in pruned


def test_business_schema_single_table_override():
    """显式指定 table_name 时，只返回该表的结构。"""
    output = business_schema(settings.METADATA_PATH, table_name="seller_orders")
    assert "### Table: seller_orders" in output
    assert "### Table: seller_products" not in output
