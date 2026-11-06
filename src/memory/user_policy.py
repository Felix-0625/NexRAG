"""核心记忆组件的持久化个人退出（opt-out）设置。

上游 WebUI 的记忆偏好属于另一套记忆引擎。本策略以已验证的用户身份为键，
跨请求共享。关闭召回/提取后，已有记录仍保留，可供查看和遗忘。
"""

import json

from src.midplatform.ops_store import get_ops_store


def _key(user_id: str) -> str:
    if not isinstance(user_id, str) or not user_id or len(user_id) > 128:
        raise ValueError("Invalid verified memory identity")
    return "memory.user-settings:" + user_id


def memory_enabled(user_id: str, *, store=None) -> bool:
    """该用户是否允许记忆召回与提取；从未设置过时默认开启，身份标识无效抛出 ValueError。"""
    row = (store or get_ops_store()).query_one(
        "SELECT value FROM workspace_settings WHERE key=?", (_key(user_id),)
    )
    # 保持现有产品默认值；用户明确的个人退出优先于部署级 ENABLE_MEMORY 开关，
    # 且重启后依然有效。
    return json.loads(row["value"]).get("enabled") is True if row else True


def set_memory_enabled(user_id: str, enabled: bool, *, store=None) -> None:
    """保存该用户的记忆开关；enabled 必须是布尔值，关闭后已有记忆仍保留，可查看和遗忘。"""
    if type(enabled) is not bool:
        raise ValueError("Memory preference must be a boolean")
    (store or get_ops_store()).execute(
        "INSERT OR REPLACE INTO workspace_settings VALUES (?,?)",
        (_key(user_id), json.dumps({"enabled": enabled})),
    )
