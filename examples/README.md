# 示例

`olist/` 是随仓库提供的演示配置，用公开的 Olist 电商数据展示“业务库 + 知识库”联合分析。它只是一份可替换的示例，核心代码不依赖其中的表名。

| 文件 | 作用 |
| --- | --- |
| `business-source.yaml` | 业务数据源：只读 SQLite；每个视图的查询必须暴露 `__tenant_id`，适配器据此只取选中租户的行 |
| `metadata.yaml` | 表、字段的业务释义与注册指标，供 Agent 做模式剪枝和指标口径匹配 |
| `tenant-access.yaml` | 两个演示卖家（租户）。管理员可选择两者；把已验证用户的邮箱加到 `users` 下可授予更窄的视图，未列出的非管理员没有业务数据权限 |
| `knowledge/seller-metrics.md` | 示例知识文档，说明指标口径 |

数据本身不在仓库里：`scripts/fetch-olist.ps1` 下载、`scripts/import_olist.py` 导入，结果在被 Git 忽略的 `data/` 下。数据许可（CC BY-NC-SA 4.0，仅限非商业）见 [数据来源](../docs/data-sources.md)。接入自己的数据库见 [业务数据库接入](../docs/connect-business-database.md)。
