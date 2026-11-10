# NexRAG 高要求检索验收

此表将项目验收要求固定为严格优于给定阈值。引用要求的上限是 100%，验收要求全部通过。医疗和法律领域各自冻结语料、用途、版本和风险分组，分别给出结果。

| 指标 | 项目门槛 | 评分口径 |
| --- | --- | --- |
| Recall@10 | > 0.90 | 每题前十个不同检索单元覆盖的标注正例比例，按题平均 |
| MRR | > 0.90 | 按返回顺序计算首个相关单元排名的倒数，无命中计零。榜单如定义 MRR@10，则单报该口径 |
| nDCG@10 | > 0.85 | 使用原始相关性分级，二元数据使用二元标注 |
| Context Precision | > 0.80 | 以实际提供给生成器的有序上下文评分，采用有参考证据的 Ragas 定义。另报返回片段的相关比例 |
| Context Recall | > 0.85 | 参考答案的原子事实能否从实际提供的上下文得到支持。多跳题额外报告必需证据是否找齐 |
| Faithfulness | > 0.95 | 已输出的原子事实有充分证据支持的比例 |
| Hallucination Rate | < 0.01 | 按最严格档执行。任一事实无法得到支持、与可靠证据矛盾或引用虚构即计入该条回答。人工复核争议项 |
| Answer Relevancy | > 0.85 | 使用固定评分器、版本和提示词，独立报告人工相关性评分 |
| Answer Correctness | > 0.85 | 按领域标注核对结论、必要条件、例外和数值，记录错误严重程度 |
| Groundedness | > 0.90 | 逐题核对结论是否建立在获授权且适用的证据上，独立于逐事实平均分 |
| Citation Accuracy | 100% | 每个事实都有引文，引用 ID 属于本轮检索集，原文位置和版本可查，原文支持具体陈述 |
| 检索 P95 | < 500 ms | 包含嵌入、索引访问、重排及上下文选择。预注册硬件、并发、数据规模与缓存策略 |
| E2E P95 | < 5 s | 从用户请求到最终校验完成，另报首字、P99、持续 QPS、错误率和任务完成率 |
| 人工与 Judge 一致率 | > 85% | 用独立双人标注及争议仲裁集校准，报告一致率、各类错误与严重漏判 |

拒答单独计分，同时报告可回答题的有效回答率、误拒率及无答案题的错误回答率。严重事实错误、跨租户读取、撤权后泄露、隐私泄露和可执行提示注入均单独作为发布阻断项。领域结果按语言、长尾实体、跨文档、时效冲突和适用人群分组。报告样本量和区间，无法支持结论时记录证据不足。

## 正式验收入口

1. 自动化脚本在隔离环境创建空知识库，通过网页使用的上传 API 提交数据集原始整份文件，保留文件哈希。系统负责解析、分块、向量化、建索引和挂库。脚本等待文件处理完成并出现在知识库中，随即发起检索和问答，无需用户手动建索引或重建库。
2. 使用数据集发布方的原题和证据标注。题目在摄入完成后才进入查询，标注只供评分器读取。脚本不挑选容易命中的文档、不按标注切块、不直接写向量库。使用预建索引的结果单独归为诊断，不计入发布门槛。
3. 自动保存语料版本、配置、逐题结果、引用、失败记录和延迟。公开标注可判定的指标由脚本自动评分。模型评分单独记录评委版本。外部公开人工标注集仅用于评委校准，本轮人工一致率必须来自本轮真实、独立、匿名评审；AI 模仿和外部校准不能替代。缺失证据和超阈值指标均阻断验收。

2026-11-09 的可执行口径见 [`evals/quality/protocol-v1.json`](../../evals/quality/protocol-v1.json)。CMRC 与 CUAD 使用官方原文区间完整覆盖计 Recall；MRR/nDCG 使用查询前冻结的实际 Parent 目录，二元相关性由完整覆盖官方区间推导。PubMedQA 仅称为已提供研究上下文的关联检索，缺失结论不会写回原文。不同 Parent 切分、不同 K、文档 ID 命中和原文区间命中不混报。

## 现有基座和可复用能力

- AarhusAI 线性链路复用 Qdrant 的 Dense/Sparse 双路预取、租户范围过滤、原生 RRF 与可选重排。基座另有带改写、拆问和纠错的检索 Agent。NexRAG 本机部署走线性链路，由外层 Agent 调用工具。[上游实现](https://github.com/AarhusAI/retrieval-agent)
- NexRAG 摄入复用 Haystack 分块器。Child 保存 Dense 与 BM42 向量，Parent 正文随 Child payload 返回。知识库版本发布、索引范围切换和授权刷新可继续复用。[知识库版本](../knowledge-versions.md)
- 先用 Qdrant 的 exact 查询对照 HNSW，分别计算近似索引误差和官方标注召回。存储调整以该对照为依据，旧索引通过现有版本切换路径保留。[Qdrant 候选深度和索引诊断](https://qdrant.tech/documentation/search-tuning/candidate-depth/)
- 参考 Haystack 的相邻句窗口、RAGFlow 的文档结构和引用位置处理。先复用现有分块和原始内容，原文位置无法核验的旧索引只使用已有的定位信息。[Haystack SentenceWindowRetriever](https://docs.haystack.deepset.ai/docs/sentencewindowretriever) · [RAGFlow 数据结构](https://github.com/infiniflow/ragflow/blob/main/docs/references/glossary.mdx)

## 实验顺序

1. 冻结通用公开题和两组领域题。LegalBench‑RAG 用官方原文区间评分，Legal RAG Bench 用于英文端到端，LeCaRDv2 用于中文类案检索。MIRAGE 用于医疗问答，HealthBench 用于医生标注的回答质量。法域、指南版本、权限和对抗测试单独保留。
2. 在同一候选池对比原有链路、父级去重后截断、重排融合选择及原文 Child。指标同时覆盖前十排序、实际上下文以及支持句，不用文档命中替代事实覆盖。
3. 对漏召回的多跳题，在现有 Agent 中按缺失证据补查。先比较一次补查的收益和 P95，再考虑分词、模型、索引或结构化引用关系。对无法确认的证据保留拒答和人工复核路径。[Qdrant 拆问教程](https://qdrant.tech/documentation/tutorials-search-engineering/query-decomposition/)
4. 用 Evals 保存检索证据和生成结果。Ragas 指标及领域人工评分固定各自的定义。对生成器做标注证据与实际证据对照，分别定位检索、证据选择、生成和引用验证。最终以独立留出集、对抗集和实际 HTTP 压测验收。[Ragas Context Precision](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_precision/) · [Ragas Context Recall](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_recall/) · [OWASP RAG 安全指南](https://cheatsheetseries.owasp.org/cheatsheets/RAG_Security_Cheat_Sheet.html)
