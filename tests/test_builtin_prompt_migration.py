"""修正旧的共享知识库种子，同时不覆盖运维人员或 RSI 产生的内容。"""

import pytest

from src.agent.prompts import DEFAULT_GUIDANCE
from src.evolution.artifacts import ArtifactRegistry
from src.evolution.builtin_migrations import (
    CURRENT_KNOWLEDGE_RULE,
    LEGACY_KNOWLEDGE_RULE,
    upgrade_shared_knowledge_default,
)


def seed(reg, **kwargs):
    legacy = DEFAULT_GUIDANCE.replace(CURRENT_KNOWLEDGE_RULE, LEGACY_KNOWLEDGE_RULE)
    return reg.create("prompt", legacy, label="production", note="built-in default", **kwargs)


def test_legacy_seed_gets_one_auditable_successor(isolated_ops):
    reg = ArtifactRegistry(isolated_ops)
    old = seed(reg)
    candidate = reg.create("prompt", "RSI pending", label="candidate", parent_id=old.id)
    current = reg.production("prompt")
    assert current.content == DEFAULT_GUIDANCE and current.parent_id == old.id
    assert current.note == "built-in knowledge authorization migration v1"
    assert reg.get(old.id).label == "archived"
    assert reg.get(candidate.id).label == "candidate"
    assert reg.production("prompt").id == current.id
    assert not upgrade_shared_knowledge_default(isolated_ops, old)
    assert len(reg.history("prompt")) == 3


def test_active_canary_defers_baseline_migration(isolated_ops):
    reg = ArtifactRegistry(isolated_ops)
    old = seed(reg)
    canary = reg.create("prompt", "RSI staging", label="staging", parent_id=old.id)
    assert reg.production("prompt").id == old.id
    assert reg.get(canary.id).label == "staging"
    assert len(reg.history("prompt")) == 2


@pytest.mark.parametrize("change", ["operator", "custom_content", "custom_note", "descendant"])
def test_custom_guidance_is_never_silently_replaced(isolated_ops, change):
    reg = ArtifactRegistry(isolated_ops)
    old = seed(reg, created_by="operator" if change == "operator" else "system")
    if change == "custom_content":
        old = reg.create(
            "prompt",
            old.content + "\nOperator customization",
            label="candidate",
            note="built-in default",
        )
        reg.set_label(old.id, "production")
    elif change == "custom_note":
        isolated_ops.execute(
            "UPDATE artifact_versions SET note='operator release' WHERE id=?", (old.id,)
        )
    elif change == "descendant":
        old = reg.create("prompt", old.content, parent_id=old.id, note="built-in default")
        reg.set_label(old.id, "production")
    assert reg.production("prompt").id == old.id
