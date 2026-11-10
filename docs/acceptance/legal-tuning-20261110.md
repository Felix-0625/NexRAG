# 英文法律检索：查询时调优（2026-11-10）

## 范围与结论

回执目录：`.local-run/quality-20261109/tune-legal-01/`（忽略目录，含脚本、真实 HTTP 回执、有效配置、评分及日志）。只读取 `public-v4/train.jsonl`、`selection.jsonl` 的问题；**未读取、请求或评分 holdout 问题**。评分器会校验共享的公开原文及固定 Parent 目录，这不等于使用 holdout 问题。

先将冻结 `.local-run/q-vbamiyz0/data/qdrant` 完整复制到 `.local-run/tune-qdrant/`，Qdrant 只打开副本（50,772 点）；不复制/打开图存储，不摄入、不重切块、不改向量。全部实验 graph OFF，K=10，使用授权文件中的全部三个 scope，而不是仅搜法律库。无回答生成、无人工裁判。

Selection 按 Recall@10 优先、MRR 次序选优：**V6（候选 150 + 最低分 0）胜出**，法律 Selection Recall 提升 0.058333、MRR 提升 0.008261；Train Recall 提升 0.100000、MRR 提升 0.019299。其他两个 cohort 的 Train/Selection Recall、MRR 均未下降。代价是法律 Selection 串行 P95 从 1.528s 升至 3.715s（约 2.43 倍）。这是开发集观察，不是 holdout、并发负载或正式质量验收。

## 法律 Selection（每项 60/60 可评分）

| 变体 | 相对 V0 改动 | Recall@10 | MRR | nDCG@10 | P50 / P95 秒 |
|---|---|---:|---:|---:|---:|
| V0 | 基线 | 0.695000 | 0.368446 | 0.441868 | 1.315 / 1.528 |
| V1 | candidate_limit=150 | 0.720000 | 0.375390 | 0.451906 | 3.501 / 3.713 |
| V2 | min_score=0 | 0.736667 | 0.374530 | 0.455245 | 1.294 / 1.364 |
| V3-replace | fusion=replace | 0.686667 | 0.370833 | 0.438479 | 1.296 / 1.384 |
| V3-rrf20 | RRF_K=20 | 0.695000 | 0.368446 | 0.441745 | 1.299 / 1.385 |
| V4 | jina-reranker-v2-base-multilingual | 0.611667 | 0.377262 | 0.423499 | 1.334 / 1.431 |
| V5 | title_routing=true | 0.695000 | 0.365668 | 0.439685 | 1.297 / 1.385 |
| **V6** | **candidate_limit=150，min_score=0** | **0.753333** | **0.376706** | **0.460542** | **3.547 / 3.715** |

共 8 个不同法律 Selection 配置。V6 合并 V1、V2 两个单独不退化的改动；它们的组合测试先完成，之后补完 V4。V4 虽 MRR 最高，但 Recall 比 V0 低 0.083333，因此未按 MRR 单指标挑选。Title routing 和较小 RRF 常数未带来收益；replace 损失 Recall。V2 是较低延迟的备选，本轮只确认 Selection 总冠军 V6 的 Train，不声称 V2 已通过跨域门禁。

## Train 确认与跨域检查

每行每配置均 60/60 可评分。R/M/N 表示 Recall@10 / MRR / nDCG@10。

| cohort / split | V0 R/M/N | V6 R/M/N | V0 P50/P95 秒 | V6 P50/P95 秒 |
|---|---|---|---|---|
| en-legal / train | .678889 / .327149 / .414049 | .778889 / .346448 / .451479 | 1.315/1.407 | 3.590/3.817 |
| zh-general / train | 1 / .991667 / .993849 | 1 / .991667 / .993849 | .797/1.026 | 2.273/3.013 |
| zh-general / selection | 1 / .991667 / .993849 | 1 / .991667 / .993849 | .762/1.065 | 2.252/3.134 |
| en-medical / train | .966667 / .966667 / .966667 | 1 / .985417 / .988591 | 1.146/1.258 | 3.237/3.438 |
| en-medical / selection | .983333 / .983333 / .983333 | 1 / 1 / 1 | 1.121/1.227 | 3.178/3.392 |

## 固定设置、模型与失败

- 固定混合搜索、five-channel=true、body=false、filename=true、title=false（V5 除外）、graph=false；BGE-M3 dense（独立 Ollama）和 BGE-M3 sparse CPU；BGE reranker base CUDA（V4 除外）。`INITIAL_RETRIEVAL_MULTIPLIER=5`、parent multiplier=3、context=parent、rerank top K=10、required=true、dedup before limit=true、fusion=rrf、RRF_K=60；V0 candidate=50、min_score=.05。
- 实际启动使用 `evals.quality.worker retrieval <独立端口>`，沿用之前的 CUDA adapter 和依赖目录；各变体的 `retrieval-effective-settings.json`、`retrieval-runtime.json` 与源码归档可审计实际加载设置。HTTP `/search` 保留原题、scope、K=10；source-map 将真实 file_id 映射至公开原文，canonical Parent 区间评分。
- FastEmbed 的支持列表包含 Jina v2；模型成功下载，revision `9cfeff2df7d40d1b78e75e5e9cebec92a99813c9`，使用相同 CUDA adapter，仅指定其本地模型路径。模型是 **CC-BY-NC-4.0**，未引入生产默认。BGE 分支 sigmoid、Jina 原始分数均沿用现有代码；V4 保持 .05 阈值，因此结果不能外推为所有 Jina 阈值/上下文设置的结论。
- 首次 V1 的第一个请求遇到 Qdrant client timeout，服务返回非 JSON 500；host 因解析 JSON 停止，finally 已清理。保留 `initial-failure.json`；修正实验脚本以记录非 JSON 失败后重启，完成 V1 60/60。首次失败请求未进入最终回执，最终 V1 表不包含这次失败的耗时；不能将表格当成零故障可用性证明。另一次下载命令 PowerShell 引号错误，改独立脚本后下载成功。
- 串行耗时包含真实 HTTP 调用，不含服务启动/权重加载；无查询或 pair-score 结果缓存。各变体单次运行，无重复随机试验或显著性检验。

## 默认值落地与验证

V6 在 Selection 上达到 Recall +0.02 门槛且 Train 不退化，但代价是 P95 由 1.53 s 升至 3.72 s，与延迟目标冲突。因此最终取舍如下：`RERANK_MIN_SCORE=0` 作为默认保留；`RETRIEVAL_CANDIDATE_LIMIT=150` **不作为默认值**（`retrieval_candidate_limit` 默认 `None`），仅在 `base/retrieval-agent/.env.example` 与根 `.env.example` 中以注释形式提供，作为“高召回、容忍延迟”的可选配置。根示例仍是 replace、rerank_top_k=3、dedup=false，不应把该示例整体宣称为本次实测 K=10 配置。本轮收益只在上面固定的 K=10 配置获得；其他 K、模型及负载需要另验。已有显式环境覆盖不自动迁移。

流水线测试：`test_vector_search_called_with_collection_names_list` 的断言按默认 `retrieval_candidate_limit=None` 校验，候选深度回落为 `k * multiplier`（该用例中为 2）；另有两个测试显式设置 `candidate_limit=None` 或 80，保留原有覆盖。`base/retrieval-agent` 全量：`225 passed`。

本次创建的 Qdrant、独立端口 Ollama 和 retrieval 全部停止；`run-ended.json` phase=done，`cleanup-check.json` 四个端口均关闭。未停止/操作用户 11434 Ollama。保留 Qdrant 副本与模型、回执供复核；未提交 git。工作树原本已有大量改动，本轮未清理或回退它们。当前工具无 codegraph，无法做定向索引。

## 复现（PowerShell，仓库根目录）

```powershell
# 只在副本不存在时复制；永远不要让 Qdrant 打开冻结源目录。
if (-not (Test-Path .local-run/tune-qdrant)) {
  Copy-Item .local-run/q-vbamiyz0/data/qdrant .local-run/tune-qdrant -Recurse
}
& .local-run/venvs/retrieval-pillow13/Scripts/python.exe `
  .local-run/quality-20261109/tune-legal-01/download_v4.py
& .local-run/venvs/retrieval-pillow13/Scripts/python.exe `
  .local-run/quality-20261109/tune-legal-01/run.py
# run.py 为可续跑脚本：已有完整回执会复用。完整重跑请先在忽略目录备份/移走
# V0..V6 子目录及 results.json；不要删除冻结数据或新增 holdout split。

# 独立复核某个变体的评分，output 必须尚不存在；只用 train/selection。
& .local-run/venvs/retrieval-pillow13/Scripts/python.exe -m evals.quality.evaluate_retrieval `
  --dataset .local-run/quality-20261109/public-v4 `
  --run .local-run/quality-20261109/tune-legal-01/V6 `
  --output .local-run/quality-20261109/tune-legal-01/rescore-selection `
  --split selection
cd base/retrieval-agent
uv run pytest -q
```

随机凭据每次启动重新生成，仅存忽略的实验目录，不写入本文或生产配置。部分仅测法律的变体，评分 summary 其它 cohort 的 missing=60 表示未请求，非失败；本表只报告真实已请求 cohort。
