# 业务数据库查询接口与变量

NexRAG 的核心接口使用通用 `tenant_id`。随仓库提供的 Olist 配置把一个卖家映射为一个业务租户；其他项目可以映射为组织、门店或客户。当前业务库连接器支持 SQLite 与 PostgreSQL。

## HTTP 接口

| 方法与路径 | 请求 | 返回 | 当前状态 |
| :--- | :--- | :--- | :--- |
| `GET /v1/tenants` | 无请求体 | `data: [{tenant_id, label}]` | 已实现；只返回当前用户可选业务租户。 |
| `POST /v1/business/query` | `{tenant_id, sql, max_rows?, timeout_seconds?}` | `{tenant_id, row_count, rows}` | 已实现；先核对用户授权，再执行只读查询。 |
| Open WebUI `GET /api/nexrag/tenants` | 浏览器携带 Open WebUI 会话令牌 | 同上 | 源码补丁已实现；以已验证用户身份代理到 NexRAG。 |
| `GET /v1/models` | 无请求体 | OpenAI 模型列表 | 已实现；供 Open WebUI 发现 `NexRAG`。 |
| `POST /v1/chat/completions` | `{model: "NexRAG", messages, stream?}`，附 `X-NexRAG-Tenant-Id` | OpenAI Chat Completion JSON 或 SSE | 已接入单 Agent；验证签名身份和卖家授权后调用业务、文档、搜索与文件沙箱工具。支持 Token 级实时流式输出。 |

业务接口必须同时携带 `Authorization: Bearer <NEXRAG_SHARED_API_KEY>` 与 Open WebUI 签发的 `X-OpenWebUI-User-Jwt`。后端验证 JWT 的 HS256 签名、`iss=open-webui`、有效期及必需身份字段。普通用户按 `TENANT_ACCESS_PATH` 指向的授权配置获得租户权限；Open WebUI 管理员可选择配置中列出的租户。直接填写未授权租户 ID 返回 403。服务密钥和 JWT 密钥应仅配置在两个后端服务的环境变量中，不发送给浏览器。

Open WebUI 源码补丁已增加通用租户选择控件、签名身份代理与聊天请求中的 `nexrag_tenant_id` 转发。补丁位于 `patches/open-webui/v0.11.4/nexrag/tenant-selector.diff`，由 `scripts/prepare-webui.ps1` 在上游补丁之后应用。根目录编排使用该定制源码并接入 AarhusAI 检索服务与外部搜索引擎。本地已验证卖家界面的切换与查询，以及结合数据库金额、知识文档、临时文件与外部事实的综合回答。

## Agent 工具与运行变量

PydanticAI 单 Agent 提供：
1. `plan_analysis`：多步拆解规划；
2. `get_business_schema`：支持基于提问与表名的动态模式剪枝；
3. `query_database`：只读业务快照安全查询；
4. `search_knowledge_documents`：私有文档知识库检索与自省反思；
5. `search_web_engine`：外部互联网（SearXNG）实时搜索与优雅离线降级；
6. `inspect_local_file`：沙箱内即时文件解析与采样（CSV/TSV/JSON 表格与文本日志，防止知识库污染）。

业务 SQL 在已授权租户的独立快照中运行；知识检索委托 `base/retrieval-agent` 的 `/search` 或本地 Qdrant 集合；外部搜索在私有文档缺失或需要实时信息时作为兜底补充；文件沙箱允许对前端临时上传的表格与文本文件进行即时采样与分析，用完即弃且不写入底层向量库。Agent 在成功获取可靠证据前不会把模型内部偏见作为事实答案；失败的工具调用可触发有限反思重试。

| 变量 | 默认值 | 用途 |
| :--- | :--- | :--- |
| `LLM_API_BASE` | `http://localhost:11434/v1` | 本地 Ollama 或任何 OpenAI 兼容模型端点（DeepSeek, Qwen, vLLM 等）。 |
| `LLM_API_KEY` | `ollama` | 兼容 API 所需的密钥；远端模型填写真实 API Key。 |
| `LLM_MODEL_NAME` | `qwen3:4b-instruct` | 运行模型名称；支持随时切换至 `deepseek-chat` 或 `qwen-plus`。 |
| `AGENT_TIMEOUT_SECONDS` | `90` | 单次 Agent 运行总时限。 |
| `BUSINESS_SOURCE_PATH` | `examples/olist/business-source.yaml` | 业务数据源类型与租户视图配置。 |
| `BUSINESS_DB_PATH` | 无 | SQLite 文件路径覆盖值；相对路径按进程工作目录解析。 |
| `BUSINESS_DB_DSN` | 无 | PostgreSQL 连接串，只从服务端环境变量读取，不写入数据源 YAML。 |
| `METADATA_PATH` | `examples/olist/metadata.yaml` | 业务视图与指标口径；必须与数据源配置的视图和字段一致。 |
| `TENANT_ACCESS_PATH` | `examples/olist/tenant-access.yaml` | 用户邮箱到业务租户的授权配置。 |
| `RETRIEVAL_API_BASE_URL` | `http://localhost:8002` | AarhusAI retrieval-agent 基址，工具请求 `/search`。 |
| `RETRIEVAL_API_KEY` | 无 | retrieval-agent 的 Bearer 密钥；未配置时知识工具返回不可用。 |
| `RETRIEVAL_TIMEOUT_SECONDS` | `45` | Agent 知识工具等待本地嵌入和交叉编码器完成的总时间上限。 |
| `RETRIEVAL_MIN_RELEVANCE` | `0.35` | 知识检索重排可信度阈值；低于该分触发 Agent 改写重试提示。 |
| `MAX_TOOL_RETRIES` | `2` | 单次提问中针对空结果或低分结果允许的最大自省反思与改写次数。 |
| `KNOWLEDGE_COLLECTION_NAME` | `nexrag_docs` | 文档向量集合名称；由摄入工具建立。 |
| `ENABLE_WEB_SEARCH` | `true` | 是否启用外部互联网检索能力。 |
| `SEARXNG_URL` | `http://localhost:8080` | SearXNG 元搜索引擎 API 地址。 |
| `WEB_SEARCH_TIMEOUT_SECONDS` | `8.0` | 外部搜索引擎 HTTP 查询超时上限。 |
| `SANDBOX_ROOT_DIR` | `data/sandbox_uploads` | `inspect_local_file` 的专用沙箱根目录；`file_path` 只能解析到此目录内，不回退到进程工作目录。 |

## 原生文档切分与摄入（Payload Trick）

针对重型外部容器（Gotenberg、Kreuzberg）对内存的高额消耗及 Markdown 全量转换格式失真，NexRAG 额外提供了纯 Python 原生切分器 `NativeParentChildChunker` 与增强的摄入命令行，作为默认 AarhusAI 摄入链路（仍部署并默认使用 Kreuzberg 容器）的可选轻量替代：

- **父子切块（Hierarchical Chunking）**：父块保留约 1000 字符的完整语义上下文，子块保留约 200 字符的高密度匹配片段；
- **Payload Trick**：索引阶段只对子块生成稠密/稀疏向量，并将完整 `parent_content` 嵌入 Qdrant payload。检索命中细粒度子块后，通过 `restore_parent_context` 自动回溯展开父块，向 LLM 喂入完整上下文；
- **摄入执行命令**：
  ```bash
  # 原生模式（无需 Kreuzberg 容器，直连 Ollama 嵌入与 Qdrant；
  # 默认同时写入稠密与本地 FastEmbed BM42 稀疏向量，--no-sparse 可退化为纯稠密）：
  python scripts/ingest_documents.py docs/ --native

  # 演练与数据统计模式：
  python scripts/ingest_documents.py docs/ --native --dry-run
  ```

## 临时文件沙箱操作规范（Sandboxed File Operations）

当表格（CSV/TSV/JSON）或日志文本文件被放入 `SANDBOX_ROOT_DIR`（默认 `data/sandbox_uploads`）后，Agent 可在对话中通过 `inspect_local_file` 工具对其进行安全即时分析。当前版本尚未提供 Open WebUI 前端到该目录的文件上传接线；文件需要由部署者或运维脚本预先放入该目录，工具侧的安全边界如下：

1. **防路径逃逸**：使用 `validate_safe_path` 严格锁定文件必须在 `SANDBOX_ROOT_DIR` 边界内（而非进程当前工作目录），物理阻断跨目录穿越（`../`）；
2. **敏感文件防窃取**：自动拦截对 `.env*`、`.git/`、`*.key`、`*.pem`、`*.db`、`*.yaml`/`*.yml`、`*.py` 等敏感或源码类文件的任何探查尝试；
3. **防上下文爆仓**：表格文件仅读取列名、总行数并抽取前 N 条样本（默认 10 行）；文本文件支持行级分页切片（`start_line` / `max_lines`），严禁海量未剪裁文本涌入上下文；
4. **零污染原则**：临时分析结果仅在当前轮次留存于内存上下文中，绝不写入底座 Qdrant 知识库。

## 内部适配器

`src.midplatform.adapters.create_business_adapter()` 按数据源配置选择 `SQLiteAdapter` 或 `PostgreSQLAdapter`。两者的 `execute_query(sql, tenant_id, *, max_rows=100, timeout_seconds=2.0)` 接收已授权的租户 ID，在独立内存 SQLite 库中执行一条只读 `SELECT`，返回 `list[dict]`。适配器不负责认证；HTTP 层先验证服务密钥、签名用户身份以及该用户对所选租户的查看权限。

| 参数或变量 | 默认值 | 含义与边界 |
| :--- | :--- | :--- |
| `sql` | 必填 | 单条 SQLite `SELECT`；由 SQLGlot 解析，只可访问下表。 |
| `tenant_id` | 必填 | 已授权的业务租户 ID，由可信调用方传入；前端选择值不能直接作为授权依据。 |
| `max_rows` | `100` | 允许返回 1–1,000 行；超出时拒绝结果，让调用方缩小查询。 |
| `timeout_seconds` | `2.0` | 内存库中的 SQL 执行时间上限；不包括构建卖家快照的耗时。 |
| `BUSINESS_SOURCE_PATH` | `examples/olist/business-source.yaml` | 数据源与允许查询的视图配置。 |
| `BUSINESS_DB_PATH` | 无 | 可选的源库路径覆盖值。 |
| `BUSINESS_DB_DSN` | 无 | PostgreSQL 源库连接串；源连接使用只读事务与 30 秒语句超时。 |
| `NEXRAG_SHARED_API_KEY` | 无 | Open WebUI 到 NexRAG 的服务间密钥，至少 32 字符。 |
| `NEXRAG_USER_JWT_SECRET` | 无 | 验证 Open WebUI 转发用户 JWT 的共享密钥，至少 32 字符；与 Open WebUI 的 `FORWARD_USER_INFO_HEADER_JWT_SECRET` 相同。 |
| `NEXRAG_USER_JWT_HEADER` | `X-OpenWebUI-User-Jwt` | 与 Open WebUI 的 `FORWARD_USER_INFO_HEADER_JWT` 保持一致。 |
| `ENABLE_FORWARD_USER_INFO_HEADERS` | Open WebUI 默认为 `false` | 接线时在 Open WebUI 设置为 `true`，使其向外部模型服务转发签名身份。 |
| `FORWARD_USER_INFO_HEADER_JWT_SECRET` | Open WebUI 无默认值 | 与 NexRAG 的 `NEXRAG_USER_JWT_SECRET` 使用同一个服务端密钥。 |
| `NEXRAG_API_BASE_URL` | Open WebUI 无默认值 | Open WebUI 租户列表代理使用的 NexRAG 地址，应指向 `/v1`。 |
| `OPENAI_API_BASE_URL` | 启动器注入本机动态端口 `/v1` | Open WebUI 访问 NexRAG 的 OpenAI 兼容入口。 |
| `OPENAI_API_KEY` | 启动器使用 `NEXRAG_SHARED_API_KEY` | Open WebUI 到 NexRAG 的服务凭据。 |

| Olist 示例查询表 | 内容 | 租户范围 |
| :--- | :--- | :--- |
| `seller_items` | 订单商品行、价格与运费（整数分） | `seller_id` 等于已授权卖家。 |
| `seller_orders` | 对应订单的状态、日期、买家城市与州 | 订单包含当前卖家至少一条商品行，每订单仅一行。 |
| `seller_products` | 当前卖家已售商品及品类 | 通过当前卖家商品行关联。 |
| `seller_profile` | 卖家城市与州 | 仅当前卖家。 |

Olist 原始 `order_items`、`orders`、`order_payments`、`order_reviews` 等表对分析 SQL 不可见。尤其不能把跨卖家订单的整笔付款归给单个卖家。示例字段释义和指标口径维护在 [metadata.yaml](../examples/olist/metadata.yaml)。这些表名不是 NexRAG 核心的固定白名单。

## 执行边界

SQLite 适配器以只读方式打开源文件；PostgreSQL 适配器使用只读事务，部署时还应使用仅有 `SELECT` 权限的账号。每个视图的 `source_sql` 必须输出 `__tenant_id`，适配器在复制行前强制按已授权的 `tenant_id` 过滤，然后仅在独立的内存数据库执行 Agent 生成的 SQL。SQLGlot 拒绝多语句、写操作、源表与跨库引用；SQLite 再限制可执行的分析函数、执行时间与结果行数。数据源配置是部署者维护的受信任文件，部署者必须确保 `__tenant_id` 映射到正确的业务归属。

错误类型：`UnsafeQueryError` 表示 SQL 不符合解析和表访问约束；`ValueError` 表示租户不存在、参数无效、函数不可用或结果超出行数；`TimeoutError` 表示查询超时；SQLite 数据文件不存在时为 `FileNotFoundError`，PostgreSQL 无法连接时为 `ConnectionError`。HTTP 层将源不可用转换为 503。
