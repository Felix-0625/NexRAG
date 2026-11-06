"""共享测试隔离：内存运维库与确定性的记忆后端。"""

from __future__ import annotations

import hashlib

import pytest

from src.config import settings
from src.memory.runtime import set_memory_service
from src.midplatform.ops_store import OpsStore, set_ops_store


class HashEmbedder:
    """确定性的词袋嵌入器，使相似度可在离线环境下测试。"""

    dim = 64

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vector = [0.0] * self.dim
            for token in text.lower().replace(":", " ").split():
                bucket = int(hashlib.md5(token.encode()).hexdigest(), 16) % self.dim
                vector[bucket] += 1.0
            vectors.append(vector)
        return vectors


@pytest.fixture(autouse=True)
def isolated_ops(tmp_path, monkeypatch):
    """每个测试都使用全新的内存运维库，且不连接任何真实记忆服务。"""
    store = OpsStore(":memory:")
    set_ops_store(store)
    set_memory_service(None)
    monkeypatch.setattr(settings, "ENABLE_MEMORY", False)
    # 未自行准备 tenant-access.yaml 的测试，不得用本仓库真实的
    # examples/olist/tenant-access.yaml 去初始化全新的运维库。
    monkeypatch.setattr(settings, "TENANT_ACCESS_PATH", str(tmp_path / "no-tenant-access.yaml"))
    yield store
    set_ops_store(None)
    set_memory_service(None)
    store.close()


@pytest.fixture
def embedder() -> HashEmbedder:
    return HashEmbedder()
