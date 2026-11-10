# NexRAG 开源基座选型与复用方案

2026-11-09。按 NexRAG 的微服务、数据中台、评测中台、业务库和知识检索五个边界筛选。公开论文、客户生产案例、源码能力和 NexRAG 实测分别列证。客户或厂商案例用于判断落地可行性，论文分数用于定位适用任务。NexRAG 以冻结的真实 HTTP、原始证据和独立留出集验收。见 [验收口径](../acceptance/rag-quality-targets.md) 与 [实测回执索引](../acceptance/rag-quality-20261109.md)。

## 十个入围项目

前六项有具名生产或机构实际使用证据。其余四项按可直接复用的源码或可审计的公开实验保留，客户质量回执有缺口。各项公开数字度量的对象不同。

| 项目与对应边界 | 公开指标或具名使用 | NexRAG 可以复用的部分 |
| --- | --- | --- |
| **Qdrant** · 知识向量库 | 厂商案例中，&AI CTO 描述专利检索的广召回、元数据收窄和专用精排，托管库超 10 亿向量；Anima Health 的工程负责人描述临床文档检索的生产使用与模型并行迁移。病例问答正确率未公开。 [法律案例](https://qdrant.tech/blog/case-study-and-ai/) · [医疗案例](https://qdrant.tech/blog/case-study-anima-health/) | 保留现有服务。按授权过滤比较精确搜索与 ANN，确认索引损失后才动量化。复用原生字段索引、批量取父块和命名向量。 [索引机制](https://qdrant.tech/documentation/concepts/indexing/) |
| **OpenTelemetry** · 微服务 | Atlassian 工程师公开约 10 万主机、14 个区域的指标迁移；合并 sidecar 后其团队报告总体 sidecar 成本约下降 30%。DoorDash 工程团队也公开了跨服务传播实现。 [Atlassian 工程复盘](https://www.cncf.io/blog/2026/09/17/opentelemetry-everywhere-migrating-a-metrics-platform-at-scale/) · [DoorDash 工程实现](https://careersatdoordash.com/blog/leveraging-opentelemetry-for-custom-context-propagation/) | 用 W3C Trace Context 关联现有 HTTP 和 Evals 样本，单列编码、数据库、重排、组装和生成耗时。现有 Prometheus 继续记录分布。 [Python 传播](https://opentelemetry.io/docs/languages/python/propagation/) |
| **OpenMetadata** · 数据中台 | 项目方公开 Yelp 以逾 10 万实体替换旧目录的案例。Scout24 的 Collate 案例报告 PoC 到生产少于 10 周，后者使用的是商业产品。 [Yelp](https://open-metadata.org/case-study/yelp) · [Scout24](https://www.getcollate.io/customers/scout24) | 复用元数据契约。数据源、业务视图、文档版本、索引、评测样本和质量结果用稳定标识相连，在现有目录登记 owner、来源、分类、质量状态与血缘。 [血缘模型](https://docs.open-metadata.org/latest/how-to-guides/data-lineage) |
| **Inspect AI** · 评测中台 | 英国 AI Security Institute 自研并用于模型评测，与 Arcadia Impact 和 Vector Institute 合作公开数十套评测。METR 发布了实际任务接入桥和容器运行示例。评分器准确性需按任务验证。 [AISI 公开评测](https://www.aisi.gov.uk/blog/inspect-evals) · [METR 实际接入](https://github.com/METR/inspect-metr-task-bridge) | 复用 Evals 已有的 Inspect Task、样本日志和评分器。正式检索走 HTTP，保留逐题失败、证据、评委与候选版本，关联产品 trace。 [日志与追踪](https://inspect.aisi.org.uk/tracing.html) |
| **DuckDB** · 业务数据分析 | Fivetran 负责人公开其生产数据湖写入用法。DuckDB 团队公布的 1.4 LTS 结果在 2025-10-26 规则调整后为当时 ClickBench 热跑开源第一，属于内存分析查询。 [Fivetran 复盘](https://www.fivetran.com/podcast/data-lakes-drive-next-gen-ai-infrastructure) · [版本与排名口径](https://duckdb.org/2025/10/09/benchmark-results-14-lts) | 仅在已过滤、已授权的大租户快照上对比向量化执行。继续复用 SQLite/PostgreSQL 源适配器和 SQLGlot 授权策略。 [嵌入式执行](https://duckdb.org/pdf/SIGMOD2019-demo-duckdb.pdf) |
| **Vespa** · 大规模分级检索 | 项目方公开 RavenPack 的金融检索和 Elicit 的科学文献检索生产案例。Perplexity 曾用于其 Web 索引，现已迁走该索引，在其他场景继续使用。Vespa 的 BEIR TREC-COVID 实验精排 50 个候选，nDCG@10 从 0.7449 提升到 0.7939，压缩版本为 0.8003。客户部署和论文式实验是两项证据。 [具名案例](https://vespa.ai/case-studies/) · [Perplexity 迁移说明](https://vespa.ai/case-studies/why-perplexity-chose-vespa-for-ai-search/) · [公开实验](https://blog.vespa.ai/announcing-colbert-embedder-in-vespa/) | 沿用现有 Qdrant 与检索服务，借鉴候选内轻量 token 匹配、限量跨编码器精排、全局归一与独立预算。 [分级排序接口](https://docs.vespa.ai/en/ranking/phased-ranking.html) |
| **Docling** · 结构化摄入 | IBM 将 Docling 做成 watsonx 可用能力，同时公开 Granite-Docling 的版面、表格与文字转换基准。尚缺同口径的独立客户检索反馈。 [IBM 落地](https://www.ibm.com/new/announcements/docling-for-ibm-watsonx-turn-complex-documents-into-ai-ready-data) · [解析评测](https://huggingface.co/ibm-granite/granite-docling-258M) | 复用结构项、章节路径、表题、页码和边框。优先用于确有版面问题的 PDF，证据回查原页和版本。 [切块接口](https://docling-project.github.io/docling/concepts/chunking/) |
| **RAGFlow** · 父子存储与检索管线 | 有云厂商可部署参考架构。同 K 同证据预算的客户质量指标未核实。 [华为云实施架构](https://www.huaweicloud.com/solution/implementations/building-an-enterprise-grade-knowledge-base-with-ragflow.html) | 父正文独立记录、子块引用及受限回取。分别配置 ANN、粗排、精排、阈值和输出预算。 [父子入库源码](https://github.com/infiniflow/ragflow/blob/2b4d628d51b29fcc37843603ef3a597f659c65ff/internal/ingestion/task/indexdoc/parent_child.go#L33) |
| **OpenContracts** · 法律条款 | CUAD 在 K=32、sliding-500 与 3-large 时 CharRecall 0.6478、CharPrecision 0.0216。原论文同类 K=32 Recall 为 0.6438。长段方案的 Recall 0.7583 伴随更大字符预算。尚缺公开独立生产质量回执。 [公开结果与预算](https://open-source-legal.github.io/OpenContracts/benchmarks/legalbench_rag_results/) | 授权、文档、版本过滤后进入候选池，再精排。全库测试明确选 corpus-wide，标注的文件 ID 只供评分。 [法律检索源码](https://github.com/Open-Source-Legal/OpenContracts/blob/fab192a548912d48bbc0d8a9bf425b50bbb88f71/opencontractserver/llms/vector_stores/core_vector_stores.py) |
| **MedRAG** · 医学专用对照，含 MedCPT | MIRAGE 有 7,663 道题、5 个医学问答集、41 种组合。论文对照中，无检索平均问答准确率 60.69%，PubMed 上 RRF-2 为 70.26%、RRF-4 为 69.71%。MedCPT 另有 BEIR 医学检索子任务结果。尚缺临床部署质量回执。 [MIRAGE 论文与对照表](https://arxiv.org/html/2402.13178#S5.SS2) · [MedCPT 检索实验](https://pmc.ncbi.nlm.nih.gov/articles/PMC10627406/) | 分语料索引、领域模型对照和带编号的原文证据。使用 MedCPT 时锁定文章与问题编码器，按授权的医学知识库启用。 [检索实现](https://github.com/Teddy-XiongGZ/MedRAG/blob/7599a728a28789fd601728c08d313b1148051f41/src/utils.py) |

## NexRAG 的问题落在哪一层

现有基座是 Open WebUI、NexRAG API、独立摄入和检索服务、Qdrant，以及各有职责的数据中台和评测中台。业务适配器从 SQLite/PostgreSQL 读出授权租户的行，由 SQLGlot 约束只读查询并在隔离快照执行。数据源目录、访问权限与外发策略归中台，知识版本另有快照契约。研发合成评测与 RSI 提案已有生命周期；正式质量验收使用 Evals 连真实 HTTP。见 [架构](../design.md)、[业务查询](../business-query.md)、[知识版本](../knowledge-versions.md)、[Evals 与 RSI](../evaluation-and-rsi.md)。

| 实际召回路径 | 法律选择集 Recall@10 | MRR | nDCG@10 | 串行检索 HTTP P95 |
| --- | ---: | ---: | ---: | ---: |
| 四路 | 0.6950 | 0.3684 | 0.4419 | 1.380 s |
| 五路，增加正文词法 | 0.6817 | 0.3609 | 0.4334 | 1.536 s |
| 六路，再增加图召回 | 0.6733 | 0.3615 | 0.4305 | 4.103 s |

每个变体 360 个真实 HTTP 请求，每个语言或领域选择组 60 题。四路为 BGE-M3 稠密、BGE-M3 稀疏、文件名/标题和结构。老部署可能仍指向 BM42 旧索引，以实际运行回执中的模型与 collection 为准。关闭文件名使中文训练组 MRR 由 0.9917 降至 0.9833。结构路消融只有 85/360 条，独立贡献待补证。四路开发环境实际候选上限 50、重排输出上限 10，新安装模板的重排上限仍为 3。串行延迟仅供定位，正式验收要求当前电脑并发 1/5/10、每组至少 1,000 个请求。 [通道与运行回执](../acceptance/rag-quality-20261109.md)

法律差距集中在**条款定位和排序**。六路法律组 120/120 题前十有正确文件，85/120 有至少一条完整证据，19/120 把证据排到第一。四路阶段记录显示法律训练/选择组分别有 6/6 题在候选融合时掉出、8/10 题被重排阈值拦掉。涉及的 25 条标注中，24 条至少在全索引中一个现有子块里完整出现，在检查的 512 token 重排输入预算下也有候选可保留完整区间。仍须按具体送排子块 ID 核实这 24 条的真实输入。合同中位数约 36,821 字符、8 个父块，中文约 455 字符、1 个父块，医学约 1,338 字符、1 个父块。 [原文区间与逐题诊断](../acceptance/rag-quality-20261109.md)

两轮父块聚合实测均低于四路基线。整池按父块融合使法律选择组 Recall@10 降至 0.6650、中文训练组 Recall@10 降至 0.9500；仅替换候选池尾部重复项使法律选择组 Recall@10 降至 0.6817，串行 P95 从 1.380 s 到 1.390 s。回执分别为 four-route-parent-fusion-01/paired-summary.json 与 four-route-parent-tail-reserve-01/paired-summary.json。

本机原始回执位于仓库 .local-run/quality-20261109。四路、五/六路比较可查 four-route-production-evals-01/without-body/evals/ 与 graph-full-evals-04/paired-summary.json。法律阶段、文本跨度及送排预算可查 graph-channel-ablation-02/without-body 下的 legal-stage-summary.json、child-evidence-geometry.json 与 rerank-token-budget-diagnostic.json。文档内精排首轮失败留在 four-route-document-scope-01，加入回退后的对照留在 four-route-document-scope-02。

## 源码对应的实施边界

### 候选、精排与父子证据

- **法律**。OpenContracts 先建立权限与文档范围查询，可按文档 ID 限定，默认检索当前版本，再交给可选 reranker。NexRAG 用已授权四路确定候选文件。在有可靠收窄依据时，精排同一版本内的原文条款。范围不确定或二阶段无合格证据时回退广召回。首轮法律预检返回空证据，加入回退后通过三个分组的真实 HTTP 预检。该分支默认关闭，后续数据见验收报告。 [范围和候选源码](https://github.com/Open-Source-Legal/OpenContracts/blob/fab192a548912d48bbc0d8a9bf425b50bbb88f71/opencontractserver/llms/vector_stores/core_vector_stores.py#L240) · [LegalBench-RAG](https://arxiv.org/html/2408.10343v1)
- **预算**。RAGFlow 当前 Go 版本分别设置 ANN 检索数、ANN 搜索候选数、精排候选数、分页和阈值。该入口精排候选默认 64，ANN 两个参数默认 1,024 和 2,048。模型与词法分数先处理量纲再合并。NexRAG 单列每路预取量、融合候选量、实际精排批大小、淘汰阶段和输出证据量。候选深度与阈值在训练及选择集决定，最终 K=10 与原文区间保持冻结。记录精排输入 token 和证据字符预算。 [RAGFlow 候选实现](https://github.com/infiniflow/ragflow/blob/2b4d628d51b29fcc37843603ef3a597f659c65ff/internal/service/nlp/retrieval.go#L110) · [模型打分](https://github.com/infiniflow/ragflow/blob/2b4d628d51b29fcc37843603ef3a597f659c65ff/internal/service/nlp/reranker.go#L42)
- **父块存储**。RAGFlow 写入独立隐藏父记录，子记录留 mom_id，恢复时用知识库和文档约束父键，缺父记录则保留原子块。这个固定提交按父键逐次查询。Java **nexus-agent** 确有批量父块回取及章节、条款结构。NexRAG 在 Qdrant 复用两者的布局和批读边界，父键绑定授权库、文件、版本和原文位置。精排先保留命中子块身份，再按键一次受限取回父文或邻接窗口。采用新索引验证或双读迁移。 [RAGFlow 回取源码](https://github.com/infiniflow/ragflow/blob/2b4d628d51b29fcc37843603ef3a597f659c65ff/internal/service/nlp/retrieval.go#L1078) · [nexus-agent 源码核对](nexus-agent-retrieval.md)
- **已测载荷**。归档的 25,386 个实际子块点对应 7,842 个带作用域的父键。父正文 UTF-8 字节量 105,407,821，按父键去重后 28,313,854，重复 77,093,967 字节，占这部分载荷的 73.1%。这是逻辑字节统计。实际磁盘、网络与 P95 要在隔离副本验证。父块外置与召回变更分别对照。

### 向量、切块、图与医学

- **向量与索引**。FlagEmbedding 从同一 BGE-M3 模型取归一化的稠密向量、按 token ID 聚合的学习式稀疏权重和 token 级向量。先验证问句和文档编码、稀疏非空、模型与索引版本一致。NexRAG 多租户 HNSW 使用 m=0、payload_m=16，按相同授权过滤比较精确搜索与 ANN。BGE-M3 官方 MLDR 的 nDCG@10 分别为稠密 0.525、稀疏 0.622、融合 0.648、再加多向量 0.650。先将 MaxSim 限于候选。25,386 个子块若每块 256×1,024 个 FP16 分量，仅向量原始数据约 12.4 GiB。 [MLDR 论文表 4](https://arxiv.org/html/2402.03216v1) · [BGE-M3 实现](https://github.com/FlagOpen/FlagEmbedding/blob/897baa67c0ffddbfccf4e6776ff55310d67809d4/FlagEmbedding/inference/embedder/encoder_only/m3.py) · [Qdrant 多向量](https://qdrant.tech/documentation/concepts/vectors/#multivectors)
- **切块**。Docling 的 HybridChunker 先按结构组块，再按 tokenizer 预算切开，只合并元数据匹配的邻接片段。编码可使用章节路径与表题，引用指回原页、字符区间和版本。对 except、subject to 等限定语或跨父块标注试用有界邻接窗口。保留已冻结文本原始坐标。 [分块源码](https://github.com/docling-project/docling-core/blob/3e3be95e9de7472e78da2068ae2b281e4dd34611/docling_core/transforms/chunker/hybrid_chunker.py#L698)
- **结构与图**。保留有实测增益的文件名/标题，正文与图默认关闭。GraphRAG 按知识库和版本隔离，单测编号跳转、定义引用和跨文档关系的增量及代价。结构通道独立收益待补证。Java 项目的 Document → Section → Item 用来组织证据。 [Java 项目结构实现](nexus-agent-retrieval.md)
- **医学**。MedRAG 为不同语料建立独立索引，每路先取候选再按排名融合。RRF 路径每路预取 max(2*k, 100)，默认最终 k=32，默认 rrf_k=100，生成按 token 截止。MedCPT 训练独立的问题和文章编码器，并配交叉编码器。对术语、反例和时效问题做难例对照后，再决定是否给现有医学库增加专用模型。 [候选与融合](https://github.com/Teddy-XiongGZ/MedRAG/blob/7599a728a28789fd601728c08d313b1148051f41/src/utils.py) · [医学编码器](https://github.com/ncbi/MedCPT/tree/11e129be74102c98d16a11c310b0b5ce74c1db5e)

### 数据中台、评测中台和业务数据库

- **数据中台**。最小血缘为数据源 → 文档或视图版本 → 解析器或索引版本 → 证据 → Evals 运行。记录原文哈希、区间、ACL、模型与评分器版本。授权在请求层和存储查询同时生效。跨库、跨租户、版本失效和外发失败进入验收。
- **评测中台**。在现有 Evals/Inspect 配对样本与候选版本。分别记录候选缺失、截断、重排掉分、原文缺失、生成未用及引用不支持。超时、缺失分组和缺失判分单列。RSI 读取训练失败，选择集选定后冻结候选，独立留出集确认。人工一致率需要真实独立人工，模拟分数另记。
- **业务数据库**。只有确认内存 SQLite 在大型授权快照中成为瓶颈，才在执行层对比 DuckDB。继续沿用单语句与函数白名单，执行器只接收已物化的租户视图。禁止任意文件、网络、扩展和跨库访问，限制内存与时限。比较结果一致性、权限和 P95 后决定是否开放。 [DuckDB 安全配置](https://duckdb.org/docs/stable/operations_manual/securing_duckdb/overview.html)

## 实施顺序与晋升条件

1. **法律条款闭环**。复用 OpenContracts 的范围与版本过滤，补齐二阶段无合格证据的回退。保留最高分子块身份，用焦点问题精排原文条款，逐题记录候选、实际送排及证据。固定最终 K=10，比较现有重排与公开模型。法律 Recall、MRR 和 nDCG 有实质改进、中文和医学不退化，才进入下一道门控。
2. **独立改存储与性能**。复用 RAGFlow 父正文分离和 nexus-agent 的批量回取。先检查读取等价、权限、失败回退和版本迁移，再测实际 I/O、进程内存与分段延迟。用精确搜索量化 ANN 误差。按实测增量决定默认通道。
3. **验证解析缺口**。Docling 只进入有版面和证据缺口的 PDF 队列。对同一原文标注核对页码、区间、完整条款进入候选的比例、最终 K=10 和上下文成本。医学专用模型按难例决定接入。
4. **验收开箱行为**。从干净安装的产品入口复跑，冻结模型、索引、默认参数、硬件和并发，分冷启动与稳态。正式验收包括真实检索、生成、引用校验与人工一致率，以及检索 P95 <500ms、端到端 P95 <5s。留出集失败按领域和原因保留。

## 源码抽样范围

独立克隆并阅读 RAGFlow 2b4d628d、OpenContracts fab192a5、FlagEmbedding 897baa67、docling-core 3e3be95e、MedRAG 7599a728、MedCPT 11e129be 与 CanLegalRAGBench 4863f222。Java 参考见单独报告。其余项目用官方规范、工程案例和 NexRAG 接口核对。外部数据来自标注的公开资料，未在目标电脑部署十套系统。CanLegalRAGBench 可作为加拿大案例法补充对照，专利搜索、案例法与合同区间标注各自对应不同任务。 [加拿大法律基准](https://arxiv.org/abs/2605.30497)
