# 评测材料

这里保存可公开的合成种子和可复现的外部评测适配器。测试代码在 [tests](../tests/README.md)，用户演示配置在 [examples](../examples/README.md)。运行报告、下载语料和实例凭据保存在被忽略的 `.local-run/` 中。

| 路径 | 用途与边界 |
| --- | --- |
| `nexrag_seed.jsonl` | 项目合成评测种子；训练、留出与回归拆分由评测流程管理 |
| `synthetic_local.jsonl` | 独立的本地合成样本；不能据此推断客户数据表现 |
| [finqa](finqa/README.md) | 固定公开 FinQA 子集的准备、文档召回及答案检查适配器；保留上游许可 |
| [promptfoo](promptfoo/README.md) | 已保存合成答案的输出回放与故意失败的负对照，不调用模型 |

生产 Agent、规则评分、RSI 候选生成和审批灰度的完整关系见 [评测与 RSI](../docs/evaluation-and-rsi.md)。新增样本应标明数据来源和预期行为，避免把留出答案放进候选生成输入；历史输出回放不能作为新的模型或前端验收结果。
