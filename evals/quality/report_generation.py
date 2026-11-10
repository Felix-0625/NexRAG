"""Render generation, performance, and outstanding acceptance evidence."""

import json
import statistics

from evals.quality.records import read_jsonl
from evals.quality.report_sections import load


def render_default_diagnostics(base, lines):
    """Append the default diagnostics evidence in report order."""
    quantized = load(base / "rerank-quantized-http-01/receipt.json")
    if quantized and quantized.get("phase") == "done" and quantized["paired"]:
        fp32_seconds, int8_seconds = quantized["http_seconds"]
        hits_fp32, hits_int8 = quantized["all_case_source_hits"]
        lines += [
            "## CPU 重排量化诊断",
            "",
            "在隔离快照使用真实原文和双路召回，以应用默认 K=3 对照 BGE 重排原权重与动态 INT8 权重。"
            f"{quantized['planned']} 题中有 {quantized['paired']} 题进入重排的候选及顺序完全一致，"
            f"其中 {quantized['exact_order']} 题最终排序一致，HTTP 中位数由 "
            f"{fp32_seconds:.2f} 秒降至 {int8_seconds:.2f} 秒。"
            f"同候选的分数最大绝对变化为 {quantized['max_abs_score_delta']:.4f}。",
            "",
            f"全部题目的预期来源命中数为 {hits_fp32} 和 {hits_int8}。"
            "新增命中所在题的重排候选也发生变化，无法归因于权重量化。"
            "请求串行执行且与全库建图并行，诊断结果未用于 K=10 或 HTTP P95 验收。"
            "默认重排权重仍为原模型。回执见 `rerank-quantized-http-01/receipt.json`。",
            "",
        ]
    clean = load(base / "clean-default-real-http-01/receipt.json")
    if clean and clean.get("phase") == "done":
        first = [row for row in clean["searches"] if row["phase"] == "first_and_subsequent"]
        repeat = [row for row in clean["searches"] if row["phase"] == "repeat"]
        if first and repeat:
            found = sum(bool(row.get("source_found")) for row in first)
            successful = sum(row["status"] == 200 for row in clean["searches"])
            empty = sum(row.get("returned") == 0 for row in first)
            warm = [row["seconds"] for row in repeat]
            lines += [
                "## 五路接入前的空库实测",
                "",
                f"沿用已有依赖缓存，以默认检索配置启动隔离的 Windows 原生服务。通过 WebUI API 上传并回下载校验 "
                f"{clean['original_verified']}/{clean['original_sources']} 份公开原文。随后使用真实知识库授权范围调用 "
                f"`/search`。共 {successful}/{len(clean['searches'])} 次 HTTP 200，首轮 {found}/{len(first)} "
                f"次找到预期来源，{empty} 次为空。",
                "",
                f"首条请求 {first[0]['seconds']:.2f} 秒。重复请求的串行中位数 {statistics.median(warm):.2f} 秒，"
                f"范围 {min(warm):.2f}–{max(warm):.2f} 秒。使用服务默认请求 K=5、默认重排返回上限 3，"
                "仅检查来源命中，未发起聊天生成。逐请求回执和阶段监控样本见 `clean-default-real-http-01/`。",
                "",
            ]
    trace = load(base / "clean-default-candidate-trace-01/receipt.json")
    if trace and trace.get("rows"):
        k3 = next((row for row in trace["rows"] if row["k"] == 3), None)
        trace_config = load(base / "clean-default-candidate-trace-01/effective-config.json", {})
        threshold = trace_config.get("settings", {}).get("RERANK_MIN_SCORE")
        if k3 and threshold is not None and k3["matching_scores"] and k3["returned"] == 0:
            lines += [
                f"复测空结果时，知识工具默认 K=3 请求找到了预期来源的候选，但该候选的重排得分 "
                f"{max(k3['matching_scores']):.5f} 低于配置阈值 {threshold}，HTTP 200 返回空结果。"
                f"该请求完整耗时 {k3['seconds']:.2f} 秒，其中模型重排 {k3['model_seconds']:.2f} 秒。"
                "同一查询在服务默认 K=5 下也复现。候选和分数见 `clean-default-candidate-trace-01/`。",
                "",
            ]


def render_answerability(base, lines, retrieved):
    """Append the answerability evidence in report order."""
    score_probe = load(base / "rerank-score-answerability-01/summary.json")
    if score_probe and score_probe["successes"] == score_probe["planned"]:
        splits = score_probe["splits"]
        lines += [
            "### 重排阈值与答案区间",
            "",
            f"对原始公开段落实际入库，并发出 {score_probe['successes']} 次默认 CPU 检索。每篇文章按冻结规则选取同段落"
            "的一条有答案题和一条无答案题。查询仅授权该题的原始段落，使用知识工具的 K=3。"
            "数据源为 [SQuAD 2.0](https://aclanthology.org/P18-2124/)。",
            "",
            "| 文章分组 | 有答案题返回原文答案区间 | 无答案题返回段落 | 无答案题最高分高于同组有答案题 |",
            "| --- | ---: | ---: | ---: |",
        ]
        for label, split in (("开发", "train"), ("诊断留出", "held")):
            row = splits[split]
            lines.append(
                f"| {label} | {row['span_found']}/{row['answerable']} | "
                f"{row['unanswerable_context_returned']}/{row['unanswerable']} | "
                f"{row['unanswerable_outscores_answerable']}/{row['answerable']} |"
            )
        floor = [
            next(x for x in row["score_sweep"] if x["threshold"] == 0.001)
            for row in splits.values()
        ]
        added_answers = sum(x["answerable_score_pass"] for x in floor) - sum(
            row["span_found"] for row in splits.values()
        )
        added_contexts = sum(x["unanswerable_score_pass"] for x in floor) - sum(
            row["unanswerable_context_returned"] for row in splits.values()
        )
        lines += [
            "",
            f"以实际分数复算阈值 0.001 时，有答案题多 {added_answers} 条达到阈值，无答案题多 {added_contexts} 条。"
            "同一阈值难以区分相关背景和答案证据，默认阈值保留。无答案题返回段落只记录检索行为，"
            "生成与幻觉未测。计划、逐题 HTTP 回执和分数见 `rerank-score-answerability-01/`。",
            "",
        ]
    lines += [
        "## 开发诊断，不能替代正式验收",
        "",
        "既有 ContractNLI 194 题的真实候选/真实模型分数回放中，直接叠加四路召回使 Recall 从 0.6967 降至 0.6864；未据此推广‘通道越多一定更准’。可选标题先验回放为 Recall 0.8789、MRR 0.7598、nDCG@10 0.7878，仍未达标，也不是新一轮 HTTP 验收。",
        "",
    ]
    if retrieved:
        lines += [
            "新语料的真实 HTTP 开发检索：",
            "",
            "| 分组 | 题数 | Recall 已评分均值/缺失 | MRR 已评分均值/缺失 | nDCG 已评分均值/缺失 |",
            "| --- | ---: | --- | --- | --- |",
        ]
        for row in retrieved:
            values = []
            for key in ("recall_at_10", "mrr", "ndcg_at_10"):
                item = row["metrics"][key]
                value = item["observed_mean"]
                values.append(
                    (f"{value:.4f}" if value is not None else "未测")
                    + f" / {item['missing_or_unverifiable']}"
                )
            lines.append(f"| {row['cohort']} | {row['planned']} | " + " | ".join(values) + " |")
        lines.append("")


def render_development_retrieval(base, lines):
    """Append the development retrieval evidence in report order."""
    batch = load(base / "reranker-batch-real-http-01/summary.json")
    if batch:
        latency = batch["diagnostic_serial_p95_seconds"]
        latency_note = (
            f"预热后串行诊断 P95 为 {latency:.3f} 秒。"
            if latency is not None
            else "预热后 P95 未测。"
        )
        lines += [
            "默认 FastEmbed 调用已限制单批大小并在内存分配失败时缩小批次，仍对同一候选集逐项打分。"
            f"使用原 CUDA 配置和原合同开发题复测真实 `/search`，HTTP 成功 {batch['successes']}/{batch['planned']}，"
            f"原 500 恢复 {batch['previous_error_recovered']}/{len(batch['prior_errors'])}。"
            + latency_note
            + "本次覆盖原故障配置，默认部署的完整性能验收仍未完成。逐题回执见 `reranker-batch-real-http-01/`。",
            "",
        ]
        paired = load(base / "reranker-batch-real-http-01/paired-and-stage-summary.json")
        if paired:
            comparison = paired["paired_metrics"]["recall_at_10"]
            stage = paired["warm_medians_seconds"]
            lines += [
                f"两轮共同有分的 {paired['paired_scored_cases']} 题 Recall@10 为 "
                f"{comparison['before']:.4f} → {comparison['after']:.4f}。"
                f"按日志与客户端耗时估算，预热后到 Qdrant 返回的中位数为 {stage['until_qdrant_response_seconds']:.3f} 秒，"
                f"其后直到客户端收齐响应约 {stage['after_qdrant_to_http_seconds_estimated']:.3f} 秒，含重排与响应开销。",
                "",
            ]
    legal = load(base / "legal-two-stage-development-01/summary.json")
    if legal:
        row = legal["variants"]["1"]
        values = row["metrics"]
        lines += [
            f"法律两阶段词法检索在 {row['planned']} 道开发题上评分完整。标题与合同开头将正确来源排首的比例为 "
            f"{row['source_hit_rate']:.2%}，条款 Recall@10 / MRR / nDCG@10 为 "
            f"{values['recall_at_10']:.4f} / {values['mrr']:.4f} / {values['ndcg_at_10']:.4f}。"
            "全库词法按标题加权的最高 Recall@10 为 0.7339。现有 Parent 可完整覆盖 87/89 个标注区间。",
            "",
        ]
    legal_rerank = load(base / "legal-clause-rerank-01/summary.json")
    if legal_rerank:
        values = legal_rerank["variants"]["clause-rerank-rrf"]["metrics"]
        lines += [
            f"合同内 Child 重排与词法融合完成全部 60 道开发题，Recall@10 / MRR / nDCG@10 为 "
            f"{values['recall_at_10']:.4f} / {values['mrr']:.4f} / {values['ndcg_at_10']:.4f}。"
            "此结果来自真实现有重排模型和原始语料开发实验。逐题回执见 `legal-clause-rerank-01/`。",
            "",
        ]
    legal_windows = load(base / "legal-clause-window-development-01/summary.json")
    if legal_windows:
        values = legal_windows["variants"]["windows-cue1.2-backup1"]["metrics"]
        lines += [
            f"合同内短窗口与条款措辞探索保持 K=10 及原有 Parent 评分单元。60 道开发题的最好观察值为 "
            f"{values['recall_at_10']:.4f} / {values['mrr']:.4f} / {values['ndcg_at_10']:.4f}。"
            "该试验使用公开条款类别描述与开发题失败样例，默认产品路径未接入，泛化能力未确认。"
            "逐题证据见 `legal-clause-window-development-01/`。",
            "",
        ]
    experiment = load(base / "evals-native-baseline-01/result.json")
    if experiment:
        eval_rows = load(base / "evals-native-baseline-01/metrics.json", [])
        selection = [
            r
            for r in eval_rows
            if r["run"] == "baseline:selection" and r["metric"] == "recall_at_10"
        ]
        planned = sum(r["planned"] for r in selection)
        scored = sum(r["scored"] for r in selection)
        lines += [
            f"原 Evals/Inspect 实验状态为 `{experiment['status']}`，Selection 有效检索评分 {scored}/{planned}。"
            "原始均值与缺失数见 `evals-native-baseline-01/metrics.json`。",
            "",
        ]


def render_generation_scores(base, labels, lines):
    """Append the generation scores evidence in report order."""
    generated = base / "train-generation-01/scores.jsonl"
    if generated.exists():
        rows = read_jsonl(generated)
        lines += [
            "真实生成开发评分（每格为已评分均值 / 已评分数；解析错误和缺失不被当成通过）：",
            "",
            "| 分组 | 已处理回答 | 指标 | 均值 / 已评分数 |",
            "| --- | ---: | --- | --- |",
        ]
        for group in ("zh-general", "en-medical", "en-legal"):
            selected = [r for r in rows if r["cohort"] == group]
            for key in (
                "context_precision",
                "context_recall",
                "faithfulness",
                "hallucination_rate",
                "answer_relevancy",
                "answer_correctness",
                "groundedness",
                "citation_accuracy",
            ):
                values = [r["scores"][key] for r in selected if key in r["scores"]]
                value = f"{statistics.mean(values):.4f}" if values else "未测"
                lines.append(
                    f"| {group} | {len(selected)} | {labels[key]} | {value} / {len(values)} |"
                )
        lines += [
            "",
            "这些是 Train 小样本诊断；完整计划、逐题错误、模型调用和引用审查在 `train-generation-01/`，不能代替每组 300 个独立来源的正式留出。",
            "",
        ]
    marker_diagnostic = load(base / "train-generation-01/citation-marker-diagnostic.json")
    if marker_diagnostic:
        groups = marker_diagnostic["groups"].values()
        present = sum(group["marker_present"] for group in groups)
        answers = sum(group["http_200"] for group in groups)
        lines += [
            f"原开发集真实聊天中有 {present}/{answers} 条回答包含至少一个方括号知识证据 ID。"
            "这些标记的出现仅用于定位格式缺口。产品指令已明确要求逐条文档事实就地引用本轮证据 ID，"
            "运行时拒绝未发行或空证据对应的知识标记。"
            "仍需真实生成和原文支持核验。回执见 `train-generation-01/citation-marker-diagnostic.json`。",
            "",
        ]
    tool_diagnostic = load(base / "train-generation-01/tool-use-diagnostic.json")
    if tool_diagnostic:
        missing = sum(row["no_retrieval_call"] for row in tool_diagnostic["groups"].values())
        lines += [
            f"逐题轨迹表明 {missing}/{len(tool_diagnostic['cases'])} 条回答未调用知识检索，"
            "其中中文和医学各 6 条。缺少工具证据导致这批回答无法核验上下文指标。"
            "新增可选的知识专用部署模式，按本轮原有证据账本要求检索。"
            "回执见 `train-generation-01/tool-use-diagnostic.json`。",
            "",
        ]


def render_generation_retests(base, lines):
    """Append the generation retests evidence in report order."""
    first_retest = load(base / "citation-generation-retest-01/citation-marker-pairs.json")
    if first_retest and first_retest["summary"]["candidate"]["answered_with_captured_trace"] == 0:
        lines += [
            f"首轮生成复测的 {first_retest['planned']} 条请求均失败。"
            "隔离副本的模型注册表仍保存原实例的本机端口，实际模型调用返回 502。"
            "WebUI 返回的 HTTP 错误及核心服务日志均保留在 `citation-generation-retest-01/`。",
            "",
        ]
    generation_retest = load(base / "citation-generation-retest-02/citation-marker-pairs.json")
    first_completed = load(base / "citation-generation-retest-02/first-completed-case.json")
    if first_completed:
        lines += [
            f"隔离库通过管理员 HTTP 更新模型地址后，本地模型首题返回 HTTP {first_completed['status']}，"
            f"轨迹有 {first_completed['model_visible_passages']} 段模型可见证据。"
            f"耗时 {first_completed['seconds']:.2f} 秒，最终返回为应用证据回退。"
            "此回执证明实际取证，未计作模型成功回答。见 "
            "`citation-generation-retest-02/first-completed-case.json`。",
            "",
        ]
    if generation_retest:
        left = generation_retest["summary"]["control"]
        right = generation_retest["summary"]["candidate"]
        lines += [
            f"同一组 {generation_retest['planned']} 条公开语料开发题完成生成复测。"
            f"有本轮模型可见上下文的条数从 {left['with_model_visible_context']} "
            f"变为 {right['with_model_visible_context']}。"
            f"答案包含本轮有效标记且没有未知标记的条数从 {left['all_markers_are_turn_ids']} "
            f"变为 {right['all_markers_are_turn_ids']}。"
            "该项只核验标记和本轮授权 ID，逐条事实语义仍需裁决。"
            "回执见 `citation-generation-retest-02/`。",
            "",
        ]
    generation_stage = load(base / "citation-generation-retest-02/development-stage-summary.json")
    context_spans = load(base / "citation-generation-retest-02/model-visible-span-diagnostic.json")
    if generation_stage and context_spans:
        lines += [
            "| 分组 | HTTP 200 / 计划 | 模型完成 | 应用证据回退 | 未完成知识检索 |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
        for cohort, group in sorted(generation_stage["groups"].items()):
            lines.append(
                f"| {cohort} | {group.get('http_200', 0)}/{group['planned']} | "
                f"{group.get('model', 0)} | {group.get('application-grounding-fallback', 0)} | "
                f"{group.get('missing_required_knowledge', 0)} |"
            )
        low, high = generation_stage["successful_latency_range_seconds"]
        legal = context_spans["cohorts"]["en-legal"]
        lines += [
            "",
            f"成功 HTTP 请求耗时 {low:.2f}–{high:.2f} 秒。"
            f"{generation_stage['requests_at_or_above_5_seconds']}/{generation_stage['planned']} 条请求"
            "耗时达到或超过 5 秒。该组是开发复测。",
            f"法律组模型可见上下文中 {legal['with_any_complete_span']}/{legal['cases_with_official_spans']} "
            f"题含至少一个完整官方证据区间，{legal['with_all_complete_spans']}/{legal['cases_with_official_spans']} "
            "题覆盖全部区间。此项为原文区间诊断，Context Recall 按原冻结评分器另算。",
            "原始回执及哈希见 `citation-generation-retest-02/development-stage-summary.json` "
            "和 `model-visible-span-diagnostic.json`。",
            "",
        ]


def render_remaining_evidence(base, lines):
    """Append the remaining evidence evidence in report order."""
    review_packet = load(base / "human-review-development-01/coordinator-index.json")
    if review_packet:
        lines += [
            f"独立人工盲审流程已有开发题包。{review_packet['ready_for_review']} 条题包条目来自本轮真实聊天"
            f"及授权证据，开发计划其余 {review_packet['unavailable']} 条未评审项留在协调索引。"
            "题包内没有 LLM Judge 判定或参考答案。本轮仍无独立人工评分，一致率未测。"
            "题包见 `human-review-development-01/`。",
            "",
        ]
    for name in ("judge-calibration-01", "retrieval-load-01", "chat-load-01"):
        value = load(base / name / "summary.json")
        if value is not None:
            lines += [
                f"`{name}` 实际摘要（开发轮，原始失败不删）：",
                "",
                "```json",
                json.dumps(value, ensure_ascii=False, indent=2),
                "```",
                "",
            ]
    lines += [
        "## 证据入口与尚缺工作",
        "",
        "- [开源基座与专业领域调研](../research/nexrag-foundations-20261109.md)。",
        "- [参考项目源码调研](../research/nexus-agent-retrieval.md)：固定提交，区分普通双通道与独立图执行器，未声称运行过参考系统。",
        "- [验收工具说明](../../evals/quality/README.md)：实际上传、HTTP、评分、Evals、外部校准与门禁入口。",
        "- 本轮完整证据根目录：`.local-run/quality-20261109/`；公开原文与问题清单：`public-v4/manifest.json`、`sources.json`、分区 JSONL。",
        "- 原始上传失败、重试、每题 HTTP 回答、模型可见上下文、每次评委请求与原始回复、每次负载请求均单独留存。凭据文件仅在忽略目录中，不公开。",
        "- 尚须适用的高风险与权限 HTTP 用例、足量独立留出、完整负载、独立人工评审及全部分组通过。开发生成与性能结果即使已产生，也不能替代这一完成条件。缺项和失败不会由回归测试、外部校准或拒答替代。",
        "",
    ]
