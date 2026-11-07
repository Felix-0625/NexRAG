# 示例

`olist/` 是随仓库提供的演示配置，用公开的 Olist 电商数据展示“业务库 + 知识库”联合分析。它只是一份可替换的示例，核心代码不依赖其中的表名。

| 文件 | 作用 |
| --- | --- |
| `business-source.yaml` | 业务数据源：只读 SQLite；每个视图的查询必须暴露 `__tenant_id`，适配器据此只取选中租户的行 |
| `metadata.yaml` | 表、字段的业务释义与注册指标，供 Agent 做模式剪枝和指标口径匹配 |
| `tenant-access.yaml` | 两个演示卖家（租户）。管理员可选择两者；把已验证用户的邮箱加到 `users` 下可授予更窄的视图，未列出的非管理员没有业务数据权限 |
| `knowledge/seller-metrics.md` | 示例知识文档，说明指标口径 |

Windows 下从仓库根目录运行 `start.cmd`，默认配置会在首次启动时下载并导入演示数据。首次登录后，管理员可选择任一演示租户，并在工作空间将 `olist/knowledge/seller-metrics.md` 上传到自己的知识库。

下载与导入分别由 `scripts/fetch-olist.ps1` 和 `scripts/import_olist.py` 完成，结果保存在被 Git 忽略的 `data/` 下。Olist 原始数据的 CC BY-NC-SA 4.0 许可限非商业用途。见 [数据来源](../docs/data-sources.md) 和 [自有业务库接入](../docs/connect-business-database.md)。
