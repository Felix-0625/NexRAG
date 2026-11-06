# 接入自己的业务数据库

NexRAG 的业务数据库是可替换的数据源。当前支持本地 SQLite 文件和 PostgreSQL；`examples/olist/` 是默认交互样例。接入新的业务库无需修改 Agent、SQL 安全规则或 Open WebUI 租户控件。

## 1. 准备只读业务库和租户归属

把自己的 SQLite 文件放在本地可读取的位置，或为 PostgreSQL 准备仅有业务表 `SELECT` 权限的专用账号。找出每条业务记录对应的租户 ID，例如 `invoices.company_id`。对于关联视图，要在查询中明确业务归属；不能仅凭订单、付款等跨租户记录推断归属。不要把私有数据库或连接密钥提交到 Git。

## 2. 定义可查询视图

复制 [Olist 数据源配置](../examples/olist/business-source.yaml)并替换数据库路径和 `views`。以下示例把发票归属映射为内部字段 `__tenant_id`：

```yaml
type: sqlite
database_path: ./company.db
views:
  invoice_summary:
    columns:
      id: TEXT
      amount_cents: INTEGER
    source_sql: >-
      SELECT id, amount_cents, company_id AS __tenant_id FROM invoices
```

`database_path` 相对于这个 YAML 文件所在目录；也可通过 `BUSINESS_DB_PATH` 覆盖，覆盖值的相对路径按进程工作目录解析。每个 `source_sql` 必须是一条只读 `SELECT`，返回所列公共字段及 `__tenant_id`。NexRAG 将它包在查询中，使用服务端验证过的租户 ID 过滤后才复制到分析沙箱。Agent 不能直接查询 `invoices` 等源表。可选的 `tenant_exists_sql` 接收一个 `?` 参数，用于拒绝不存在的租户。

PostgreSQL 使用相同的公开视图配置，但把类型改为 `postgresql`，不在 YAML 中放凭据：

```yaml
type: postgresql
tenant_exists_sql: SELECT 1 FROM public.companies WHERE company_id = %s
views:
  invoice_summary:
    columns:
      id: TEXT
      amount_cents: INTEGER
    source_sql: >-
      SELECT id, amount_cents, company_id AS __tenant_id FROM public.invoices
```

在 `.env` 设置 `BUSINESS_DB_DSN=postgresql://用户:密码@主机:5432/数据库`。连接地址通过环境配置或管理员数据源设置传入，本机数据库一般使用 `127.0.0.1`，其他主机使用真实可达地址。`tenant_exists_sql` 在 PostgreSQL 中使用一个 `%s` 参数。源库连接以只读事务读取，并为源查询设置 30 秒超时；获授权租户的数据仍复制到独立的 SQLite 分析沙箱后才执行 Agent SQL。当前按请求构建快照，接入大库前应评估行数、延迟和内存。

这个配置由部署者维护，属于受信任的权限配置。部署者必须检查每个视图中 `__tenant_id` 的业务含义；把错误的字段映射为租户 ID 会产生错误的授权边界。

## 3. 维护元数据与授权

复制 [Olist 元数据](../examples/olist/metadata.yaml)，为每个公开视图维护同名的字段释义和指标公式。配置中的视图名、字段名必须与数据源配置一致，否则 Agent 会拒绝加载。可以在 `query_guidance` 里写业务口径，例如金额单位、去重规则和不可归属的指标。

复制 [Olist 租户授权](../examples/olist/tenant-access.yaml)，在 `tenants` 中列出可选租户，在 `users` 中按 Open WebUI 已验证的邮箱授权。例如：

```yaml
tenants:
  company-a: Company A
  company-b: Company B
users:
  analyst@example.local:
    - company-a
```

普通用户只能选择其授权租户；Open WebUI 管理员当前可以选择配置中的全部租户。文档知识库检索不使用此业务租户限制。

## 4. 指向自己的配置并验证

在本地 `.env` 设置三个路径：

```dotenv
BUSINESS_SOURCE_PATH=path/to/business-source.yaml
METADATA_PATH=path/to/metadata.yaml
TENANT_ACCESS_PATH=path/to/tenant-access.yaml
```

这些路径由本地原生进程读取，SQLite 通过只读连接打开，PostgreSQL 使用 `BUSINESS_DB_DSN` 和专用只读账号。修改启动配置后重启本项目服务，再检查数据源和授权；不要将真实业务文件或凭据放入可公开的示例目录。详见 [本地运行说明](local-run.md)。

服务间密钥 `NEXRAG_SHARED_API_KEY` 和签名密钥 `NEXRAG_USER_JWT_SECRET` 必须分别设置为至少 32 字符，并与 Open WebUI 服务端配置一致；不要把密钥或业务数据提交到仓库。先用只读 SQL 验证每个租户的行数、跨租户隔离和字段口径，再通过 Open WebUI 选择租户测试问答。仓库中的 `tests/test_custom_business_source.py` 使用完全不同的发票库验证配置替换与 HTTP 权限边界。

当前两个连接器都按请求创建租户快照，适合本地开发和中小规模数据。大库性能与生产部署权限需要按实际数据规模另行验收。本地 PostgreSQL 集成测试需要一个专用测试库，包含 `companies(company_id)` 与 `invoices(id, company_id, amount_cents)`；分别写入 `company-a` 的 1250 分发票和 `company-b` 的 9900 分发票，为测试只读账号授予这两张表的 `SELECT` 权限，再设置 `NEXRAG_TEST_POSTGRES_DSN` 并运行 `python -m pytest tests/test_postgresql_source.py -q`。
