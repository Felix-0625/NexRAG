# 可复现的 NexRAG 质量与性能验收

正式阈值、K=10、分母、语言分组、引用定义、负载和人工独立性见 [protocol-v1.json](protocol-v1.json)。没有全部实测通过就不完成目标。训练、选择、留出、外部校准和安全用例分别记录；失败与无法核验项不丢弃。

## 本次数据与源码

`prepare.py` 固定 CMRC2018 与 PubMedQA 的发布提交，核对官方 LegalBench-RAG 下载包 SHA256。全部 2,310 份原文独立上传：848 份 CMRC 上下文、1,000 份研究摘要上下文、462 份 CUAD 合同。原题、答案、原文坐标只在评分目录，绝不写入知识库。LegalBench-RAG 使用发布方已生成的固定题库，其问题构建包含 LLM；本轮没有生成替身合同或替身题目，也不把该题库称为人工独立评审。原始下载入口见 [发布方仓库](https://github.com/zeroentropy-ai/legalbenchrag)。

当前复现目录为 `.local-run/quality-20261109/public-v4/`，每组 Train 60、Selection 60、Holdout 300，留出每题不同来源组。`manifest.json` 固定资源哈希、文件和题目清单。CMRC 无有效原文坐标的题保留为不可核验；PubMedQA 缺失的结论不补进语料。该医疗组仅研究 QA，不证明临床安全。

`runtime.py` 使用独立临时数据目录、动态端口和随机服务凭据，只结束本次创建的进程；不打开生产数据库。`loaded-source-epoch.zip` 保存启动源码，后续修改不会覆盖旧轮证据。恢复开发轮明确记录密码夹具重置、失败原文重试和新源码，不计作全新正式验收。凭据文件在忽略目录，不能公开。

## 执行入口

从项目根目录以已安装对应依赖的 Python 执行 `python -m evals.quality.<模块> --help` 查看完整参数。

| 模块 | 实际边界与证据 |
| --- | --- |
| `prepare` | 下载/验证发布原件，按来源分区；不接触模型输出 |
| `run_native` | 从空库通过 WebUI 上传全部原文、核对下载字节，实际 `/search` 与生产聊天 HTTP |
| `resume_native` | 仅延续已停止的本轮隔离开发数据，原失败保留；全部原文复核后再查询 |
| `worker` | 运行实际 Agent/检索服务，旁路保存该轮模型可见知识工具消息；不注入参考答案 |
| `evaluate_retrieval` | 评分实际 HTTP 回执；区间覆盖、固定 Parent 单位排序、缺失项分别记录 |
| `case_plan` / `run_evals` / `http_gateway` | 复用现有 Evals/Inspect；网关真实转发 `/search`，参考答案不发给应用；本轮基线只消费 Train/Selection |
| `calibrate_judge` | 固定 RAGTruth 原件及外部人工标签，先保留 40+40 个来源隔离案例；仅作外部评委校准 |
| `judge` | 原版 Ragas 0.4.3 五项指标，正确性权重 0.75/0.25；保存每次真实模型请求/回复及解析错误 |
| `prepare_human_review` | 从实际聊天和本轮授权上下文生成本地盲审题包。把未取得回执的题保留在协调索引中 |
| `citations` | 在实际答案中绑定事实摘录、局部 KB ID、本轮授权上下文、版本及原文引句；语义判断仍有 LLM 限制 |
| `run_load` / `http_load` | 每端点 C1/C5/C10 各 1,000 次完整 HTTP；记录首请求、20 次预热、每次失败和 P95/P99；无查询/答案/嵌入/重排分数替身缓存 |
| `gate` | 必需分组、足量独立来源、严格不等式、真实人工、安全检查、完整清单，缺一不可 |
| `report` | 汇总真实回执；开发数字不升级为正式通过 |

`run_evals` 的 Evals 业务规则通过率不是 Recall 均值。原始 Recall/MRR/nDCG 与评分覆盖数另存 `metrics.json`。布尔边界夹具仅验证规则解释器，不是模型实测或人工一致率。

## 当前自动流程

`overnight.py` 在整份语料与真实生成完成、Evals 实验终止后，串行执行原文区间评分、外部人工标签校准、实际生成评分及两端点负载。阶段、退出码、日志和失败随时保存，自动刷新 [验收记录](../../docs/acceptance/rag-quality-20261109.md)。

独立人工尚未产生，本轮一致率保持未测。医疗、法律安全和权限用例、正式完整留出仍须实际通过。现有 `pytest` 和补丁验证只证明回归，不证明达到全部质量或性能指标。

人工评审者各自领取 `instructions.md`、`review-items.jsonl` 和空白的 `review-template.jsonl`。
协调者单独保管 `coordinator-index.json` 和 LLM Judge 结果。协调索引绑定题单、真实 HTTP、逐题
工具轨迹及盲审内容的哈希，也保留不可评审的题。准备题包不会生成任何人工分数。

知识专用部署可显式设置 `REQUIRE_KNOWLEDGE_EVIDENCE=true`。如果本轮有授权知识范围、
没有选业务租户、没有其他明确的工具义务，且当前指令允许使用知识检索，证据账本将要求本轮调用
知识工具。该模式不扩大授权范围，缺少上下文和拒答仍按原口径记录。

## 中文词法检索

在检索服务设置 `ENABLE_ZH_LEXICAL_RETRIEVAL=true` 可开启线性 Parent 模式下的中文正文 BM25。路由对一至三字词项建立 SQLite FTS5 索引，候选 ID 经过当前 Qdrant 范围过滤后再返回原文。索引按当前授权范围构建，使用 `BM25_MAX_DOCS` 和 `BM25_CACHE_TTL_SECONDS` 控制文档数和刷新。首请求包含建索引耗时。中文以外的请求、非 Parent 模式和词法无结果的请求沿用已有检索路径。

实测证据见 `zh-lexical-real-http-01/`、`evals-zh-lexical-01/` 和 `zh-lexical-holdout-01/`。正式留出包含两条未评分记录，完整状态见验收记录。
