"""Shared test isolation with in-memory operations storage and deterministic memory vectors."""

from __future__ import annotations

import hashlib

import pytest

from src.config import settings
from src.memory.runtime import set_memory_service
from src.midplatform.ops_store import OpsStore, set_ops_store


class HashEmbedder:
    """Deterministic bag-of-words embedder for offline similarity tests."""

    dim = 64

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Provide embed test data or behavior for this scenario."""
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
    """Give every test fresh operations storage without connecting real memory services."""
    store = OpsStore(":memory:")
    set_ops_store(store)
    set_memory_service(None)
    monkeypatch.setattr(settings, "ENABLE_MEMORY", False)
    # Tests without their own tenant-access YAML must not initialize fresh storage
    # from the repository's real Olist tenant grants.
    monkeypatch.setattr(settings, "TENANT_ACCESS_PATH", str(tmp_path / "no-tenant-access.yaml"))
    yield store
    set_ops_store(None)
    set_memory_service(None)
    store.close()


@pytest.fixture
def embedder() -> HashEmbedder:
    """Provide deterministic offline embeddings."""
    return HashEmbedder()
