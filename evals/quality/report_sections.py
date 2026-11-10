"""Render traceable quality evidence without promoting development results."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def load(path: Path, default=None):
    """Read optional JSON evidence, returning the supplied default when absent."""
    return json.loads(path.read_text("utf-8-sig")) if path.exists() else default


def render_acceptance_status(
    base, chinese_holdout, dataset, ended, failures, labels, protocol, recovered, uploads
):
    """Append the acceptance status evidence in report order."""
    lines = []
    lines = [
        "# NexRAG 严格指标验收记录",
        "",
        f"更新时间：{datetime.now().astimezone().isoformat(timespec='seconds')}。**尚未验收通过，目标未完成。**",
        "",
        "已先完成参考项目源码调研，再进行隔离实测和按失败定位的小改动。保留原有工作区改动与全部失败；未提交、推送或发布。",
        "",
        "## 正式验收状态",
        "",
        "独立留出集尚未完成正式全流程实验。以下正式状态不会由训练集、回放、单元测试或外部人工标注校准替代。",
        "",
        "| 指标 | 严格门槛 | 正式状态 |",
        "| --- | --- | --- |",
    ]
    for key, threshold in protocol["thresholds"].items():
        state = "未完成正式验收"
        if chinese_holdout and key in {"recall_at_10", "mrr", "ndcg_at_10"}:
            state = "中文留出证据不足（298/300），其他分组未完成"
        elif key == "retrieval_p95_seconds" and (chinese_holdout or {}).get("chinese_only_load"):
            state = "中文负载三组实测低于门槛，混合负载未完成"
        elif key == "human_judge_agreement":
            state = "本轮独立人工未测"
        lines.append(f"| {labels[key]} | {threshold[0]} {threshold[1]} | {state} |")
    lines += [
        "",
        "每个必需语言/数据集分组均须通过；Citation Accuracy 必须每个事实有本轮、可定位、支持结论的局部引用。AI 模仿评审不计独立人工。",
        "",
        "## 冻结数据与边界",
        "",
        "当前电脑：Ryzen 9 7940H、约 16 GB RAM、RTX 4050 Laptop 6141 MiB VRAM；以 `hardware.json` 和实际 `nvidia-smi` 为准。",
        "",
        "| 分组 | 原文来源数 | Train | Selection | 独立留出题/来源组 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for group, row in dataset.get("cohorts", {}).items():
        counts = row["selected_cases"]
        lines.append(
            f"| {group} | {row['sources']} | {counts['train']} | {counts['selection']} | {counts['holdout']} |"
        )
    lines += [
        "",
        "原文全部通过生产上传 API 摄入，问题与答案不进入知识库。按来源组散列分区；正式留出每组 300 个不同来源组。",
        "",
        "CMRC 官方非抽取式答案、无效原文坐标保留为无法核验；不猜坐标、不换容易题。PubMedQA 提供的摘要上下文缺少结论段，关联召回不证明临床安全。公开数据预训练污染不能排除。",
        "",
        "检索固定 K=10；原文区间完整覆盖与冻结 Parent 排序单位各自定义见 [可执行口径](../../evals/quality/protocol-v1.json)。并发 1/5/10，每端点每组至少 1,000 次；完整 HTTP 响应，失败保留，冷请求不冒充冷 P95。",
        "",
        "## 真实运行进度",
        "",
        f"- 第一轮已记录成功原文：{len(uploads)}/2310；已记录失败：{len(failures)}。检查点可能比实时日志最多落后 19 份。",
        f"- 恢复轮已完整验证的原文：{len(recovered)}/2310。仅当全部原文齐全并可核对索引后才发起质量查询。",
        "- 第一轮服务在旧源码进程中运行，后续修复不会热加载。`loaded-source-epoch.zip` 保存启动时源码；恢复轮单独保存谱系和源码。开发轮不冒充从头冻结的正式验收。",
        "",
    ]
    if ended:
        lines.append("原流水线最后一次落盘的状态为 `" + str(ended.get("stage")) + "`。")
        lines.append("")
    interrupted = load(base / "ollama-run-interruption.json")
    if interrupted:
        lines += [
            f"原 Ollama 端到端并发 1 保留 {interrupted['observed']}/1,000 条完整回执，"
            f"部分样本 P95 为 {interrupted['sample_p95_seconds']:.2f} 秒。核查时该轮服务均已离线，"
            "该组没有完整结束回执。原检索负载在预热时出现 HTTP 500。",
            "",
        ]
    return lines


def render_chinese_recall(base, chinese_development, chinese_holdout, lines):
    """Append the chinese recall evidence in report order."""
    if chinese_development or chinese_holdout:
        lines += [
            "## 中文召回改进",
            "",
            "对授权 Qdrant Parent 正文建立中文一至三字 BM25 索引，检索后按当前授权范围重新读取原文。"
            "实验开关为 `ENABLE_ZH_LEXICAL_RETRIEVAL`。中文路径在本轮直接使用词法排序。",
            "",
            "| 分区 | Recall@10 | MRR | nDCG@10 | 评分覆盖 |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
        for evidence in (chinese_development, chinese_holdout):
            for split, values in (evidence or {}).get("splits", {}).items():
                scores = values["scores"]
                recall = scores["recall_at_10"]
                lines.append(
                    f"| {split} | {recall['value']:.4f} | {scores['mrr']['value']:.4f} | "
                    f"{scores['ndcg_at_10']['value']:.4f} | {recall['scored']}/{recall['planned']} |"
                )
        lines += [
            "",
            "同题 Train 基线为 0.6167 / 0.5431 / 0.5619。新路由、语料与评分代码在留出查询前保存指纹。",
            "",
        ]
        lines += [
            "检索回归套件 197 项通过，新增的索引候选实时授权回归另行通过。受限范围 HTTP 复测返回 10 条，全部属于该范围。",
            "",
        ]
        chinese_evals = load(base / "evals-zh-lexical-01/metrics.json", [])
        if chinese_evals:
            coverage = [
                r
                for r in chinese_evals
                if r["cohort"] == "zh-general" and r["metric"] == "recall_at_10"
            ]
            lines += [
                f"Evals/Inspect 再次调用真实 `/search`，中文开发和选择合计评分 "
                f"{sum(r['scored'] for r in coverage)}/{sum(r['planned'] for r in coverage)}。"
                "逐题证据见 `evals-zh-lexical-01/native/`。",
                "",
            ]
        if chinese_holdout:
            lines += [
                "中文留出保留两条缺项。`cmrc:DEV_110_QUERY_3` 无有效官方原文区间。"
                "`cmrc:DEV_261_QUERY_2` 回退到本次未启动的嵌入服务并返回 HTTP 500。"
                "已评分均值高于阈值，完整覆盖门禁仍为证据不足。",
                "",
            ]
            load_rows = chinese_holdout.get("chinese_only_load", {})
            if load_rows:
                lines += [
                    "中文检索完整 HTTP 负载，仅含中文开发题和选择题。首请求建索引约 10 秒。",
                    "",
                    "| 并发 | 成功/计划 | 预热后 P95 |",
                    "| ---: | ---: | ---: |",
                ]
                for group, row in load_rows.items():
                    lines.append(
                        f"| {group} | {row['successes']}/{row['planned']} | {row['p95_seconds'] * 1000:.1f} ms |"
                    )
                lines += [
                    "",
                    "负载期间本机有短时检索回归测试。新路由的中文检索完成实测，混合语种负载和端到端指标仍待验收。",
                    "",
                ]


def render_native_channels(base, lines):
    """Append the native channels evidence in report order."""
    lines += [
        "## 已验证的小改动",
        "",
        "1. 首次写入空 Qdrant 集合后补建/验证过滤索引，修复首次启动因集合不存在而永久跳过索引的问题。真实服务回归见 `first-write-real-http-01/receipt.json`；摄入测试 397 项通过。",
        "2. 原始文件物理存储改用 UUID 加有限扩展名，保留原始显示名和内容。失败的同一份官方长文件名合同已通过真实上传、下载、哈希和 Windows 路径长度回归：`opaque-storage-real-http-02/receipt.json`。完整入库重试另行记录。",
        "3. 可选的明确标题排序先验默认关闭。只在已授权候选内调整顺序，不补造证据、不改变原始分数；检索测试 197 项通过。尚无正式独立通过证据，不默认开启。",
        "",
        "完整 WebUI 四补丁链已在固定上游提交的独立 Git 索引中应用，110 份生成文件均与当前检出一致；没有改动工作树：`patch-chain-check-02/receipt.json`。这些是回归证据，不是 RAG 质量指标。",
        "",
    ]
    five_channels = load(base / "five-channel-http-01/receipt.json")
    if five_channels and five_channels.get("phase") == "done":
        lanes = five_channels["lane_candidates"]
        rows = five_channels["rows"]
        mixed = [row for row in rows if row["kind"] == "mixed"]
        lines += [
            "## 默认原生五路召回",
            "",
            "保留 BGE-M3 稠密和 BM42 稀疏两路，新增正文精确词法、标题与文件名、文档结构链路。"
            "后三路借鉴参考项目的多字段检索与文档章节链路。按点 ID 融合后再重排与展开父块。",
            "",
            f"在旧库隔离副本的 {five_channels['before']['points_count']} 个原有点上创建正文、文件名、章节标题和父块序号索引。"
            f"升级后仍有 {five_channels['after']['points_count']} 个点。"
            f"{sum(row['status'] == 200 for row in rows)}/{len(rows)} 次真实请求返回 HTTP 200。",
            "",
            f"本轮五路累计候选数依次为 {lanes['dense']}、{lanes['sparse']}、{lanes['body_lexical']}、"
            f"{lanes['title_filename']}、{lanes['document_structure']}。"
            f"混合知识库的 {len(mixed)} 次 K=3 请求中，{sum(row['source_found'] for row in mixed)} 次找到预期来源。"
            "医学空结果保留。排除合同知识库的复测没有返回合同来源。逐题回执和各路监控见 `five-channel-http-01/`。",
            "",
        ]
    five_ab = load(base / "five-channel-ab-k10-03/receipt.json")
    if five_ab and five_ab.get("phase") == "done":
        two, five = (five_ab["summary"][name] for name in ("two", "five"))
        lines += [
            f"完整冻结语料上的开发对照固定 K=10、原 Parent 区间评分和同一个 CUDA 重排器。"
            f"两路 HTTP 200 为 {two['http_200']}/{five_ab['planned_each']}，"
            f"五路为 {five['http_200']}/{five_ab['planned_each']}。正式留出尚待实测。",
            "",
            "| 分组 | 两路 Recall / MRR / nDCG | 五路 Recall / MRR / nDCG |",
            "| --- | --- | --- |",
        ]
        for cohort in two["cohorts"]:
            columns = [
                " / ".join(
                    f"{row['mean']:.4f}（{row['scored']}）" if row["mean"] is not None else "未测"
                    for row in variant["cohorts"][cohort].values()
                )
                for variant in (two, five)
            ]
            lines.append(f"| {cohort} | " + " | ".join(columns) + " |")
        lines += [
            "",
            "括号为有效评分题数。结构路加入文档内章节检索后的回执见 `five-channel-ab-k10-03/`。"
            "上一轮仅取相邻父块时，合同组 Recall 为 0.6000，保留于 `five-channel-ab-k10-02/`。"
            "首次全库索引构建因客户端默认等待时间过短而失败，回执保留在 `five-channel-ab-k10-01/`。"
            "索引构建调用已增加有界等待并检查服务端超时状态。",
            "",
        ]


def render_sparse_migration(base, lines):
    """Append the sparse migration evidence in report order."""
    m3_engine = load(base / "m3-sparse-engine-01/receipt.json")
    if m3_engine and m3_engine.get("phase") == "done":
        lines += [
            "### BGE-M3 稀疏模型接入",
            "",
            "稀疏 Provider 增加 `bge-m3`，摄入与查询共用官方 ONNX 编码器和固定版本的稀疏线性头。"
            "正权重按原始 token ID 取最大值，并剔除特殊 token。"
            f"固定模型版本为 `{m3_engine['revision']}`。CPU 实算官方英文示例的词项权重点积为 "
            f"{m3_engine['english_example_dot_product']:.6f}。"
            "实现核对依据为 [官方编码实现](https://github.com/FlagOpen/FlagEmbedding/blob/master/FlagEmbedding/inference/embedder/encoder_only/m3.py)"
            "及 [模型卡](https://huggingface.co/BAAI/bge-m3)。",
            "",
            "旧 BM42 IDF 索引须重建后才能使用 M3 稀疏查询，摄入与查询均加入不兼容检查。"
            "上述完整语料对照仍使用 BM42。M3 全库质量与性能待测，模型例程只证明真实编码已运行。"
            "回执见 `m3-sparse-engine-01/`。",
            "",
        ]
    m3_http = load(base / "m3-five-channel-http-01/receipt.json")
    if m3_http and m3_http.get("phase") == "done":
        rows = m3_http["searches"]
        uploads = m3_http["uploads"]
        lines += [
            f"M3 稀疏配合原有真实稠密模型，在独立空库通过摄入 API 上传 {len(uploads)} 份官方原文，"
            f"成功 {sum(row['status'] == 200 for row in uploads)} 份。"
            f"CPU 五路检索成功 {sum(row['status'] == 200 for row in rows)}/{len(rows)}，"
            f"找到预期来源 {sum(row.get('source_found', False) for row in rows)}/{len(rows)}。"
            f"完整请求耗时范围 {min(row['seconds'] for row in rows):.2f}–{max(row['seconds'] for row in rows):.2f} 秒。"
            "该组仅验证实际接线，K=10 请求保留默认重排上限 3，未用于正式 K=10 验收。"
            "模型配置、原文哈希及逐题回执见 `m3-five-channel-http-01/`。",
            "",
        ]
    m3_marker = load(base / "m3-five-channel-http-02/receipt.json")
    if m3_marker and m3_marker.get("phase") == "done":
        lines += [
            "集合模型标记加入后的独立空库复测完成真实上传与查询，回执见 "
            "`m3-five-channel-http-02/`。摄入 399 项、检索 201 项、原生启动器 23 项回归测试通过。",
            "",
        ]
    m3_migration = load(base / "m3-full-corpus-migration-02/migration-receipt.json")
    if m3_migration is None:
        m3_migration = load(base / "m3-full-corpus-migration-02/progress.json")
    if m3_migration:
        lines += [
            f"全库迁移最新回执阶段为 `{m3_migration['phase']}`，已处理 "
            f"{m3_migration['processed_points']}/{m3_migration['planned_points']} 个点，"
            f"实际编码 {m3_migration['real_encoder_calls']} 次，精确重复输入复用 "
            f"{m3_migration['document_dedup_hits']} 次。目标为独立的 `nexrag_knowledge_bge_m3` 集合。"
            "进度见 `m3-full-corpus-migration-02/`。逐点原文、元数据和稠密向量的最终哈希核对"
            + ("已通过。" if m3_migration["phase"] == "done" else "待完成。")
            + "该迁移使用真实官方权重的 CUDA 编码。首轮迁移在创建集合时因配置对象类型错误而退出，"
            "错误日志保留于 `m3-full-corpus-migration-01/`。",
            "",
        ]


def render_graph_isolation(base, lines):
    """Append the graph isolation evidence in report order."""
    graph_backfill = load(base / "graph-scoped-http-02/graph-build-receipt.json")
    graph_http = load(base / "graph-scoped-http-02/receipt.json")
    graph_timing = load(base / "graph-scoped-http-02/graph-only-timing.json")
    if graph_backfill and graph_backfill.get("evidence_verification", {}).get("passed"):
        graph_stats = graph_backfill["stats"]
        lines += [
            "## 第六路实体 GraphRAG 与分库隔离",
            "",
            "图按物理集合和知识库分别持久化。每条路径最多两跳，单条路径只遍历同一知识库。"
            "多库查询分别检索已授权库，再合并候选。Qdrant 复核每条边的库、文件、版本、内容哈希和原文区间。",
            "",
            f"以 12 份真实上传原文的隔离快照重建 {graph_stats['shards']} 个库图。核验 "
            f"{graph_stats['chunks']} 个点、{graph_stats['mentions']} 处实体提及和 "
            f"{graph_stats['relations']} 条关系原文，证据定位错误为 0。"
            "快照含知识库副本和单文件作用域。回执见 `graph-scoped-http-02/graph-build-receipt.json`。",
            "",
        ]
    if graph_http and graph_http.get("phase") == "done":
        lines += [
            f"真实 K=10 HTTP 查询返回 {graph_http['graph_tagged']} 条带图标记的结果，"
            f"最大跳数为 {graph_http['maximum_returned_graph_hops']}，逐条核验 "
            f"{graph_http['provenance_edges_checked']} 个路径引用。限制为非合同库后，"
            f"返回 {graph_http['restricted']['returned']} 条，均通过当前库范围校验。"
            "诊断首次运行的边核对误将 SQLite Row 与元组直接比较，修正后重新发起请求。"
            "回执见 `graph-scoped-http-02/receipt.json`。",
            "",
        ]
    if graph_timing:
        lines += [
            "同一份 380 点真实语料、同一组公开查询的串行图查询阶段计时，"
            "每种范围和模式各 240 次。分库版使用每线程至多 8 条只读连接。"
            "计时与全库建图并行，覆盖实体识别和 SQLite 检索，端到端 HTTP 性能另行验收。",
            "",
            "| 查询范围 | 共享图 P95 | 分库图 P95 |",
            "| --- | ---: | ---: |",
        ]
        for key, label in (("single_library", "单库"), ("combined_authorized_libraries", "三库")):
            timings = graph_timing["groups"][key]
            lines.append(
                f"| {label} | {timings['shared']['p95_ms']:.2f} ms | "
                f"{timings['per_library']['p95_ms']:.2f} ms |"
            )
        lines += ["", "计时回执见 `graph-scoped-http-02/graph-only-timing.json`。", ""]
    graph_acl = load(base / "graph-live-acl-01/receipt.json")
    if graph_acl and graph_acl.get("phase") == "done":
        lines += [
            "普通账号通过真实 WebUI 账户与共享接口验证授权变更。每轮先读服务端授权快照，"
            "再用该范围执行真实 K=10 检索。合同与医学原文由独立实例上传。",
            "",
            "| 阶段 | 合同库文件接口 | 检索 HTTP | 命中合同原文 | 返回图路径边 |",
            "| --- | ---: | ---: | --- | ---: |",
        ]
        phases = {"before_grant": "授权前", "granted": "授权后", "revoked": "撤销后"}
        for row in graph_acl["rows"]:
            found = "是" if row["legal_source_found"] else "否"
            lines.append(
                f"| {phases[row['phase']]} | {row['direct_legal_file_list_status']} | "
                f"{row['search_status']} | {found} | {row['graph_path_edges']} |"
            )
        lines += [
            "",
            "旧 Agent 上下文使用撤销前的快照刷新时被生产授权逻辑拒绝。"
            "本轮覆盖知识库授权与图边返回，正式安全验收仍需跨租户业务和生成链路。"
            "回执见 `graph-live-acl-01/receipt.json`。",
            "",
        ]


def render_graph_evaluations(base, lines):
    """Append the graph evaluations evidence in report order."""
    graph_full = load(base / "graph-full-evals-03/graph-build-receipt.json")
    if graph_full:
        verification = graph_full.get("evidence_verification", {})
        lines += [
            f"BGE-M3 完整语料分库建图已扫描 {graph_full['scanned']}/{graph_full['expected']} 个点。"
            f"阶段为 `{graph_full['phase']}`。建图回执见 `graph-full-evals-03/graph-build-receipt.json`。",
            "",
        ]
        if verification.get("passed"):
            lines += [
                f"共 {graph_full['stats']['shards']} 个物理作用域分片。与真实 Qdrant 数据逐点核对 "
                f"{verification['chunks']} 个点、{verification['mentions']} 处实体提及和 "
                f"{verification['relations']} 条关系引用，错误 {verification['errors']}。"
                "作用域包含知识库及单文件副本。",
                "",
            ]
        if load(base / "graph-full-evals-03/failure.json"):
            lines += [
                "首轮全图 Evals 启动使用了缺少 Inspect Scout 的解释器，未发出评测查询。"
                "同一启动计划也遗漏了 Evals 要求登记的留出集和反例分区。"
                "对照接续轮复用已核验快照，使用既有 Evals 环境和已冻结四分区清单。"
                "实际只执行训练和选择集各 180 题，两种配置共用模型、语料、K=10 和评分规则。"
                "原错误与接续计划分别留在 `graph-full-evals-03/` 和 `graph-full-evals-04/`。",
                "",
            ]
    graph_metrics = {}
    for variant, label in (("five", "五路"), ("six", "六路")):
        metrics = load(base / f"graph-full-evals-04/{variant}/evals/metrics.json")
        if metrics:
            for metric in metrics:
                key = (label, metric["run"].removeprefix("baseline:"), metric["cohort"])
                graph_metrics.setdefault(key, {})[metric["metric"]] = metric
    if graph_metrics:
        lines += [
            "### 全库 K=10 Evals 开发对照",
            "",
            "| 通道 | 分区 | 组别 | Recall@10 | MRR | nDCG@10 |",
            "| --- | --- | --- | ---: | ---: | ---: |",
        ]
        for (label, split, cohort), observed in graph_metrics.items():
            cells = []
            for metric in ("recall_at_10", "mrr", "ndcg_at_10"):
                row = observed.get(metric)
                if row and row["observed_mean"] is not None:
                    cells.append(f"{row['observed_mean']:.4f} ({row['scored']}/{row['planned']})")
                else:
                    cells.append("未测")
            lines.append(f"| {label} | {split} | {cohort} | {' | '.join(cells)} |")
        lines += [
            "",
            "每种通道、每个分区均计划 60 题/组。表内只列已产出 Evals 终态回执的轮次。"
            "开发结果不替代独立留出和指定并发 HTTP 负载。逐题回执见 `graph-full-evals-04/`。",
            "",
        ]
