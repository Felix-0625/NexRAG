"""Resolve memory conflicts through deterministic L0-L4 rules.

Use model arbitration only for ambiguity; failure means disputed, never silent deletion.
Preserve superseded and disputed records until explicit forgetting. L0 refreshes duplicates,
L1 adds refinements, L2 records temporal changes, L3 favors corrections, and L4 asks the user.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal

from src.memory.models import SOURCE_RANK, MemoryCandidate, MemoryRecord, normalize
from src.memory.vectors import cosine

Level = Literal["L0", "L1", "L2", "L3", "L4", "ADD"]

DUPLICATE_SIMILARITY = 0.95
# Equally trusted observations this close in time are genuine contradictions.
CONTRADICTION_WINDOW_SECONDS = 3_600.0
_CORRECTION = re.compile(
    r"(不对|不是|错了|更正|纠正|其实|改成|改为|现在是|不再|已经换|actually|correction|no longer|not anymore|changed to)",
    re.IGNORECASE,
)


@dataclass
class Decision:
    """Conflict-resolution action and the records it affects."""

    level: Level
    target_id: str | None
    reason: str


Adjudicator = Callable[[MemoryCandidate, MemoryRecord], Awaitable[str]]


def is_explicit_correction(utterance: str) -> bool:
    """Heuristically detect an explicit correction of an earlier statement."""
    return bool(_CORRECTION.search(utterance or ""))


def _same_value(left_value: str, right_value: str) -> bool:
    return normalize(left_value) == normalize(right_value)


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
    """Choose an action using same-slot active or disputed records and vector neighbors."""
    record_vectors = record_vectors or {}

    # L0 refreshes duplicates rather than multiplying matching slots or near-identical vectors.
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

    current = slot_matches[0]
    new_rank = SOURCE_RANK.get(candidate.source, 0)
    old_rank = SOURCE_RANK.get(current.source, 0)
    explicit = candidate.source == "user_explicit" or is_explicit_correction(utterance)

    # L3 explicit corrections outrank weaker or equally trusted sources.
    if explicit and new_rank >= old_rank:
        return Decision("L3", current.id, "user explicitly corrected this fact")

    # Inferred memory must never replace explicit user statements.
    if new_rank < old_rank:
        return Decision(
            "L4", current.id, f"lower-trust {candidate.source} contradicts {current.source}"
        )

    # L1 complementary values refine or extend existing knowledge.
    if _contains(candidate.value_text, current.value_text):
        return Decision("L1", current.id, "new value extends the existing one")

    # L2 equally trusted but clearly later evidence supersedes old values.
    if observed_at - current.valid_from > CONTRADICTION_WINDOW_SECONDS:
        return Decision("L2", current.id, "value changed over time")

    # Different values with equal trust and near-identical timing require arbitration.
    if adjudicator is not None:
        try:
            verdict = (await adjudicator(candidate, current)).strip().upper()
        except Exception:  # noqa: BLE001 - Fail safely into disputed status.
            verdict = ""
        if verdict in {"L1", "L2"}:
            return Decision(verdict, current.id, "resolved by adjudicator")  # type: ignore[arg-type]
        if verdict == "L0":
            return Decision("L0", current.id, "adjudicator judged equivalent")
    return Decision(
        "L4", current.id, "conflicting values with equal trust; needs user confirmation"
    )
