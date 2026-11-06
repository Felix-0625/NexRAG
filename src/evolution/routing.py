"""解析某次请求应使用哪个产物版本（production 或 canary）。

存在 staging 版本时，按用户 ID 哈希选出一部分稳定的用户使用它，
使同一用户在观察窗口内始终看到同一版本；其余用户使用 production。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from src.config import settings
from src.evolution.artifacts import ArtifactRegistry, registry


@dataclass
class ActiveArtifacts:
    prompt: str
    params: dict[str, Any]
    versions: dict[str, str] = field(default_factory=dict)
    canary: bool = False


def in_canary(user_id: str, ratio: float | None = None) -> bool:
    """按用户 ID 的哈希稳定地判断是否落入灰度人群；ratio 缺省取 CANARY_RATIO，同一用户结果恒定。"""
    ratio = settings.CANARY_RATIO if ratio is None else ratio
    bucket = int(hashlib.sha256(f"canary:{user_id}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return bucket < ratio


def resolve(user_id: str, reg: ArtifactRegistry | None = None) -> ActiveArtifacts:
    """为该用户选出本次使用的 prompt 与参数版本：灰度用户且存在 staging 版本时用 staging，否则用 production。"""
    reg = reg or registry
    canary_user = in_canary(user_id)
    chosen: dict[str, Any] = {}
    versions: dict[str, str] = {}
    used_canary = False
    for kind in ("prompt", "params"):
        artifact = reg.production(kind)
        staging = reg.by_label(kind, "staging")
        if canary_user and staging is not None:
            artifact = staging
            used_canary = True
        chosen[kind] = artifact.content
        versions[kind] = artifact.id
    return ActiveArtifacts(chosen["prompt"], dict(chosen["params"]), versions, used_canary)
