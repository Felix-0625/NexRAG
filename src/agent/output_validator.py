"""最终答案的输出校验器：未获证据支撑的答案会被要求重试。"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from pydantic_ai import ModelRetry, RunContext

from src.agent.dependencies import AgentDeps

if TYPE_CHECKING:
    from src.agent.metric_results import BusinessReport


def _render_business_answer(ctx: RunContext[AgentDeps], answer):
    """将结构化报告或已就绪的证据渲染为答案正文。"""
    from src.agent.metric_results import (
        BusinessReport,
        business_report_ready,
        render_business_body,
        report_from_evidence,
    )

    if isinstance(answer, BusinessReport):
        try:
            return render_business_body(answer, ctx.deps.ledger)
        except ValueError as error:
            raise ModelRetry(str(error)) from error
    if business_report_ready(ctx.deps.ledger) or any(
        entry["kind"] == "metric" for entry in ctx.deps.ledger.entries
    ):
        if missing := ctx.deps.ledger.missing():
            raise ModelRetry(
                "Gather missing evidence before the report is available: "
                + ", ".join(sorted(missing))
                + ". "
                + ctx.deps.ledger.retry_hint()
            )
        return render_business_body(report_from_evidence(ctx.deps.ledger), ctx.deps.ledger)
    return answer


def _link_exports(ctx: RunContext[AgentDeps], answer: str) -> str:
    """确保本轮导出的文件下载链接出现在答案中。"""
    for exported in ctx.deps.exports:
        url = exported["download_url"]
        answer = re.sub(
            r"\]\((?:https?://[^/\s)]+)?" + re.escape(url) + r"\)",
            lambda _: "](" + url + ")",
            answer,
        )
        if "](" + url + ")" not in answer:
            answer += f"\n\n[{exported['filename']}]({url})"
    return answer


def register_output_validator(agent) -> None:
    @agent.output_validator
    def require_verified_evidence(ctx: RunContext[AgentDeps], answer: str | BusinessReport) -> str:
        """Retry an unsupported final answer after failed or skipped tools.

        Schema inspection alone does not establish a business fact. Successful
        query or retrieval calls are tracked per run, so a failed SQL attempt
        cannot become an invented authorization or data conclusion.
        """
        answer = _render_business_answer(ctx, answer)
        denials = [
            e["data"]["message"] for e in ctx.deps.ledger.entries if e["kind"] == "business_denial"
        ]
        if denials:
            answer = "\n".join(dict.fromkeys(denials)) + "\n\n" + answer
        if issues := ctx.deps.ledger.issues(answer):
            ctx.deps.ledger.rejections += 1
            raise ModelRetry(
                "Answer failed evidence checks: "
                + "; ".join(issues)
                + ". "
                + ctx.deps.ledger.retry_hint()
                + " Answer only from this turn's evidence. Do not fill gaps with examples."
            )
        return _link_exports(ctx, answer)
