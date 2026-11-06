"""对未被改动的应用默认值做窄范围、带版本的修正。

这是软件基线迁移，不是模型生成的 RSI 晋升。自定义指引和
staging 候选仍走各自明确的治理流程。
"""

from src.agent.prompts import DEFAULT_GUIDANCE
from src.midplatform.ops_store import dumps, new_id, now

CURRENT_KNOWLEDGE_RULE = (
    "- Use search_knowledge_documents for document-based questions. It searches only this\n"
    "  account's personal knowledge libraries and explicitly granted libraries. Business\n"
    "  tenant selection does not expand knowledge access."
)
LEGACY_KNOWLEDGE_RULE = (
    "- Use search_knowledge_documents for document-based questions. Its documents are shared;\n"
    "  tenant permissions apply only to the business database."
)


def upgrade_shared_knowledge_default(store, artifact) -> bool:
    """只替换完全未被改动的旧版种子，并保留其版本历史。"""
    legacy = DEFAULT_GUIDANCE.replace(CURRENT_KNOWLEDGE_RULE, LEGACY_KNOWLEDGE_RULE)
    if (
        artifact.kind != "prompt"
        or artifact.parent_id is not None
        or artifact.note != "built-in default"
        or artifact.content != legacy
        or legacy == DEFAULT_GUIDANCE
    ):
        return False
    with store.transaction() as connection:
        # 插入前先做 compare-and-swap，使并发的解析方（含不同 SQLite 连接）
        # 收敛到同一个替换版本。
        changed = connection.execute(
            "UPDATE artifact_versions SET label='archived' "
            "WHERE id=? AND label='production' AND created_by='system' "
            "AND note='built-in default' AND parent_id IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM artifact_versions WHERE kind='prompt' AND label='staging')",
            (artifact.id,),
        )
        if changed.rowcount != 1:
            return False
        connection.execute(
            "INSERT INTO artifact_versions VALUES (?,?,?,?,?,?,?,?)",
            (
                new_id(),
                "prompt",
                dumps(DEFAULT_GUIDANCE),
                artifact.id,
                "production",
                "built-in knowledge authorization migration v1",
                "system:builtin-migration",
                now(),
            ),
        )
    return True
