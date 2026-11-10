# nexus-agent 检索实现调研

调研日期：2026-11-09。参考源码固定在 `java-up-up/nexus-agent@6359b737771dd6c6e9a2338da49b27eefe9c5406`。本记录依据源码静态阅读，不代表启动过参考系统或验证了其质量、性能。源码在临时目录独立克隆，没有接入 NexRAG 的运行数据。

## 核心结论

值得借鉴的是**结构信息贯穿入库、路由、检索与证据组装**，以及各阶段的观测记录。该提交的普通 RAG 只有向量、关键词两种 `RetrievalChannel`；图查询走独立执行器。父子块和 reranker 分别是聚合、排序步骤，不能把它们计成额外独立召回通道。

代码具备可扩展的通道接口，不等于已经实证四至五路召回一定更准确。本次阅读未发现能支撑 NexRAG 全部目标阈值的同口径公开评测回执。

## 实际链路与 NexRAG 差异

| 环节 | 参考项目的实际实现 | NexRAG 当前实现与借鉴判断 |
| --- | --- | --- |
| 通道编排 | 子问题并行，各子问题再并行调用支持当前计划的通道；通道超时单独记录并降级 | 已有 Dense/BM42 原生融合；可借鉴独立通道轨迹和预算，不能把降级当完整成功 |
| 关键词 | Elasticsearch 多字段检索，章节、文件名、知识域和正文使用不同 boost；没有 ES 时有 SQL 关键词兜底 | BM42 已存在；单纯再加同质词法通道未必有收益。中文词法、精确编号和标题需要专门验证 |
| 融合 | 按 chunk ID 去重的 RRF，常数为 60；融合前分别应用向量最低分与词法相对阈值 | 已有原生 RRF/DBSF。区别在候选身份和可观测性，不在于是否有 RRF |
| 父子块 | 融合后批量取父块、聚合多子块支持分，再对父证据 rerank | 目前对 Child 重排后展开 Parent。可以同一索引比较重排对象，但先扩父块会增加模型输入和截断风险 |
| 图查询 | Document → Section → Item，加父子、前后兄弟、编号、稳定路径；直接结构回答或结构定位取证 | 当前没有这条独立检索路径。适合“第几章”“上一项”和跨段引用，不是已经具备实体知识图谱推理 |
| 路由 | Scope → Topic → Document；结合语义分数、词法分数与别名；记录人工选文档时的 shadow route | NexRAG 先由服务端确定授权库。相关性路由只能在授权库内缩小范围，不能扩大 ACL |
| 切分 | Parent/Child 两套可组合流水线：结构、递归、所谓语义、可选 LLM | NexRAG 已有 Markdown 结构与 tokenizer 递归切分。可补充条款/编号与结构位置，而非重写已有分块器 |
| 存储 | MinIO 原文件；MySQL 元数据、Parent、Child、结构节点；PGVector 向量；ES 词法和导航；可选 Neo4j 投影 | 参考项目将父正文独立保存，批量回取。当前 Parent 随每个 Child 重复存储，节省回表但增大 payload；需要测量后选择 |
| 性能 | 有界线程池、通道/子问题超时、批量 embedding 和批量父块读取 | 可借鉴有界并发和分阶段计时。参考实现的超时和异步返回不能证明 P95 <500 ms / <5 s |

## 需要避免照搬的细节

1. `DocumentStrategyServiceImpl.semanticSplit` 使用英文词元与中文单字集合的 **Jaccard**，不是 embedding 语义边界检测。LLM 切块接收模型生成的字符串数组；正式证据必须另外核对原文连续区间与哈希，不能把改写当原始证据。
2. PGVector 建表脚本使用不固定维度的 `VECTOR`，创建的是文档、任务、父块等普通索引。HNSW/IVF_FLAT 只在注释中建议补充，不能据此认定其向量索引优于 NexRAG 当前 HNSW。
3. 图存储支持 Neo4j 和 MySQL 回退。因此先验证结构邻接的收益，不需要为文档层次树立即增加一个 Neo4j 服务。跨文件实体关系、别名归一和时间冲突是另一个建设范围。
4. `GraphThenEvidenceExecutor` 在该提交中定位图节点后调用 `GraphAnswerRenderer`，不能仅凭类名理解为又执行一轮 Dense/关键词召回。
5. `RagPromptAssemblyService` 有全局与子问题预算，也会把文档片段截到 1,100 字符。候选进入检索结果并不保证其全部证据进入模型；最终上下文仍须独立评分。
6. 引用编号分配、来源展示与提示词要求，不等于逐条事实蕴含校验，不能直接当 Citation Accuracy 100% 的依据。

## 面向当前机器的候选选择依据

当前机器为 Ryzen 9 7940H、约 16 GB RAM、RTX 4050 Laptop。避免同时引入 ES、Neo4j、额外数据库及多个大模型。优先复用 Qdrant、现有元数据和本机存储，逐项判断是否需要增加运行组件。

可能互补的检索来源如下；这是待验证的候选设计，不是已实现能力或收益承诺：

- Dense：表达改写和语义近似。
- 稀疏词法：行业词、缩写、精确编号；分别评估英文 BM42 与中文表现。
- 标题/元数据：用户明确指定文件、章节、版本时产生候选，不能无条件把标题 boost 施加到全部题目。
- 结构邻接：章节、条款、前后步骤和明确的引用边；每条边保留来源、版本与授权范围。
- 有界拆问/补证：需要组合多个证据时保留原始查询，再执行有上限的子问题召回；分别计算额外召回与耗时。

所有通道使用相同的授权过滤。融合后保持原文身份与本轮引用，最终仍以固定 Top-10 排序、实际模型上下文和完整 HTTP 回应评分。

## 与已有失败证据的联系

已有 ContractNLI 阶段诊断的 194 题显示，Dense/Sparse 候选并集覆盖率约为 0.990；融合候选约为 0.948，重排展开父块后约为 0.843，投影到 12,000 字符预算后约为 0.630。它是历史只读诊断，不能充当本轮发布验收。

这说明该数据上的主要损失发生在**候选选择、排序与最终上下文预算**。先比较新的召回通道能补回哪些缺失证据，同时比较聚合/排序策略；不能把新增图服务本身当成改善此处损失的证据。历史窗口候选也有显著排序失败，需要保留，不能因其较快就替代正式基线。

## 源码依据

以下均为固定提交，路径前缀 `nexus-agent-business/nexus-agent-business-chat/src/main/java/org/javaup/ai/`：

- [检索编排、RRF、父块聚合与重排](https://github.com/java-up-up/nexus-agent/blob/6359b737771dd6c6e9a2338da49b27eefe9c5406/nexus-agent-business/nexus-agent-business-chat/src/main/java/org/javaup/ai/chatagent/rag/service/RagRetrievalEngine.java)
- [向量通道](https://github.com/java-up-up/nexus-agent/blob/6359b737771dd6c6e9a2338da49b27eefe9c5406/nexus-agent-business/nexus-agent-business-chat/src/main/java/org/javaup/ai/chatagent/rag/retrieve/channel/VectorRetrievalChannel.java) · [关键词通道](https://github.com/java-up-up/nexus-agent/blob/6359b737771dd6c6e9a2338da49b27eefe9c5406/nexus-agent-business/nexus-agent-business-chat/src/main/java/org/javaup/ai/chatagent/rag/retrieve/channel/KeywordRetrievalChannel.java)
- [ES 多字段检索](https://github.com/java-up-up/nexus-agent/blob/6359b737771dd6c6e9a2338da49b27eefe9c5406/nexus-agent-business/nexus-agent-business-chat/src/main/java/org/javaup/ai/manage/service/keyword/ElasticsearchDocumentKeywordSearchGateway.java)
- [结构图执行器](https://github.com/java-up-up/nexus-agent/blob/6359b737771dd6c6e9a2338da49b27eefe9c5406/nexus-agent-business/nexus-agent-business-chat/src/main/java/org/javaup/ai/chatagent/rag/executor/GraphThenEvidenceExecutor.java) · [Neo4j/MySQL 选择](https://github.com/java-up-up/nexus-agent/blob/6359b737771dd6c6e9a2338da49b27eefe9c5406/nexus-agent-business/nexus-agent-business-chat/src/main/java/org/javaup/ai/manage/service/impl/CompositeDocumentStructureGraphService.java)
- [组合切分及 Jaccard](https://github.com/java-up-up/nexus-agent/blob/6359b737771dd6c6e9a2338da49b27eefe9c5406/nexus-agent-business/nexus-agent-business-chat/src/main/java/org/javaup/ai/manage/service/impl/DocumentStrategyServiceImpl.java)
- [父块批量回取与证据渲染](https://github.com/java-up-up/nexus-agent/blob/6359b737771dd6c6e9a2338da49b27eefe9c5406/nexus-agent-business/nexus-agent-business-chat/src/main/java/org/javaup/ai/manage/service/impl/DocumentKnowledgeServiceImpl.java)
- [知识路由与影子观测](https://github.com/java-up-up/nexus-agent/blob/6359b737771dd6c6e9a2338da49b27eefe9c5406/nexus-agent-business/nexus-agent-business-chat/src/main/java/org/javaup/ai/manage/service/impl/KnowledgeRouteServiceImpl.java)
- [证据预算](https://github.com/java-up-up/nexus-agent/blob/6359b737771dd6c6e9a2338da49b27eefe9c5406/nexus-agent-business/nexus-agent-business-chat/src/main/java/org/javaup/ai/chatagent/rag/service/RagPromptAssemblyService.java) · [线程池](https://github.com/java-up-up/nexus-agent/blob/6359b737771dd6c6e9a2338da49b27eefe9c5406/nexus-agent-business/nexus-agent-business-chat/src/main/java/org/javaup/ai/chatagent/rag/config/ChatRagExecutorConfiguration.java)
- [PGVector 实际建表及索引](https://github.com/java-up-up/nexus-agent/blob/6359b737771dd6c6e9a2338da49b27eefe9c5406/sql/PostgresSql/create_table_postgres_sql.sql)
