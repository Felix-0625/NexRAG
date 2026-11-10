# 目标达成与降级说明（2026-11-10）

本文汇总各项质量目标的**实测值、业界参照与未达标原因**。目标阈值见 [rag-quality-targets.md](rag-quality-targets.md)；
完整过程与回执见 [rag-quality-20261109.md](rag-quality-20261109.md) 与 [legal-tuning-20261110.md](legal-tuning-20261110.md)。
未测的指标一律写“未测”，不使用估算值替代。

## 检索指标

| 数据组 | 阶段 | Recall@10 | MRR | nDCG@10 | 结论 |
| --- | --- | --- | --- | --- | --- |
| zh-general（CMRC2018） | Train / Selection | 1.000 / 1.000 | 0.992 / 0.992 | 0.994 / 0.994 | 达目标（开发/选择集） |
| en-medical（PubMedQA） | Train / Selection | 1.000 / 1.000 | 0.985 / 1.000 | 0.989 / 1.000 | 达目标（开发/选择集） |
| en-legal（LegalBench-RAG / CUAD） | Selection，基线 V0 | 0.695 | 0.368 | 0.442 | 未达 |
| en-legal | Selection，最佳 V6 | 0.753 | 0.377 | 0.461 | 未达 |

说明：
- 表中 zh/en-medical 数据来自调优轮的 Train / Selection 分组，用于确认无退化；Holdout 终验见 `rag-quality-20261109.md`，该报告结论仍为“尚未验收通过”。
- en-legal 是未达标的主要来源。公开基准 LegalBench-RAG（[arXiv 2408.10343](https://arxiv.org/abs/2408.10343)）报告的最好结果在 CUAD 子集上约 0.64（K=32），本项目 0.753（K=10，自建评测口径）与其处于同一量级；两者语料、K 与切分不同，仅作量级参照，不是同口径对比。两者都低于 0.90 目标。
  法律合同条款需要精确定位到片段，通用 embedding + 重排难以达到通用问答集的水平，这是领域难度，不是评测口径问题。
- 已尝试并记录的法律域方案：8 个变体（候选数、最小分数等）、Jina 多语言重排（Recall 反降至 0.612，且为 CC-BY-NC，未采用）、图召回（Recall 0.695→0.673，P95 1.5→4.1 s，默认关闭）。
- Contextual Retrieval（为每个片段加上下文前缀）预计对法律域有帮助，但需要重建法律全集索引，成本高，尚未实施，列入后续计划。

## 延迟

| 指标 | 目标 | 实测 | 结论与原因 |
| --- | --- | --- | --- |
| 检索 P95 | < 500 ms | 默认配置约 1.5 s；候选 150 的高召回配置约 3.7 s | 未达。本机为 CPU 重排（cross-encoder）+ 本地 bge-m3；500 ms 需要 GPU 或云端小型重排。 |
| 端到端 P95 | < 5 s | 约 31 s | 未达。本机生成模型为 `qwen3:4b-instruct`（RTX 4050 6GB），需要更快的生成模型。 |

默认不启用 `RETRIEVAL_CANDIDATE_LIMIT=150`：它带来约 +0.02 Recall，却使 P95 增加约 2.4 s，与延迟目标冲突。需要高召回的部署可自行打开。

## 回答级指标

| 指标 | 目标 | 实测 |
| --- | --- | --- |
| Faithfulness | > 0.95 | 未测 |
| Hallucination Rate | < 0.01 | 未测 |
| Answer Relevancy | > 0.85 | 未测 |
| Answer Correctness | > 0.85 | 未测 |
| Groundedness | > 0.90 | 未测 |
| Citation Accuracy | 100% | 未测 |
| 人工与 LLM Judge 一致率 | > 85% | 未测 |

原因：这些指标依赖独立的云端评委模型，本机只有同一个 `qwen3:4b-instruct` 可用，用被测模型给自己打分不具备可信度。
仓库内 `evals/quality/judge.py`、`calibrate_judge.py`、`gate.py` 已就绪；配置评委密钥（仅从本地环境变量读取）并运行即可产出。

## 已落实的“代替指标”的工程保障

- 逐声明校验：`src/agent/` 的 `grounding.py` 对回答中的声明做证据校验，用来约束幻觉（借用 Self-RAG / CRAG 的思想，未做模型训练）。其效果尚未用上表指标量化。
- 权限隔离与只读业务库约束保持不变，联网补救不会越过授权范围。

## 后续计划（按收益排序）

1. 配置云端评委并跑回答级评测与人工一致率校准。
2. 为法律全集实施 Contextual Retrieval 前缀，重建索引后复测 en-legal。
3. 在有 GPU 的机器上复测检索 P95，评估 ONNX/INT8 重排。
