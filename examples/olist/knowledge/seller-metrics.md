# Olist 卖家指标解释（NexRAG 非商业示例）

数据源为 [Olist Brazilian E-Commerce Public Dataset](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce)，许可为 CC BY-NC-SA 4.0。本文件由 NexRAG 根据导入表结构与公开视图配置编写，用于说明演示场景的指标口径；不是 Olist 官方业务政策。

## 卖家商品收入

卖家商品收入使用该卖家 `order_items` 商品行的价格求和。NexRAG 的 `seller_items.price_cents` 是整数分，换算巴西雷亚尔时计算 `SUM(price_cents) / 100.0`。此数值不包含商品行运费，也不等于订单实际付款总额。

## 订单计数

一个订单可能包含多个商品行，且可能涉及多个卖家。对一个卖家统计订单量时，按该卖家商品行中的 `order_id` 去重，即 `COUNT(DISTINCT order_id)`。不能直接计算商品行数，也不能把整个订单独占地分给其中一个卖家。

## 为什么不使用订单级付款与评价

源数据中的 `order_payments` 和 `order_reviews` 与订单关联，不与单个卖家的商品行一一对应。跨卖家订单的整笔付款或整条评价没有可信的卖家级归属规则。因此 NexRAG 的默认卖家视图不暴露这两类表，也不生成卖家付款额或卖家评分指标。若实际业务要使用这些指标，应先定义并记录明确的分摊或归属规则，再修改业务视图与元数据。

## 权限说明

Open WebUI 中选择的卖家只是查询视角；NexRAG 后端还要验证已登录用户对该卖家的查看权限。以上文档解释对所有用户共享，文档检索不受业务租户选择约束。
