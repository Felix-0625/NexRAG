"""长期记忆的分级冲突消解（L0-L4）。

能用确定性规则判定时一律用规则；可选的 LLM 仲裁器只用于模糊情形，仲裁器
一旦失败就按 L4（争议）处理，因此模型出错也不会悄悄删除知识。所有状态转换
都是软删除：被取代/争议的记录保留用于审计和回滚，只有用户明确"遗忘"才会物理删除。

    L0 duplicate      -> NOOP (refresh confidence/access)
    L1 complementary  -> new version, old one superseded
    L2 temporal update-> old valid_to=now, new supersedes old (bi-temporal)
    L3 explicit fix   -> user_explicit always wins over lower-ranked sources
    L4 contradiction  -> both marked disputed, surfaced for user confirmation
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal

from src.memory.models import SOURCE_RANK, MemoryCandidate, MemoryRecord, normalize
from src.memory.vectors import cosine

Level = Literal["L0", "L1", "L2", "L3", "L4", "ADD"]

DUPLICATE_SIMILARITY = 0.95
# 时间间隔如此之近且可信度相同的两次观察，视为真正的矛盾。
CONTRADICTION_WINDOW_SECONDS = 3_600.0
_CORRECTION = re.compile(
    r"(不对|不是|错了|更正|纠正|其实|改成|改为|现在是|不再|已经换|actually|correction|no longer|not anymore|changed to)",
    re.IGNORECASE,
)


@dataclass
class Decision:
    level: Level
    target_id: str | None
    reason: str


Adjudicator = Callable[[MemoryCandidate, MemoryRecord], Awaitable[str]]


def is_explicit_correction(utterance: str) -> bool:
    """启发式判断"用户正在更正先前的说法"。"""
    return bool(_CORRECTION.search(utterance or ""))


def _same_value(a: str, b: str) -> bool:
    return normalize(a) == normalize(b)


def _contains(longer: str, shorter: str) -> bool:
    return normalize(shorter) in normalize(longer) and len(normalize(longer)) > len(
        normalize(shorter)
    )


async def decide(
    candidate: MemoryCandidate,
    slot_matches: list[MemoryRecord],
    similar: list[tuple[MemoryRecord, float]],
    *,
    candidate_vector: list[float] | None = None,
    record_vectors: dict[str, list[float]] | None = None,
    observed_at: float,
    utterance: str = "",
    adjudicator: Adjudicator | None = None,
) -> Decision:
    """针对已有记忆，为候选选出一个处理动作。

    ``slot_matches`` 是规范化后 subject+attribute 相同的 active/disputed 记忆；
    ``similar`` 是向量近邻及其分数。
    """
    record_vectors = record_vectors or {}

    # L0 重复：槽位与值都相同，或向量近邻几乎一致。
    for record in slot_matches:
        if _same_value(record.value_text, candidate.value_text):
            return Decision("L0", record.id, "same slot and value")
    for record, score in similar:
        if score >= DUPLICATE_SIMILARITY and (
            _same_value(record.value_text, candidate.value_text)
            or (
                candidate_vector
                and record.id in record_vectors
                and cosine(candidate_vector, record_vectors[record.id]) >= DUPLICATE_SIMILARITY
            )
        ):
            return Decision("L0", record.id, f"near-duplicate (similarity {score:.2f})")

    if not slot_matches:
        return Decision("ADD", None, "no memory occupies this slot")

    current = slot_matches[0]  # 该槽位的最新版本
    new_rank = SOURCE_RANK.get(candidate.source, 0)
    old_rank = SOURCE_RANK.get(current.source, 0)
    explicit = candidate.source == "user_explicit" or is_explicit_correction(utterance)

    # L3 明确更正：优先于任何更弱或同等级的来源。
    if explicit and new_rank >= old_rank:
        return Decision("L3", current.id, "user explicitly corrected this fact")

    # 推断出的记忆绝不能覆盖用户明确说过的内容。
    if new_rank < old_rank:
        return Decision(
            "L4", current.id, f"lower-trust {candidate.source} contradicts {current.source}"
        )

    # L1 互补：新值是对旧值的细化/扩展。
    if _contains(candidate.value_text, current.value_text):
        return Decision("L1", current.id, "new value extends the existing one")

    # L2 时间演变：可信度相同但明显更晚的证据取代旧值。
    if observed_at - current.valid_from > CONTRADICTION_WINDOW_SECONDS:
        return Decision("L2", current.id, "value changed over time")

    # 模糊情形：可信度相同、时间相近、值不同。
    if adjudicator is not None:
        try:
            verdict = (await adjudicator(candidate, current)).strip().upper()
        except Exception:  # noqa: BLE001 - 失败时安全降级为争议
            verdict = ""
        if verdict in {"L1", "L2"}:
            return Decision(verdict, current.id, "resolved by adjudicator")  # type: ignore[arg-type]
        if verdict == "L0":
            return Decision("L0", current.id, "adjudicator judged equivalent")
    return Decision(
        "L4", current.id, "conflicting values with equal trust; needs user confirmation"
    )
