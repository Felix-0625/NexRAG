"""Render traceable quality evidence without promoting development results."""

from __future__ import annotations

import json

from evals.quality.metrics import nearest_rank
from evals.quality.report_sections import load


def render_graph_pairing(base, lines):
    """Append the graph pairing evidence in report order."""
    graph_pairs = load(base / "graph-full-evals-04/paired-summary.json")
    if graph_pairs:
        paired_legal = graph_pairs["groups"]["selection:en-legal"]["metrics"]["recall_at_10"]
        p95 = {}
        for variant in ("five", "six"):
            observations = [
                load(path)
                for path in (base / f"graph-full-evals-04/{variant}/evals/native").glob("*.json")
            ]
            if len(observations) == graph_pairs["planned_each"] and all(
                row["status"] == 200 for row in observations
            ):
                p95[variant] = nearest_rank([row["seconds"] for row in observations], 0.95)
        if len(p95) == 2:
            recall_without_graph, recall_with_graph = paired_legal["paired_means"]
            lines += [
                f"两个变体各完成 {graph_pairs['planned_each']} 次 HTTP 请求。开启图召回时，"
                f"法律选择集 Recall@10 由 {recall_without_graph:.4f} 变为 {recall_with_graph:.4f}，"
                f"串行检索 P95 由 {p95['five']:.3f} 秒变为 {p95['six']:.3f} 秒。"
                "这组计时是开发轮，正式混合并发性能仍待测。GraphRAG 作为可选第六路保留。"
                "配对回执见 `graph-full-evals-04/paired-summary.json`。",
                "",
            ]
    legal_gap = load(base / "graph-full-evals-04/legal-evidence-gap.json")
    if legal_gap:
        splits = legal_gap["summary"].values()
        n = sum(group["n"] for group in splits)
        file_hits = sum(group["source_in_k10"] for group in splits)
        evidence_hits = sum(group["any_evidence_in_k10"] for group in splits)
        first_evidence = sum(group["first_evidence_at_1"] for group in splits)
        unrepresentable = sum(
            group["reference_spans"] - group["spans_with_existing_parent"] for group in splits
        )
        lines += [
            f"六路法律组 {file_hits}/{n} 题在前十返回正确文件，{evidence_hits}/{n} 题返回至少一条完整证据，"
            f"{first_evidence}/{n} 题把证据排到第一。另有 {unrepresentable} 条标注证据跨越现有父块边界。"
            "核对按原始标注区间执行。逐题回执见 `graph-full-evals-04/legal-evidence-gap.json`。",
            "",
        ]


def render_channel_ablation(base, lines):
    """Append the channel ablation evidence in report order."""
    ablation_failure = load(base / "graph-channel-ablation-01/interrupted-by-diagnostic.json")
    if ablation_failure:
        lines += [
            f"文件名消融首轮 {ablation_failure['without_title_http_500']} 个请求均为 HTTP 500。"
            "测试适配器误用 FastEmbed 的同名响应类。该轮没有可用的质量分数。"
            "修复轮改用 Qdrant REST 响应类型，以当前默认五路为对照，"
            "先通过真实 K=10 HTTP 探针，再运行每个消融变体。失败回执和修复轮分别见 "
            "`graph-channel-ablation-01/` 和 `graph-channel-ablation-02/`。",
            "",
        ]
    ablation = load(base / "graph-channel-ablation-02/paired-ablation-summary.json")
    if ablation:
        body = ablation["variants"]["without-body"]
        title = ablation["variants"]["without-title"]
        structure = ablation["variants"]["without-structure"]
        if body["terminal"] and body["received"] == ablation["expected"]:
            body_metrics = body["groups"]["selection:en-legal"]["metrics"]
            title_mrr = title["groups"]["train:zh-general"]["metrics"]["mrr"]
            lines += [
                f"正文与文件名消融各执行 {body['received']} 次实际 K=10 检索。关闭正文通道后，"
                "法律选择组 Recall@10、MRR、nDCG@10 分别为 "
                f"{body_metrics['recall_at_10']['variant']:.4f}、{body_metrics['mrr']['variant']:.4f}、"
                f"{body_metrics['ndcg_at_10']['variant']:.4f}，中文与医学各分组保持原分。"
                f"关闭文件名通道时，中文训练组 MRR 由 {title_mrr['control']:.4f} 变为 "
                f"{title_mrr['variant']:.4f}。结构通道轮保留 {structure['received']}/{ablation['expected']} 条，"
                "没有完整终态回执。原生默认关闭正文通道，文件名和结构保留。"
                "回执见 `graph-channel-ablation-02/paired-ablation-summary.json`。",
                "",
            ]
    four = load(base / "four-route-production-evals-01/without-body/preflight-http.json")
    if four:
        succeeded = sum(row["status"] == 200 for row in four["records"])
        lines += [
            f"默认四路使用生产实现发起真实 K=10 探针，当前 HTTP 200 为 {succeeded}/{four['planned']}。"
            "完整对照及失败回执位于 `four-route-production-evals-01/`。",
            "",
        ]
    four_root = base / "four-route-production-evals-01/without-body/evals"
    four_metrics = load(four_root / "metrics.json")
    if four_metrics and (four_root / "result.json").exists():
        four_legal = {
            row["metric"]: row
            for row in four_metrics
            if row["run"] == "baseline:selection" and row["cohort"] == "en-legal"
        }
        four_rows = [load(path) for path in (four_root / "native").glob("*.json")]
        if len(four_rows) == 360 and all(row["status"] == 200 for row in four_rows):
            p95 = nearest_rank([row["seconds"] for row in four_rows], 0.95)
            scores = [
                four_legal[name]["observed_mean"] for name in ("recall_at_10", "mrr", "ndcg_at_10")
            ]
            lines += [
                f"产品代码的四路开发对照已返回 {len(four_rows)}/360 条 HTTP 200。法律选择组指标为 "
                f"{'、'.join(f'{value:.4f}' for value in scores)}，串行检索 P95 为 {p95:.3f} 秒。"
                "本轮保持原候选预算、稠密和稀疏 BGE-M3、重排模型以及冻结 K=10 口径。"
                "实测采用候选上限 50、重排上限 10、RRF 与父块去重后截断。"
                "新安装模板的重排上限仍为 3，本轮只确认四路产品实现与评测配置组合。"
                "实际并发验收仍未完成。回执见 `four-route-production-evals-01/without-body/evals/`。",
                "",
            ]


def render_candidate_diagnostics(base, lines):
    """Append the candidate diagnostics evidence in report order."""
    legal_trace = load(base / "graph-channel-ablation-02/five/legal-stage-summary.json")
    if legal_trace:
        train = legal_trace["summaries"]["train"]
        if train.get("matched") == train["planned"] and not legal_trace["trace_repeats"]:
            lines += [
                f"默认五路 {train['matched']} 道法律训练题的阶段追踪与真实 HTTP 返回逐题对齐。"
                f"{train.get('absent_from_all_lanes', 0)} 题的正确父块没有出现在任一路，"
                f"{train.get('lost_at_fusion_cap', 0)} 题在融合候选截断时丢失，"
                f"{train.get('below_rerank_threshold', 0)} 题的相关候选均低于重排阈值，"
                f"{train.get('eligible_but_not_returned', 0)} 题有达阈值候选但未进入最终前十，"
                f"{train.get('no_canonical_parent', 0)} 题的证据没有可完整覆盖的父块。"
                "这是开发集阶段诊断。逐题回执见 `graph-channel-ablation-02/five/legal-stage-summary.json`。",
                "",
            ]
    rank_order = load(base / "graph-channel-ablation-02/five/rerank-order-diagnostic.json")
    if rank_order:
        fused = rank_order["summary"]["fused-with-original-rank"]
        raw = rank_order["summary"]["raw-cross-encoder"]
        lines += [
            f"对已记录的真实重排分数复算排序，移除粗召回排序融合只将 MRR 由 {fused['mrr']:.4f} "
            f"提高至 {raw['mrr']:.4f}，Recall@10 保持 {raw['recall_at_10']:.4f}。"
            "复算使用相同候选与分数阈值，用于定位瓶颈。回执见 "
            "`graph-channel-ablation-02/five/rerank-order-diagnostic.json`。",
            "",
        ]
    parent_pool = load(
        base / "graph-channel-ablation-02/without-body/parent-rrf-pool-diagnostic.json"
    )
    if parent_pool:
        selection = parent_pool["summary"]["selection"]
        lines += [
            f"使用四路消融保留的真实候选，法律选择组每题仍分配 50 个粗召回名额。按父块融合时，"
            f"不同父块数中位数从 {selection['baseline_distinct_median']:.0f} 增到 "
            f"{selection['parent_distinct_median']:.0f}，进入候选池的正确证据题数从 "
            f"{selection['baseline_first_hit_cases']}/{selection['cases']} 增到 "
            f"{selection['parent_first_hit_cases']}/{selection['cases']}。"
            "这是基于已记录候选的重排前诊断，最终 K=10 排序需要重新调用真实模型。"
            "复算见 `graph-channel-ablation-02/without-body/parent-rrf-pool-diagnostic.json`，"
            "真实对照入口为 `four-route-parent-fusion-01/`。",
            "",
        ]
    child_geometry = load(
        base / "graph-channel-ablation-02/without-body/child-evidence-geometry.json"
    )
    if child_geometry:
        groups = child_geometry["groups"].values()
        total = sum(group["below_threshold_evidence_spans"] for group in groups)
        contained = sum(group["below_threshold_child_complete"] for group in groups)
        lines += [
            f"四路法律组因重排阈值丢失的题目涉及 {total} 条原文标注，其中 {contained} 条已完整落在"
            "现有某个子块内。这项诊断核对全索引中的子块几何关系，尚不能确认每个完整子块是否实际送入重排。"
            "回执见 `graph-channel-ablation-02/without-body/child-evidence-geometry.json`。",
            "",
        ]


def render_candidate_ceilings(base, lines):
    """Append the candidate ceilings evidence in report order."""
    ceiling = load(base / "four-route-production-evals-01/legal-parent-geometry-ceiling.json")
    if ceiling:
        ceiling_groups = ceiling["summary"]
        train = ceiling_groups["train"]["means"]["recall_at_10"]
        selection = ceiling_groups["selection"]["means"]["recall_at_10"]
        lines += [
            "对法律训练与选择各 60 题，按现有父块和原文区间计算几何上限。"
            f"Recall@10 上限分别为 {train:.4f} 和 {selection:.4f}。"
            "这项诊断直接读取标准答案，说明当前父块允许达到开发集 Recall 门槛。"
            "主要缺口仍在发现并排好证据。回执见 "
            "`four-route-production-evals-01/legal-parent-geometry-ceiling.json`。",
            "",
        ]
    candidate_ceiling = load(
        base / "graph-channel-ablation-02/without-body/legal-candidate-ceiling-with-reserve.json"
    )
    if candidate_ceiling:
        selection = candidate_ceiling["summary"]["selection"]
        train = candidate_ceiling["summary"]["train"]
        lines += [
            "使用真实阶段追踪与官方区间计算候选覆盖上限。法律选择集的 Recall@10 上限在四路并集为 "
            f"{selection['lane_union']['recall_at_10']:.4f}，原 50 条送排候选为 "
            f"{selection['reranker_pool']['recall_at_10']:.4f}。"
            "按首三命中定位文件，在固定预算内保留广召回前半并为同文件未覆盖父块保留后半名额，"
            f"离线估计的训练与选择上限分别为 {train['document_reserve_replay']['recall_at_10']:.4f} 和 "
            f"{selection['document_reserve_replay']['recall_at_10']:.4f}。"
            "诊断使用原文标签和记录的候选，尚需同 K=10 的真实 HTTP 对照。"
            "回执见 `graph-channel-ablation-02/without-body/legal-candidate-ceiling-with-reserve.json`。",
            "",
        ]
    candidate_depth = load(base / "graph-channel-ablation-02/legal-observed-candidate-pools.json")
    if candidate_depth:
        depth = candidate_depth["summary"]["selection"]["control"]["budget_recall_ceiling"]
        lines += [
            f"四路原排序的候选深度诊断中，法律选择集前 20、30、50 条候选对应的 Recall@10 "
            f"上限为 {depth['20']:.4f}、{depth['30']:.4f}、{depth['50']:.4f}。"
            "最终评分始终使用 K=10。回执见 `graph-channel-ablation-02/legal-observed-candidate-pools.json`。",
            "",
        ]
    parent_pair = load(base / "four-route-parent-fusion-01/paired-summary.json")
    if parent_pair:
        legal = parent_pair["groups"]["selection:en-legal"]
        chinese = parent_pair["groups"]["train:zh-general"]["recall_at_10"]
        received = parent_pair["parent_fusion"]["received"]
        planned = parent_pair["planned_per_variant"]
        lines += [
            f"父块融合实测回传 {received}/{planned} 题。法律选择组 Recall@10 从 "
            f"{legal['recall_at_10']['control']:.4f} 降到 {legal['recall_at_10']['variant']:.4f}，MRR 从 "
            f"{legal['mrr']['control']:.4f} 变为 {legal['mrr']['variant']:.4f}，nDCG@10 从 "
            f"{legal['ndcg_at_10']['control']:.4f} 变为 {legal['ndcg_at_10']['variant']:.4f}。"
            f"中文训练组 Recall@10 由 {chinese['control']:.4f} 降至 {chinese['variant']:.4f}。"
            "三道中文退步题的原始高分子块在聚合后被同父块的低分子块替代，产品保持原融合。"
            "配对与失分记录见 `four-route-parent-fusion-01/paired-summary.json` 和 `zh-train-regressions.json`。"
            "仅替换尾部重复候选的独立对照见 `four-route-parent-tail-reserve-01/`。",
            "",
        ]


def render_document_experiments(base, lines):
    """Append the document experiments evidence in report order."""
    scope_failure = load(base / "four-route-document-scope-01/failure.json")
    scope_retry = load(base / "four-route-document-scope-02/without-body/preflight-http.json")
    if scope_failure:
        lines += [
            "### 法律文档内检索与重排",
            "",
            "首轮文档内查询在法律预检返回空证据，Evals 对照未启动。"
            "接续实现保留原候选和原问题，文档内没有命中或全部低于重排阈值时回退广召回。"
            "失败和接续回执见 `four-route-document-scope-01/` 与 `four-route-document-scope-02/`。",
            "",
        ]
        if scope_retry:
            passed = sum(row["status"] == 200 for row in scope_retry["records"])
            lines += [
                f"接续轮的中文、医学和法律预检返回 {passed}/{scope_retry['planned']} 条 HTTP 200，"
                "并通过真实证据返回检查。最终 K 保持 10。",
                "",
            ]
    for directory, label in (
        ("four-route-document-scope-02", "文档内检索回退方案"),
        ("four-route-reference-reranker-01", "BGE-reranker-v2-m3"),
        ("four-route-document-reserve-01", "同文档父块候选补位"),
        ("four-route-reserve-reference-reranker-01", "同文档候选补位与 BGE-reranker-v2-m3"),
        ("four-route-focused-reference-reranker-01", "同一候选池内的分组条款重排"),
    ):
        pair = load(base / directory / "paired-summary.json")
        if not pair:
            continue
        variant = pair["variant"]
        lines += [
            f"**{label}**完成 {variant['received']}/{pair['planned_per_variant']} 次开发集真实 HTTP。"
            f"HTTP 200 为 {variant['http_statuses'].get('200', 0)}/{variant['received']}。"
            f"串行 P95 为 {variant['observed_http_p95_seconds']:.3f} 秒。"
            f"逐题与配对回执见 `{directory}/`。",
            "",
            "| 分组 | Recall@10 基线 → 候选 | MRR 基线 → 候选 | nDCG@10 基线 → 候选 |",
            "| --- | ---: | ---: | ---: |",
        ]
        for group, scores in pair["groups"].items():
            columns = [
                f"{score['control']:.4f} → {score['variant']:.4f}（{score['paired']}）"
                if score["paired"]
                else "无可配对评分"
                for score in scores.values()
            ]
            lines.append(f"| {group} | " + " | ".join(columns) + " |")
        lines += ["", "括号为有效配对数。该轮使用训练和选择集。", ""]
        pools = load(base / directory / "legal-observed-candidate-pools.json")
        if pools:
            selection = pools["summary"]["selection"]
            control_pool = selection["control"]["budget_recall_ceiling"]["50"]
            variant_pool = selection["variant"]["budget_recall_ceiling"]["50"]
            lines += [
                f"法律选择集的实际 50 条送排候选对应的 Recall@10 上限由 "
                f"{control_pool:.4f} 变为 {variant_pool:.4f}。"
                f"新增可用证据父块 {selection['cases_with_added_relevant_parents']} 题，"
                f"丢失原有证据父块 {selection['cases_with_removed_relevant_parents']} 题。"
                f"逐题候选和证据匹配回执见 `{directory}/legal-observed-candidate-pools.json`。",
                "",
            ]
            ranked = selection["variant"]
            if "raw_top_one_relevant" in ranked:
                lines += [
                    f"该组重排阈值后的覆盖上限为 {ranked['eligible_ceiling']:.4f}。"
                    f"按原始重排分数看，{ranked['raw_top_one_relevant']}/{ranked['paired']} 题首位相关，"
                    f"最终 HTTP 首位相关为 {ranked['actual_top_one_relevant']}/{ranked['paired']}。"
                    "阶段分析用于区分候选覆盖、模型排序和结果截断。",
                    "",
                ]


def render_reranker_diagnostics(base, lines):
    """Append the reranker diagnostics evidence in report order."""
    scope_coverage = load(base / "four-route-document-scope-02/coverage-diagnostic.json")
    if scope_coverage:
        scoped = scope_coverage["summary"]["selection"]
        lines += [
            f"法律选择组有 {scoped['scoped_return']} 道题使用文档内结果，"
            f"其中 {scoped['scoped_lt_ten']} 道实际返回少于十个父块。"
            f"选择组 MRR 改善 {scoped['mrr_improved']} 题、下降 {scoped['mrr_regressed']} 题，"
            f"Recall 改善 {scoped['recall_improved']} 题、下降 {scoped['recall_regressed']} 题。"
            "文档内方案保持默认关闭。逐题区间见 `four-route-document-scope-02/coverage-diagnostic.json`。",
            "",
        ]
    reference_preflight = load(
        base / "four-route-reference-reranker-01/without-body/preflight-http.json"
    )
    if reference_preflight:
        passed = sum(row["status"] == 200 for row in reference_preflight["records"])
        lines += [
            f"固定版本的 BGE-reranker-v2-m3 社区 FP16 ONNX 导出完成 {passed}/"
            f"{reference_preflight['planned']} 条真实 HTTP 预检。"
            "同语料、同四路候选、同 K=10，替换重排模型，沿用原过滤和排序参数。"
            "权重哈希、运行配置与原始回执位于 `four-route-reference-reranker-01/`。",
            "",
        ]
    reference_timing = load(base / "four-route-reference-reranker-01/latency-attribution.json")
    if reference_timing:
        stages = reference_timing["runs"]["four-route-reference-reranker-01"]["stages"]
        lines += [
            f"v2-m3 服务侧阶段均值为稠密 {stages['embed_dense']['mean_seconds'] * 1000:.0f} ms、"
            f"稀疏 {stages['embed_sparse']['mean_seconds'] * 1000:.0f} ms、"
            f"Qdrant {stages['qdrant']['mean_seconds'] * 1000:.0f} ms、"
            f"重排 {stages['rerank']['mean_seconds'] * 1000:.0f} ms。"
            "阶段均值含预检和失败样本。回执见 `four-route-reference-reranker-01/latency-attribution.json`。",
            "",
        ]
    returned_order = load(base / "four-route-reference-reranker-01/returned-order-diagnostic.json")
    if returned_order:
        selection = returned_order["runs"]["four-route-reference-reranker-01"]["summary"][
            "selection"
        ]
        lines += [
            f"仅对 v2-m3 已真实返回的前十证据按原始重排分数排序，法律选择集的 MRR 由 "
            f"{selection['actual_mrr']:.4f} 复算为 {selection['raw_mrr']:.4f}。"
            f"仍只有 {selection['hit_at_10']}/{selection['scored']} 题至少返回一条完整证据。"
            "该诊断保持返回集合，无法恢复被截断的候选。回执见 "
            "`four-route-reference-reranker-01/returned-order-diagnostic.json`。",
            "",
        ]
    proxy_failure = load(base / "four-route-reference-reranker-01/qdrant-proxy-timeout.json")
    if proxy_failure:
        lines += [
            f"法律选择集 {proxy_failure['case_id']} 返回 HTTP 500。"
            "服务堆栈显示本机 Qdrant 查询进入 HTTP 代理后连接超时。"
            "原失败和 59 道可配对题均保留。检索客户端现在对本机明文回环连接禁用环境代理。",
            "",
        ]
    proxy_check = load(base / "four-route-document-reserve-01/qdrant-proxy-check.json")
    if proxy_check and proxy_check.get("returncode") == 0:
        result = json.loads(proxy_check["stdout"])
        if result.get("proxied_control") == "ConnectError" and result.get(
            "direct_local_collection_exists"
        ):
            lines += [
                "在隔离快照上故意设置不可达 HTTP 代理。对照客户端连接失败，产品客户端绕过代理，"
                "成功读取实际 Qdrant 集合。回执见 `four-route-document-reserve-01/qdrant-proxy-check.json`。",
                "",
            ]


def render_boundary_diagnostics(base, lines):
    """Append the boundary diagnostics evidence in report order."""
    focused_preflight = load(
        base / "four-route-focused-reference-reranker-01/without-body/preflight-http.json"
    )
    if focused_preflight:
        passed = sum(row["status"] == 200 for row in focused_preflight["records"])
        lines += [
            f"分组条款重排的真实 HTTP 预检为 {passed}/{focused_preflight['planned']}。"
            "同一文件版本内按条款问题重排，其他候选按原问题重排，复用现有候选池。"
            "逐题回执见 `four-route-focused-reference-reranker-01/`。",
            "",
        ]
    focused_pair = load(
        base
        / "four-route-focused-reference-reranker-01/paired-vs-four-route-reserve-reference-reranker-01.json"
    )
    focused_output = load(
        base / "four-route-focused-reference-reranker-01/legal-output-diagnostic.json"
    )
    if focused_pair and focused_output:
        recall = focused_pair["groups"]["selection:en-legal"]["recall_at_10"]
        counts = focused_output["groups"]["selection"]
        lines += [
            f"与同模型及同候选补位的对照相比，分组重排使法律选择集 Recall@10 从 "
            f"{recall['control']:.4f} 降至 {recall['variant']:.4f}。"
            f"结果少于十个父块的题从 {counts['combined']['below_ten']} 增至 {counts['focused']['below_ten']}，"
            f"无相关父块的题从 {counts['combined']['no_relevant']} 增至 {counts['focused']['no_relevant']}。"
            "该实验已从运行代码移除。冻结源码、配置和回执保留在本轮实验目录。",
            "",
        ]
    lexical = load(base / "scoped-bm25-child-lane-01/summary.json")
    if lexical:
        lines += [
            "### 正文通道与父块边界诊断",
            "",
            "在独立 Qdrant 以已授权的原始子块建立稀疏 BM25，查询侧 IDF 统计限定在授权库。"
            "在同一组训练问题中对比现有正文过滤与 BM25 排序。每路保留至多 32 个子块，"
            "去重还原原父块，再以冻结评分器计算前 10 个父块的结果。",
            "",
            "| 组别 | 题数 | 现有正文正确文件命中率 | BM25 正确文件命中率 | BM25 Recall@10 | BM25 MRR | BM25 nDCG@10 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for cohort, row in lexical["cohorts"].items():
            scores = row["bm25"]["k10_metrics"]
            lines.append(
                f"| {cohort} | {row['planned']} | {row['old']['candidate_source_hit']:.1%} | "
                f"{row['bm25']['candidate_source_hit']:.1%} | {scores['recall_at_10']:.4f} | "
                f"{scores['mrr']:.4f} | {scores['ndcg_at_10']:.4f} |"
            )
        lines += [
            "",
            "该对照仅隔离正文一路，未执行多路融合或交叉重排。"
            "对照中的旧索引查询与全库开发评测同时运行。逐题回执见 `scoped-bm25-child-lane-01/`。",
            "",
        ]
    overlap = load(base / "parent-overlap-geometry-01/summary.json")
    if overlap and not overlap["baseline_mismatches"]:
        old, small, large = (overlap["cohorts"]["en-legal"][str(size)] for size in (0, 80, 160))
        growth = small["copied_parent_utf8_bytes"] / old["copied_parent_utf8_bytes"] - 1
        lines += [
            "实际原生转换器与切分器复现了训练集原始父块目录。法律训练组的 89 条原文证据，"
            f"父块重叠 0、80、160 token 分别完整覆盖 {old['coverable_spans']}、{small['coverable_spans']}、"
            f"{large['coverable_spans']} 条。80 token 的法律组子块数从 {old['children']} 增至 {small['children']}，"
            f"重复存入子块的父块文本字节增加 {growth:.1%}。"
            "这是原文边界和载荷诊断。当前冻结父块目录及实测索引继续使用重叠 0。"
            "回执见 `parent-overlap-geometry-01/`。",
            "",
        ]
