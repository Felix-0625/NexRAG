"""Compile traceable evidence without promoting development results."""

from __future__ import annotations

import argparse
from pathlib import Path

from evals.quality.report_diagnostics import (
    render_boundary_diagnostics,
    render_candidate_ceilings,
    render_candidate_diagnostics,
    render_channel_ablation,
    render_document_experiments,
    render_graph_pairing,
    render_reranker_diagnostics,
)
from evals.quality.report_generation import (
    render_answerability,
    render_default_diagnostics,
    render_development_retrieval,
    render_generation_retests,
    render_generation_scores,
    render_remaining_evidence,
)
from evals.quality.report_sections import (
    load,
    render_acceptance_status,
    render_chinese_recall,
    render_graph_evaluations,
    render_graph_isolation,
    render_native_channels,
    render_sparse_migration,
)

ROOT = Path(__file__).resolve().parents[2]


def publish(base: Path, output: Path) -> None:
    """Publish ordered evidence from base to output without changing acceptance status."""
    protocol = load(Path(__file__).with_name("protocol-v1.json"))
    dataset = load(base / "public-v4/manifest.json", {})
    uploads = load(base / "native-baseline-01/uploads.json", {})
    failures = load(base / "native-baseline-01/upload-failures.json", [])
    recovered = load(base / "native-recovered-01/uploads.json", {})
    retrieved = load(base / "train-ranking-01/summary.json", [])
    ended = load(base / "pipeline-01/status.json", {})
    chinese_development = load(base / "zh-lexical-real-http-01/summary.json")
    chinese_holdout = load(base / "zh-lexical-holdout-01/summary.json")
    labels = {
        "recall_at_10": "Recall@10",
        "mrr": "MRR（实际 K=10）",
        "ndcg_at_10": "nDCG@10",
        "context_precision": "Context Precision",
        "context_recall": "Context Recall",
        "faithfulness": "Faithfulness",
        "hallucination_rate": "回答级 Hallucination Rate",
        "answer_relevancy": "Answer Relevancy",
        "answer_correctness": "Answer Correctness",
        "groundedness": "端到端 Groundedness",
        "citation_accuracy": "Citation Accuracy",
        "retrieval_p95_seconds": "检索 P95（秒）",
        "e2e_p95_seconds": "端到端 P95（秒）",
        "human_judge_agreement": "本轮独立人工与 LLM Judge 一致率",
    }
    lines = render_acceptance_status(
        base, chinese_holdout, dataset, ended, failures, labels, protocol, recovered, uploads
    )
    render_chinese_recall(base, chinese_development, chinese_holdout, lines)
    render_native_channels(base, lines)
    render_sparse_migration(base, lines)
    render_graph_isolation(base, lines)
    render_graph_evaluations(base, lines)
    render_graph_pairing(base, lines)
    render_channel_ablation(base, lines)
    render_candidate_diagnostics(base, lines)
    render_candidate_ceilings(base, lines)
    render_document_experiments(base, lines)
    render_reranker_diagnostics(base, lines)
    render_boundary_diagnostics(base, lines)
    render_default_diagnostics(base, lines)
    render_answerability(base, lines, retrieved)
    render_development_retrieval(base, lines)
    render_generation_scores(base, labels, lines)
    render_generation_retests(base, lines)
    render_remaining_evidence(base, lines)
    output.write_text("\n".join(lines), "utf-8")


def main() -> None:
    """Run the command-line workflow with the supplied options."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=ROOT / ".local-run/quality-20261109")
    parser.add_argument(
        "--output", type=Path, default=ROOT / "docs/acceptance/rag-quality-20261109.md"
    )
    args = parser.parse_args()
    publish(args.base, args.output)


if __name__ == "__main__":
    main()
