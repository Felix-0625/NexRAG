"""Tiered L0-L4 conflict handling and soft expiration of long-term memory."""

from __future__ import annotations

import asyncio

import pytest

from src.memory.models import MemoryCandidate, MemoryScope
from src.memory.service import MemoryService
from src.memory.vectors import InMemoryVectorIndex

DAY = 86_400.0
SCOPE = MemoryScope(user_id="alice", tenant_id="seller-a", chat_id="chat-1")


def candidate(
    value: str,
    *,
    attribute: str = "report currency",
    source: str = "user_implicit",
    scope: str = "tenant",
    kind: str = "fact",
) -> MemoryCandidate:
    """Build a memory candidate with explicit test defaults."""
    return MemoryCandidate(
        scope=scope,
        kind=kind,
        subject="user",
        attribute=attribute,
        value_text=value,
        confidence=0.7,
        source=source,
        importance=6,
    )


@pytest.fixture
def service(isolated_ops, embedder):
    """Create an isolated memory service for the scenario."""
    return MemoryService(isolated_ops, InMemoryVectorIndex(), embedder)


def write(service, cand, *, at, utterance="", scope=SCOPE):
    """Provide write test data or behavior for this scenario."""
    return asyncio.run(service.write(cand, scope, utterance=utterance, at=at))


def statuses(service, user="alice"):
    """Provide statuses test data or behavior for this scenario."""
    return {
        r.value_text: r.status for r in service.store.list_for_user(user, include_inactive=True)
    }


def test_l0_duplicate_is_noop(service):
    first = write(service, candidate("BRL"), at=1_000)
    second = write(service, candidate("brl"), at=2_000)
    assert first.decision.level == "ADD"
    assert second.decision.level == "L0"
    assert len(service.store.list_for_user("alice", include_inactive=True)) == 1


def test_l1_complementary_value_supersedes_softly(service):
    write(service, candidate("BRL"), at=1_000)
    result = write(service, candidate("BRL with two decimals"), at=1_500)
    assert result.decision.level == "L1"
    assert statuses(service) == {"BRL": "superseded", "BRL with two decimals": "active"}


def test_l2_temporal_update_sets_valid_to_and_keeps_history(service):
    write(service, candidate("BRL"), at=1_000)
    result = write(service, candidate("USD"), at=1_000 + 3 * DAY)
    assert result.decision.level == "L2"
    old = [
        r
        for r in service.store.list_for_user("alice", include_inactive=True)
        if r.value_text == "BRL"
    ][0]
    new = service.store.get(result.memory_id, "alice")
    assert old.status == "superseded" and old.valid_to == 1_000 + 3 * DAY
    assert new.supersedes_id == old.id and new.status == "active"


def test_l3_explicit_correction_wins_even_when_recent(service):
    write(service, candidate("BRL"), at=1_000)
    result = write(service, candidate("USD"), at=1_100, utterance="不对，报表币种其实是美元")
    assert result.decision.level == "L3"
    assert service.store.get(result.memory_id, "alice").source == "user_explicit"
    assert statuses(service)["BRL"] == "superseded"


def test_inferred_memory_never_overrides_explicit(service):
    write(service, candidate("BRL", source="user_explicit"), at=1_000)
    result = write(service, candidate("USD", source="agent_inferred"), at=1_000 + 10 * DAY)
    assert result.decision.level == "L4"
    assert set(statuses(service).values()) == {"disputed"}


def test_l4_close_contradiction_is_disputed_then_user_confirms(service):
    write(service, candidate("BRL"), at=1_000)
    result = write(service, candidate("EUR"), at=1_200)
    assert result.decision.level == "L4"
    assert statuses(service) == {"BRL": "disputed", "EUR": "disputed"}
    assert service.confirm("alice", result.memory_id)
    assert statuses(service) == {"BRL": "superseded", "EUR": "active"}
    assert service.store.get(result.memory_id, "alice").source == "user_explicit"


def test_adjudicator_failure_falls_back_to_disputed(isolated_ops, embedder):
    async def broken(_candidate, _record):
        """Provide broken test data or behavior for this scenario."""
        raise RuntimeError("model down")

    svc = MemoryService(isolated_ops, InMemoryVectorIndex(), embedder, adjudicator=broken)
    write(svc, candidate("BRL"), at=1_000)
    assert write(svc, candidate("EUR"), at=1_200).decision.level == "L4"


def test_forget_removes_whole_version_chain(service):
    write(service, candidate("BRL"), at=1_000)
    latest = write(service, candidate("USD"), at=1_000 + 3 * DAY)
    assert service.forget("alice", latest.memory_id) == 2
    assert service.store.list_for_user("alice", include_inactive=True) == []


@pytest.mark.parametrize("choose_new", [True, False])
def test_forget_confirmed_dispute_also_removes_rejected_history(service, choose_new):
    first = write(service, candidate("BRL"), at=1_000)
    second = write(service, candidate("EUR"), at=1_200)
    selected = second.memory_id if choose_new else first.memory_id
    assert service.confirm("alice", selected)
    assert service.forget("alice", selected) == 2
    assert service.store.list_for_user("alice", include_inactive=True) == []
