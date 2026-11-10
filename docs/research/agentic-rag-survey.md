# Agentic RAG 调研与提升方案

2026-11-10。目标：对照成熟的 agentic RAG 项目，找出能实质提升 NexRAG 验收指标的做法，并给出采用或弃用结论。
指标口径见 [rag-quality-targets](../acceptance/rag-quality-targets.md)，实测回执见 [验收记录](../acceptance/rag-quality-20261109.md)，更早的基座选型见 [nexrag-foundations](nexrag-foundations-20261109.md)。

## 1. 现状基线（冻结公开语料，K=10，真实 HTTP）

| 分组 | Recall@10 | MRR | nDCG@10 | 说明 |
| --- | ---: | ---: | ---: | --- |
| zh-general（CMRC2018） | 0.667 → 1.000 | 0.625 → 1.000 | 0.636 → 1.000 | 两路 → 五路，12 题开发对照；留出 0.997 / 0.976 / 0.982（298/300） |
| en-medical（PubMedQA） | 1.000 | 1.000 | 1.000 | 摘要即单个 Parent，检索本身不是瓶颈 |
| en-legal（LegalBench-RAG / CUAD） | 0.625 | 0.350 | 0.412 | 瓶颈。选择集四路 0.695，加入图召回后反降至 0.673 且 P95 升至 4.1 s |

回答级指标（Faithfulness、Hallucination、Citation 等）尚无通过正式流程的实测；独立人工一致率未测。
当前机器没有配置云端评委密钥，本地 `qwen3:4b-instruct` 在 RTX 4050 上端到端 P95 约 31 s，因此 E2E P95 < 5 s 依赖更快的生成模型。

结论：中文与医学检索已接近目标，**法律长合同的条款定位与排序**是最大缺口；回答侧缺少可信的评委与低延迟生成模型。

## 2. 入选项目

只列出对某项指标有明确预期增益、并有公开来源的项目。

| 项目 | 借鉴点 | 对应指标 | 预期增益（来源口径） | 结论 |
| --- | --- | --- | --- | --- |
| **Anthropic Contextual Retrieval** [文章](https://www.anthropic.com/news/contextual-retrieval) | 嵌入和 BM25 前，给每个子块补一句"所属文档与章节"的上下文；再叠加重排 | Recall@10、MRR、Context Recall（法律组） | 官方报告 top-20 检索失败率降低 49%，叠加重排降低 67% | **采用**。条款类子块缺少合同名与条款号是法律组掉榜的直接原因。上下文用确定性的"文件名 + 章节路径"前缀生成，不依赖 LLM，避免摄入成本 |
| **RAGFlow** [仓库](https://github.com/infiniflow/ragflow) | Parent/Child 存储、模型分数与词法分数归一后融合、分别配置 ANN / 精排候选量与阈值、答案带可点击引用 | MRR、nDCG、Context Precision、Citation Accuracy | 项目未公开同口径指标；机制已在 NexRAG 五路召回中复用 | **采用（已落地）**。后续仅调整精排候选量，并保持 Parent 回取受授权范围约束 |
| **LlamaIndex** [文档](https://docs.llamaindex.ai/en/stable/examples/retrievers/auto_merging_retriever/) | AutoMerging / Sentence-Window：命中小块、返回其父块或邻接窗口；`CitationQueryEngine` 给出句级引用 | Context Precision、Citation Accuracy | 论文外的工程经验，无统一数字 | **部分采用**。Parent 展开已有；再增加"同一 Parent 内去重后按命中分合并"，不再整池按 Parent 融合（实测使法律 Recall 降到 0.665，已弃） |
| **Haystack** [文档](https://docs.haystack.deepset.ai/docs/documentjoiner) | `DocumentJoiner` 的 RRF / 加权融合、`SentenceWindowRetriever`、检索与生成拆成可替换的管线组件 | MRR、nDCG、可维护性 | 机制与 NexRAG 五路 RRF 一致，无新增增益 | **仅参考结构**。管线组件边界用于拆分过长的检索服务代码 |
| **Self-RAG** [论文](https://arxiv.org/abs/2310.11511) | 生成过程中用反思标记判断"是否需要检索""证据是否支持""输出是否有用" | Faithfulness、Hallucination、Citation | 论文在 PopQA、PubHealth、ASQA 上事实性与引用精度均优于同尺寸检索增强基线 | **采用思想，不采用训练**。用现有 `grounding.py` 的逐声明校验替代反思标记：无支持证据的声明被删除或改为拒答 |
| **CRAG** [论文](https://arxiv.org/abs/2401.15884) | 检索评估器给出 正确 / 模糊 / 错误 三档，错误时改写查询或扩大检索，不直接送入生成 | Hallucination、Context Precision | 论文在 PopQA、Biography、PubHealth、Arc-Challenge 上优于标准 RAG | **采用思想**。证据不足时扩大范围重试一次，仍不足则拒答，不用外部网页补救（权限隔离要求） |
| **Ragas** [文档](https://docs.ragas.io) | Faithfulness、Context Precision/Recall、Answer Relevancy 的标准定义 | 全部回答级指标 | 度量工具，不直接提升 | **已采用**（`evals/quality/judge.py`，Ragas 0.4.3） |
| **LightRAG / GraphRAG** [论文](https://arxiv.org/abs/2410.05779) | 实体关系图的双层检索，适合跨文档综述类问题 | 多跳 Context Recall | 论文在综述类问题上胜率较高 | **弃用为默认**。本项目实测：加入图召回后法律 Recall 0.695 → 0.673，P95 1.5 s → 4.1 s。保留为可选通道，默认关闭 |

LegalBench-RAG 论文（[arXiv 2408.10343](https://arxiv.org/abs/2408.10343)）给出的 CUAD 最好 Recall 约 0.64（K=32），说明法律合同的 Recall@10 > 0.90 超出业界公开水平。法律组的目标按验收协议的降级规则处理，见第 4 节。

## 3. 落地方案

| 序号 | 改动 | 来源项目 | 目标指标 | 风险与验证 |
| --- | --- | --- | --- | --- |
| 1 | 摄入时给子块嵌入文本加"文件名 + 章节路径"前缀，原文不变 | Contextual Retrieval | 法律 Recall / MRR | 需重建法律集合；用同一批留出前的 Train/Selection 题对比 |
| 2 | 默认关闭图召回，保留开关 | LightRAG（实测弃用） | 检索 P95 | 已有实测回执 |
| 3 | 精排候选量与阈值只在 Train/Selection 上调，K=10 与评分区间冻结 | RAGFlow | MRR、nDCG | 留出集只评一次 |
| 4 | 回答前逐声明校验，不被支持的声明删除或拒答 | Self-RAG / CRAG | Faithfulness、Hallucination、Citation | 单元测试 + 评委抽检 |
| 5 | 评委换为云端强模型，保留 40+40 外部人工标签校准和本轮独立人工盲审 | Ragas | 评委一致率 | 需要用户提供云端密钥与独立人工 |

## 4. 达标策略与降级规则

- 未下调的指标必须在正式留出集上实测达标。
- 下调只允许因"诚实评测下不可达"，并且不低于业界通行水平。每个下调项在验收记录中写出实测值、参照来源和原因。
- 不用调参泄漏留出集，不改变评分口径；失败样本保留。
