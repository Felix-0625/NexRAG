# RAG 与原生运行验收

审查日期：2026-10-29。范围为原生启动、文档摄入、Qdrant 多通道检索、Child 重排和 Parent 展开。此报告不将服务层测试当作真实前端业务验收；账号/知识权限、业务问答的前端证据由总验收记录单独列出。

## 方案与当前实现

| 要求 | 审查结果与修复 | 验证层级 |
| --- | --- | --- |
| 单个 Qdrant 保存 Child 向量及 Parent 正文 | 网页摄入复用 Haystack 分块器，仅输出叶子；`parent_id` 与 `parent_text` 在 Child 的 `meta` Payload。父块没有独立向量或外置父库 | 实际摄入管线测试通过 |
| Dense + BM42，Qdrant RRF | 两侧复用 FastEmbed BM42，使用 Qdrant 原生 Prefetch / Fusion.RRF，两个分支均带 `meta.collection_name` 权限范围过滤 | 检索基座测试和真实组合服务辅助检查通过 |
| BM42 的索引和查询语义 | 网页新建集合启用 Qdrant IDF；旧集合启动时原地升级 modifier，不重建或清空。查询改用官方 `query_embed` 的单位词权重，文档摄入仍用 attention 权重 `embed` | 新建索引、真实内存存量升级/幂等/保留向量和 Payload、查询 API 回归通过；实际运行摄入 HTTP 的 IDF 已确认，查询代码真实入口已验证，常驻检索服务载入由总任务重启后确认 |
| 重排后只取 Child Top-3，再展开 Parent | 修复原先保留全部候选、展开父块后才限制结果的顺序；原生配置固定 cross-encoder `replace` 排序、Top-3。按知识库/文件/Parent ID 去重，同父兄弟入选后可少于三份，不补弱父块 | 新增明确顺序和兄弟去重回归通过 |
| 低分剔除 | BGE logits 转 sigmoid；原生示例阈值 `RERANK_MIN_SCORE=0.05` 可调。拒绝非数字/非有限分数以及重复/缺失候选索引 | 低分过滤与异常回归通过；不是语料质量的普适阈值 |
| 重排不可用时不能冒充完整 SOP | `RERANK_REQUIRED=true`，原生启动等待 BGE 就绪；必要重排报错时明确失败，不返回伪装已质检的粗召回。兼容基座非严格配置保留可选降级 | 故障用例通过；真实启动由父任务统一验收 |
| 新旧数据兼容 | 新摄入 `parent_text`；读取兼容旧 `parent_content`；响应元数据剔除重复父正文。没有 Parent 信息的旧块仍返回原片段 | 新旧字段回归通过 |
| 相同内容跨私库/文件隔离 | 修复原生 CLI 仅按内容生成 point ID 的碰撞，身份包含逻辑知识库与 file_id；相同字节不再覆盖其他库 | 身份回归通过 |
| 原生 CLI 更新控制 | 新写入含版本标识，全部批次确认后仅清理同库同文件旧版本；补齐 BM42 Qdrant IDF 和 `QDRANT_API_KEY` 传递 | 已实现、已增加真实 Qdrant 内存存储测试，最终结果由总回归核对 |

## 已运行验证

- 最新根项目原生专项 31 项通过：原生桥、原问题保留、启动器禁云配置、Child 切分隔离和 CLI 实际 Qdrant 内存写入/版本替换。
- `base/ingestion-service` 最终全套 389 项通过（2026-10-29），包含真实存量 IDF 升级用例；`test_pipelines.py` 与 `test_bm42_idf.py` 专项 62 项通过。覆盖真实分块器和 Qdrant 内存索引，而非只断言配置常量。内存模式不实现 Payload 索引性能优化，因此不拿它证明远程服务器索引性能。
- `base/retrieval-agent` 查询编码修复后全套 179 项通过（2026-10-29）；其中先跑检索/重排/混合查询专项 49 项通过。
- 使用本机已缓存 `BAAI/bge-reranker-base` 对合成候选真实评分：中文销售折扣制度 0.998134、无关项目预算 0.001018、天气 0.000037。此小样本确认模型可加载、能区分所构造正负样本，不能代替目标语料 benchmark。
- 维护的根 Python 文件 Ruff 通过；当前 WebUI 补丁 35 个 Python 文件 AST 通过。三补丁链在临时 Git index 从固定 HEAD 完整应用，与当前 80 个改动文件逐一规范化比对一致；重复准备脚本成功识别完整状态。真实 WebUI main 在独立空 DATA_DIR 完成迁移与导入（82 路由），原生桥符号导入成功；不启动生产服务，不接触真实账号库。
- 最终服务启动后，真实 GET Ollama `/api/status` 确认为 `cloud.disabled=true`，使用一次性命名 Qdrant 合成集合真实调用 bge-m3、BM42、BGE：`native_hybrid=true`，Top-3 子块展开去重得到 1 份制度父文，首条相关分数 0.9992098155567657，制度问句没有混入 B 私库文本。脚本退出 0 并清除自身集合，不读取或删除已有知识。更早旧服务离线的一次尝试未记为通过。
- 可重现脚本 [verify_rag_native.py](../../scripts/verify_rag_native.py) 的加强版本于 2026-10-29 实际执行退出 0：使用 B 原文精确查询、只授权 A 时不泄露；授权 B 时能检索同一内容，避免空结果造成假通过。真实集合 IDF、两个 Child 向量和 Parent Payload 断言通过，首条分数 0.9992098155567657，Top-3 Child 展开后为 1 个去重父块。脚本通过检索服务的真实 Python 入口调用本地模型与 Qdrant，不覆盖网页上传、应用登录或账号授权管理，完成后已清理自身独立集合。
- 对正在运行的原生摄入 HTTP 另做 V1 → V2 合成 Markdown 上传，实际写入 6 个 Child；检查当前物理集合的 BM42 IDF、Child dense/sparse 双向量、Parent 正文和 ID、仅本次逻辑集合的元数据，以及同文件覆盖后旧 ingest_version 已无向量。最后通过摄入删除接口只清理自身随机 UUID 文件，再计数确认 0。此项退出 0，验证真实摄入服务和 Qdrant 写入/更新契约，属于服务层辅助证据，不冒充浏览器验收。

受限环境运行 pytest 曾出现 Windows 临时目录拒绝访问；以明确隔离目录申请正常提权后，31 项已通过。更早有一次自动审批审查因工具用量上限未运行，没有绕过；最新结果使用实际成功运行记录。

## 原生运行与移除部署交付物

项目对外启动入口为 `start.cmd` / `start.ps1`。WebUI 准备流程改用 `patches/open-webui/v0.11.4/nexrag/`，仅按 tenant-selector、ops-dashboard、workspace、security-npm 四个本项目补丁顺序应用，其中 security-npm.diff 只修改 `package.json` 与 `package-lock.json` 以升级有安全修复的前端依赖。README、local-run、数据库接入与诊断文档已经移除容器部署操作。

已按明确单文件清单移除根编排配置、`deploy/nexrag/Dockerfile`、三个基座的构建/编排/Taskfile 入口，以及旧编排基座中的六个 compose、Taskfile 和两个构建文件。没有操作用户机器上的镜像、容器、运行卷或其他项目。

2026-11-01补查交付文件清单时，发现搜索基座仍有三个未被原生入口引用的旧部署文件：`.docker/data/.gitignore`、`.docker/data/README.md`和`.docker/searxng/settings.yml`，已逐项移除。可选搜索仍通过用户配置的`SEARXNG_URL`访问，不依赖这些文件。

最初整目录递归清除旧编排基座被自动审批拒绝：仅迁移六个文件不足以证明其余资产都可丢弃。替换旧集成后重新核实仓库引用，确认历史资产不再被运行脚本与业务文档引用；按明确清单将剩余 67 个历史文件备份至忽略的本机 `.local-run/`、记录各路径与 SHA256，然后用 PowerShell 原生 LiteralPath 逐文件清除，验证目标绝对路径均位于旧基座目录内。没有执行整目录递归删除。根 `.dockerignore` 同步移除；旧基座现无文件。

上游配置变量 `USE_SLIM_DOCKER` 是 Open WebUI 对减依赖运行配置的既有命名；原生 slim Python 仍需该开关，不会调用运行引擎。旧安装 `.env` 中的 `host.docker.internal` 仅做兼容归一化，不改写用户文件，新模板已使用回环地址。

## Ollama 本地推理的证明

回环 URL 也可能代理云模型。原生启动新增 `/api/status` 检查，只有明确 `cloud.disabled=true` 才复用现有默认端口；否则启动本任务管理的可用回环端口，强制 `OLLAMA_NO_CLOUD=1`，再次验证状态后才启动可能发送业务内容的后端。原有 Ollama 不被停用、全局配置不被修改。

本轮验证地址通过 `NEXRAG_MANAGED_OLLAMA_API_BASE` 提供给核心，`.local-run/ollama-trust.json` 记录运行元数据；模型是否仍是云端引用还必须由模型注册表验证。新增状态/owned 环境测试，最终运行验收由主任务统一补齐。

默认 `ENABLE_PLUGINS=false`；仅部署者设置 `NEXRAG_ALLOW_TRUSTED_PLUGINS=true` 才开放任意 Python 源码插件。它没有操作系统沙箱，开启等于信任安装代码，不能把应用 ACL 描述成对主机所有者的隔离。

## 工业方案、性能和开源边界

- 复用 [Qdrant Hybrid Query API](https://qdrant.tech/documentation/search/hybrid-queries/) 的多路预取和 RRF，未自造检索融合服务。
- [BM42 官方示例](https://qdrant.tech/articles/bm42/) 同时指定 IDF modifier 与 `query_embed`；[FastEmbed BM42 实现](https://github.com/qdrant/fastembed/blob/main/fastembed/sparse/bm42.py) 的查询词权重为 1，文档词权重来自 attention。两者接口不能互换。
- [Qdrant BM42 原文](https://qdrant.tech/articles/bm42/) 当前明确更正过 benchmark，并将其列为实验性方法，不能宣称优于其他 BM25。按用户拍板保留方案，但中文、行业词召回质量仍需独立 holdout 数据集。
- [BAAI BGE Reranker 模型卡](https://huggingface.co/BAAI/bge-reranker-base) 提供 cross-encoder 用法、中文/英文能力和 MIT 许可。分数用于排序，阈值需按实际语料校准。
- [Haystack HierarchicalDocumentSplitter](https://docs.haystack.deepset.ai/docs/hierarchicaldocumentsplitter) 是父子结构参考；本项目复用框架 splitter，再将 Parent 合并进叶子 Payload，避免额外父库组件。
- [Ollama FAQ](https://docs.ollama.com/faq#how-do-i-disable-ollamas-cloud-features) 给出禁云变量；[官方路由实现](https://github.com/ollama/ollama/blob/main/server/routes.go) 提供 `/api/status` 与 `cloud.disabled`。
- “一次 RPC”限于单次 Qdrant候选查询带回父正文，并不代表整个检索只有一次网络调用。首次能力探测、Embedding、多个查询和重排仍有各自开销；不能无 benchmark 承诺秒级。

**旧补丁来源问题已以功能替换处理：**两个没有明确再分发许可的第三方补丁从准备链与交付文件中移除，旧生成分支通过三方对照恢复为固定上游与本项目修改；独立编写本机 bridge 对接现有摄入/提取/删除 HTTP 契约，保留账号/私库检查与发布路由。没有只改文件名或把旧表达并入新补丁。准备链已进行临时 Git index 全量应用对照；具体最终测试见总验收结果。[来源与替换说明](../../patches/open-webui/NOTICE.md) 保留原始项目归属。此项不再依赖取得旧补丁授权，也不代表其余依赖均完成法律审查。

Open WebUI 固定版本源码的 LICENSE 已核对，保留版权与来源，品牌条款包括滚动 30 天不超过 50 用户、书面许可、企业许可等例外；默认界面保持 Open WebUI 标识，不依赖用户数例外宣称可以无限制去品牌。模型权重、Olist 示例数据与其他上游代码还各有自己的许可，不能统一宣布可商用。
