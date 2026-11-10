# 本地运行与自有数据接入

NexRAG 以原生进程运行 Open WebUI、单 Agent 后端、AarhusAI 文档摄入与检索服务、Qdrant 和本地 Ollama。业务数据来自指定的 SQLite 文件或 PostgreSQL，知识与记忆向量存储在 Qdrant。没有容器部署步骤。

## 一条命令

支持 **Windows x64**。需要 Git 和首次准备时的网络连接。在源码包解压目录或仓库根目录执行：

```powershell
.\start.cmd
```

无需预装 Python、Node.js、uv 或 Qdrant。缺失的运行包下载至项目目录，不修改系统 PATH。启动器使用独立 Python 3.12 环境；默认对话模型 `qwen3:4b-instruct`，Embedding `bge-m3`。

建议将项目放在 `C:\NexRAG` 等短路径。旧发布包首次准备时若出现 `Filename too long`，请把源码包重新解压到短路径再运行。

本机地址不等于本地推理：Ollama 也能转发云模型。启动器仅复用 `/api/status` 明确返回 `cloud.disabled=true` 的已有 Ollama，否则在可用回环端口启动自己管理的 `OLLAMA_NO_CLOUD=1` 进程，并再次验证状态。不会改全局 Ollama 配置或关闭其他服务。`.local-run/ollama-trust.json` 记录本轮地址与验证时间；旧版不支持验证时拒绝启动，而不是宣称机密数据可以安全外发。[Ollama 官方禁云说明](https://docs.ollama.com/faq#how-do-i-disable-ollamas-cloud-features)。

启动器依次准备固定版本的网页源码与补丁、依赖、缺失的本地服务密钥、业务示例、Qdrant、摄入/检索与网页。首次下载依赖和模型可能较慢且占用数 GB；后续复用缓存。严格检索配置会等待本地 BGE cross-encoder 加载完成，不能把仅有粗召回的状态当成重排已就绪。

```powershell
.\start.cmd -NoBrowser    # 不自动打开网页
.\start.cmd -PrepareOnly  # 仅准备环境，不启动服务
.\start.cmd -SkipModels   # 跳过 Ollama 模型下载，仍需已配置的可用模型
```

所有服务监听 `127.0.0.1`。默认网页为 `http://127.0.0.1:3000`，若端口占用则一起选取可用端口、同步服务间 URL。终端与 `.local-run/runtime.json` 记录实际地址。待终端显示 `All local services ready` 再使用完整对话与检索功能。

保持终端打开；Ctrl+C 关闭本次启动的进程，保留数据。同一仓库只允许一个启动器，不关闭其他项目或已有 Ollama。启动失败查看 `.local-run/logs/<服务名>.log`，修复配置后重启；不需要清理账号或重建数据。

服务创建或进程隔离失败会报告具体服务、退出码（已退出时）和日志路径。Windows 在子进程已经退出后调用 Job Object 分配也可能返回“拒绝访问”；启动器会区分该状态与仍存活进程的隔离拒绝。两者都不会被当成就绪，也不会通过提权或关闭进程隔离来绕过。

## 启动器维护边界

`launch_local.py` 保留 CLI、依赖准备及整体启动顺序；`scripts/native_environment.py` 管理密钥/服务环境与回环端口，`native_ollama.py` 管理本地禁云检查和模型准备，`native_processes.py` 管理仅本次创建的进程树。分模块不改变 `start.cmd` 用法或数据路径。开发检查使用 `tests/test_local_launcher.py` 验证隔离与进程生命周期；完整全新 clone 启动仍需独立验收。

## 数据位置

| 位置 | 内容 |
| --- | --- |
| `.local-run/logs/` | 各服务日志；不能提交业务正文或密钥到公开仓库 |
| `.local-run/venvs/`、`tools/`、`.local-run/cache/` | 独立运行环境、运行包和模型缓存 |
| `data/openwebui/` | 账号、会话、知识元数据和上传文件 |
| `data/qdrant/` | 文档、记忆和经验向量 |
| `data/ops/` | 权限、模型配置、评测及版本治理元数据 |
| `data/artifacts/` | 默认产物目录，可由管理员在网页绑定其他文件夹 |

运行数据和模型缓存被 Git 忽略。生产资料与真实凭据不应出现在仓库、补丁、演示图或测试夹具中。

## 网页和知识库

Release 源码包包含前端准备脚本和五个补丁。首次运行 `start.cmd` 时，`scripts/prepare-webui.ps1` 会联网检出 Open WebUI `v0.11.4` 的固定提交，按序应用 tenant-selector、ops-dashboard、workspace、security-npm 和 fonts，随后删除无用的 Vazirmatn TTF；启动器再安装依赖并启动网页。生成的 `webui/` 在本机复用。维护前端时，应把修改同步到 `patches/open-webui/v0.11.4/nexrag/`，以便新用户从发布包重建相同界面。来源与许可见 [NOTICE](../patches/open-webui/NOTICE.md)。

个人库通过“工作空间 → 知识库”创建。私人库只能由所有者或明确获得授权的账号访问，管理员身份不构成旁路。企业库由管理员维护并按授权下发，员工引用同一个库，管理员内容更新后无需复制到每个员工账号。知识权限和业务数据库范围是两套独立授权。

上传和更新经本项目原生桥调用本机摄入服务，传递文档所属库和所有者、验证认证结果，再更新网页处理状态。删除仅作用于指定库的文档副本；同文件在其他库的副本独立保留。聊天检索由核心 Agent 调用工具，不经过网页内的第二套检索循环。支持有文字层的 PDF、DOCX、UTF-8 文本/Markdown/CSV；扫描件需要 OCR，其他二进制格式先转换。版本增删与发布生成历史记录、清单和原文件 ZIP，撤权同时阻止历史访问。版本回滚能力以最新功能与验收记录为准，不能把历史下载当成恢复版本。

## 业务数据接入

管理员在“数据中台 → 业务数据库”测试并保存 SQLite 或 PostgreSQL 连接，选定业务表、租户字段和访问范围，再授予员工。连接成功不等于授予访问权限。账号、组织结构和授权入口见 [工作空间](workspace-ui.md)。

需要自定义视图/指标时配置以下文件，详细约束见 [数据库接入](connect-business-database.md)：

| 配置 | 含义 |
| --- | --- |
| `BUSINESS_SOURCE_PATH` | 数据源和允许的只读视图配置 |
| `METADATA_PATH` | 可见字段、单位和指标口径 |
| `TENANT_ACCESS_PATH` | 初始业务范围及授权配置 |
| `BUSINESS_DB_PATH` | 可选 SQLite 文件路径覆盖 |
| `BUSINESS_DB_DSN` | PostgreSQL 只读连接凭据，不提交到仓库 |

源业务库只能读取；Agent SQL 只在授权数据的独立分析快照执行。PostgreSQL 应由 DBA 提供专用只读角色，有写入/建表/管理能力的凭据会被拒绝。数据源和权限修改后需要以真实不同账号重新核对，禁止依靠模型遵守租户条件。

## 检索配置

原生新安装默认启用四路召回。BGE-M3 稠密语义、BGE-M3 稀疏词项权重、标题与文件名和文档结构链路分别产生候选，并按点 ID 用加权 RRF 融合。文件名使用 Qdrant 多语言文本索引取有界候选，再按词项匹配排序。结构路查询章节标题，沿命中的文档检索其章节，并沿父块序号查找相邻父块。所有结构请求均限定同库、同文件、同版本。

设置 `ENABLE_GRAPH_RAG=true` 可额外启用实体 GraphRAG。它为每个 Qdrant 物理集合中的知识库分别保存 SQLite 图，使用原文实体、关系和出处。查询仅打开授权知识库的图，每条路径最多两跳，边界限定在同一个知识库。多库查询融合各库候选。路径中的每条关系都会从 Qdrant 核验知识库、文件、版本和原文哈希。图文件放在 `GRAPH_STORE_PATH` 对应的 `.scopes` 目录，启用时摄入服务启动补齐旧库图数据，新上传和删除会更新对应图。 [通道消融实测](acceptance/rag-quality-20261109.md)

本地 BGE 对融合后的 Child 重排，选出最多 `min(请求 K, RERANK_TOP_K)` 个 Child，默认展开 Parent 并按库、文件和 Parent 标识去重。多个 Child 命中同一个 Parent 时，实际返回的 Parent 数会少于 K。知识正文总预算为 12,000 字符。摄入启动及首次入库时会补齐索引，旧库升级保留现有向量和原文。未启用五路、索引尚未就绪或选用 DBSF 时沿用原检索路径。现有的中文 BM25 实验开关默认关闭。本机受管 Qdrant 版本为 1.19.1。

| 环境变量 | 默认与作用 |
| --- | --- |
| `QDRANT_INDEX` | 新安装默认 `nexrag_knowledge_bge_m3`，摄入与检索共享的物理集合 |
| `EMBEDDING_MODEL` / `EMBEDDING_DIM` | `bge-m3` / 1024；换模型或维度需新建索引并重新摄入 |
| `ENABLE_SPARSE_EMBEDDINGS` / `ENABLE_HYBRID_SEARCH` | `true` / `true`，写入与检索必须配套 |
| `ENABLE_FILENAME_RETRIEVAL` | `false`。实验配置，上传时自动提取文件标题，供稀疏索引和重排使用。比较时通过正常上传摄入到独立集合，避免混用两种稀疏向量 |
| `SPARSE_MODEL` | 新安装默认 `BAAI/bge-m3`，两侧须使用相同模型 |
| `CHUNK_SPLIT_BY` | `token`，复用 tokenizer 和递归切分器 |
| `ENABLE_PARENT_CHILD_CHUNKS` | `true`，只索引叶子；父正文内嵌 |
| `PARENT_CHUNK_SIZE` / `PARENT_CHUNK_OVERLAP` | 1200 / 0，与当前切分单位一致 |
| `PARENT_CANDIDATE_MULTIPLIER` | 3，扩大粗筛 Child 候选 |
| `INITIAL_RETRIEVAL_MULTIPLIER` | 3，允许 1–10；启用重排时，候选数为请求 K 乘以两个倍率的较大值 |
| `RETRIEVAL_CANDIDATE_LIMIT` | 可选 1–200；覆盖线性路径的候选数，独立于返回 K |
| `RERANKER_MODEL` / `RERANKER_THREADS` | `BAAI/bge-reranker-base` / 2，本地 cross-encoder |
| `ENABLE_FIVE_CHANNEL_RETRIEVAL` | 原生默认开启多路融合；同时关闭该项与 `ENABLE_GRAPH_RAG` 会切回两路 |
| `ENABLE_BODY_LEXICAL_RETRIEVAL` | 原生默认关闭正文精确词法；开启后保留其独立候选与排序权重 |
| `RERANK_FUSION` / `RERANK_RRF_K` | `replace` / 60；选 `rrf` 时融合粗召回排名与 cross-encoder 排名 |
| `RERANK_TOP_K` | 3，允许 1–20；先保留 Child 再展开 Parent |
| `RERANK_DEDUP_BEFORE_LIMIT` | 默认关闭；开启后按重排顺序先合并同一 Parent 的命中，再限制返回数量 |
| `HYBRID_FUSION` | 原生 Qdrant 候选融合，`rrf`（默认）或 `dbsf`；与重排融合独立 |
| `HYBRID_RRF_K` / `HYBRID_DENSE_WEIGHT` / `HYBRID_SPARSE_WEIGHT` | 原生 RRF 常数 2、两路权重各 1；需在开发/验证分区评估，`dbsf` 不使用这些值 |
| `ENABLE_CONTEXT_WINDOWS` / `CONTEXT_WINDOW_SIZE` | 默认关闭 / 1；正常摄入时为每个 Child 自动保存左右邻接块的原文窗口与位置，需启用父子分块 |
| `RETRIEVAL_CONTEXT_MODE` | `parent`、`matched_child` 或 `window`；窗口模式返回自动保存的邻域原文，旧索引缺少窗口时返回 Child 并标记 `context_window_missing`；位置指向解析后正文 |
| `KNOWLEDGE_TOP_K` | 可选 1–20；设置后覆盖部署中的 RSI 参数值。未设置时读取已生效参数，初始值为 3 |
| `RERANK_MIN_SCORE` | 0.05，BGE logit sigmoid 后的分数；是可调过滤线，不是正确率保证 |
| `RERANK_REQUIRED` | 原生启动固定 `true`；重排不可用时报告失败，不伪装经过质检 |

窗口模式沿用[入库保存邻域、检索后替换上下文](https://developers.llamaindex.ai/python/examples/node_postprocessor/metadatareplacementdemo/)的设计，复用现有分块器，不额外引入 Agent 框架。窗口仅用于证据返回，不加入 Embedding 输入。开启后通过正常上传重新摄入即可；比较分块、窗口和融合配置时使用独立知识库。候选参数尚未通过完整发布验收，不能据此宣称达到医疗或法律领域的质量要求。
| `RETRIEVAL_TIMEOUT_SECONDS` | 45 秒，单次知识查询预算 |

高要求场景的分项评估见 [RAG 质量验收](acceptance/rag-quality-targets.md)。

可在 `.env` 中加入以下候选配置，重启后试用。此配置最多粗召回 80 个 Child，重排保留 16 个。回退时删除 `KNOWLEDGE_TOP_K`，并恢复原有重排配置。

```ini
ENABLE_HYBRID_SEARCH=true
INITIAL_RETRIEVAL_MULTIPLIER=5
PARENT_CANDIDATE_MULTIPLIER=3
KNOWLEDGE_TOP_K=16
RERANK_TOP_K=16
RERANK_FUSION=rrf
RERANK_RRF_K=60
RERANK_MIN_SCORE=0.05
```

旧索引 `parent_content` 仍兼容读取，新摄入使用 `parent_text`，响应元数据移除重复父正文。没有父信息的旧块保持原片段返回。不要原地将密集索引改为不同维度/不同模型的混合索引；使用新集合名并重新摄入，保留旧资料后再核对。

BGE-M3 的稀疏 Provider 已接入摄入和查询。原生环境中设置 `SPARSE_MODEL=BAAI/bge-m3` 会为两端选择该 Provider。模型固定在官方版本 `5617a9f61b028005a4858fdac845db406aefb181`，首次使用时下载 ONNX 编码器、tokenizer 和稀疏头。可通过 `BGE_M3_MODEL_PATH` 指定该版本的本地完整目录。两端共用原始 token ID 的正词项权重，使用 ReLU 投影、同 token 最大值聚合和特殊 token 过滤。

稀疏模型身份保存在 Qdrant 集合元数据中。已有安装继续读取配置中指定的模型和集合，旧版隐式 BM42 集合也继续使用原配置。切换 BGE-M3 前须在独立集合完整重建稀疏向量。模型身份不匹配的查询和摄入会报错。 [验收记录](acceptance/rag-quality-20261109.md) · [官方模型说明](https://huggingface.co/BAAI/bge-m3)

## 外部能力与诊断

### 本地语音与产物目录

原生启动保留精简 WebUI，并启用本地 Whisper 转写（`NEXRAG_LOCAL_STT=true`、CPU/int8）。启动命令安装锁定的 faster-whisper 与 PyAV，首次转写自动下载所选语音模型，缓存位于 `.local-run/cache/whisper`；录音在本机处理。管理员可在语音设置中选择模型或填写已下载模型的绝对目录。浏览器朗读使用系统语音，与语音转文字分别配置。

在“设置 → 产物文件夹”绑定本机绝对目录。生成报告时，文件直接写入该目录的账号/会话子目录，网页同时提供受账号授权保护的访问入口。Markdown 知识摘要使用正文导出；结构化表格行必须来自本轮真实数据工具结果，不能用改写后的字段绕过来源校验。

外部搜索需要明确配置的搜索服务，启动器不部署搜索服务器。对外查询可能携带业务信息，是否可调用必须经过数据分级和外发策略，不能将“只做搜索”当作天然安全。外部模型、工具、文件操作也分别执行授权边界。

默认关闭任意 Python 源码插件。只有部署者显式设置 `NEXRAG_ALLOW_TRUSTED_PLUGINS=true` 才开启上游插件入口，业务管理员身份本身不能解除这个部署限制。任意源码工具没有操作系统沙箱，开启等于信任部署者安装的代码；应用账号/知识库 ACL 不构成对主机所有者或受信代码的加密隔离。核心 SQL、知识检索、受控文件读取与导出不依赖源码插件入口。

运行 `powershell -File scripts/doctor.ps1` 查看本地服务状态；端口改变时查 `.local-run/runtime.json`。在网页用两个不同权限账号分别执行文档查询、SQL、联合分析、导出与撤权验证。HTTP 200、模型连通和测试通过不能替代真实业务验收。

服务准备完毕后，可以另开终端运行 `.local-run/venvs/retrieval/Scripts/python.exe scripts/verify_rag_native.py`。它使用独立的合成集合验证真实 Dense/BM42/RRF/BGE 和 Parent 展开，并以 B 私库原文检查只授予 A 时的过滤；仅清理本次创建的集合。运行时不要并发性能验收或模型问答，以免抢占同一本机模型资源。

历史及最新结果见 [前端验收](frontend-acceptance.md)、[RAG 与原生交付验收](acceptance/rag-native.md)。

更新项目补丁后，启动器会比较生成的`webui/`与固定上游加当前补丁的完整预期结果。完全一致时不重建；若旧生成源码与新补丁不一致，先将旧源码完整保留在`.local-run/webui-updates/<标识>/previous/`，再从本机已验证的固定Git提交重建并应用新补丁。不会用`git reset --hard`或`git clean`删除维护者改动；日志给出旧目录位置。仅移动生成的`node_modules`缓存到新目录，依赖指纹仍会检查，应用账号和知识数据不在此源码重建范围内。
