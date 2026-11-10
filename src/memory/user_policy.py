"""Persist personal opt-out for the core memory engine.

Upstream WebUI preferences control another engine. Key this policy by verified identity;
disabling recall and extraction preserves records for viewing and forgetting.
"""

import json

from src.midplatform.ops_store import get_ops_store


def _key(user_id: str) -> str:
    if not isinstance(user_id, str) or not user_id or len(user_id) > 128:
        raise ValueError("Invalid verified memory identity")
    return "memory.user-settings:" + user_id


def memory_enabled(user_id: str, *, store=None) -> bool:
    """Return personal memory permission, enabled by default; reject invalid identities."""
    row = (store or get_ops_store()).query_one(
        "SELECT value FROM workspace_settings WHERE key=?", (_key(user_id),)
    )
    # Keep existing defaults, but let personal opt-out override ENABLE_MEMORY
    # and persist across restarts.
    return json.loads(row["value"]).get("enabled") is True if row else True


def set_memory_enabled(user_id: str, enabled: bool, *, store=None) -> None:
    """Persist a Boolean preference without deleting existing memories."""
    if type(enabled) is not bool:
        raise ValueError("Memory preference must be a boolean")
    (store or get_ops_store()).execute(
        "INSERT OR REPLACE INTO workspace_settings VALUES (?,?)",
        (_key(user_id), json.dumps({"enabled": enabled})),
    )
