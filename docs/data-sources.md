# 本地数据来源与租户边界

## Olist 业务数据库

- 来源：[Olist 发布的 Brazilian E-Commerce Public Dataset](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce)。
- 版本：Kaggle dataset version 2；`scripts/fetch-olist.ps1` 使用发布者数据集的下载接口。
- 许可：[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/)；本项目仅作非商业作品演示。公开展示时保留 Olist 数据来源与许可说明。若用途变为商业用途，应先重新确认数据使用权。
- 压缩包 SHA-256：`967E41E04FC306FE604E2A693F488995A8B41E5047418F8A5C8E4ABD6DECA784`。
- 本地原始文件：`data/source/olist/`；导入结果：`data/sqlite/business.db`。两者均由脚本生成并被 Git 忽略。

`scripts/import_olist.py` 保留原始字段名，并为价格、运费和付款金额增加以整数分为单位的派生列。这样可复现原始数据，也能在汇总金额时避免浮点舍入。

| 表 | 实际导入行数 | 业务用途 |
| :--- | ---: | :--- |
| `customers` | 99,441 | 买家与地区 |
| `geolocation` | 1,000,163 | 邮编前缀坐标 |
| `order_items` | 112,650 | 商品、卖家与商品级金额 |
| `order_payments` | 103,886 | 订单级付款 |
| `order_reviews` | 99,224 | 订单级评价 |
| `orders` | 99,441 | 订单状态与时间 |
| `products` | 32,951 | 商品属性 |
| `sellers` | 3,095 | 卖家与地区 |
| `product_category_name_translation` | 71 | 品类英文映射 |

**权限粒度**：此数据集中 `seller_id` 位于 `order_items`，共有 1,278 个订单涉及多个卖家。因此业务租户以卖家为单位，后端从商品行建立权限边界。订单级的 `order_payments` 与 `order_reviews` 不作为卖家专属数据返回；若将来提供卖家指标，必须先定义并实现可解释的分摊或聚合口径。当前已有带签名用户身份校验的业务查询 HTTP 接口、Open WebUI 租户控件源码补丁及本地服务编排。

## 文档知识库

项目使用 `base/ingestion-service` 和 `base/retrieval-agent` 作为文档摄取与检索基座，原生启动器连接本机服务和 Qdrant。通过网页上传到本人知识库后，服务端校验所有权/授权，再摄入独立库范围。企业授权库引用同一资源并跟随更新；私人库不会因业务租户选择或管理员身份而对其他账号开放。

`scripts/ingest_documents.py` 供受信部署者离线摄入使用，不能代替员工知识库分发与网页验收。演示文档 [seller-metrics.md](../examples/olist/knowledge/seller-metrics.md) 及早期“2 个片段”的记录属于当时样例，不是新 clone 已有知识的证明。新用户可在网页创建知识库并上传自有材料。Olist 的业务视图、元数据和初始授权配置位于 `examples/olist/`，其他用户按 [接入说明](connect-business-database.md) 替换。

## Open WebUI 源码

`scripts/prepare-webui.ps1` 固定检出 [AarhusAI/open-webui](https://github.com/AarhusAI/open-webui) 的 `v0.11.4`，提交 `8bd8b4fac5e059578ac0c74b3c18d11139f88b7d`，然后按序应用 `patches/open-webui/v0.11.4/nexrag/` 中的 tenant-selector、ops-dashboard、workspace、security-npm 四个项目补丁，其中 security-npm.diff 维护依赖清单、锁和对应的 ESLint 配置。旧外部 RAG 补丁已经由独立原生桥替换，来源与许可处理见 [NOTICE](../patches/open-webui/NOTICE.md)。源码工作树位于被忽略的 `webui/`；后续定制应维护为本仓库可重放的补丁。使用与展示时遵守 [Open WebUI 许可证及品牌条款](https://docs.openwebui.com/license/)。
