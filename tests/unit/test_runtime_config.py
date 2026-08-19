"""RuntimeConfig backend selection (ADR-0004 D4).

Defaults stay memory (byte-for-byte backward compatible); every other
selection validates eagerly at the composition root with actionable
errors — never halfway through a boot.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from whirlwind.runtime import RuntimeConfig, WhirlwindRuntime
from whirlwind.storage.memory import MemoryKVStore, MemoryMetadataStore


def test_default_backends_are_memory(tmp_path: Path) -> None:
    runtime = WhirlwindRuntime(RuntimeConfig(data_dir=tmp_path))
    assert isinstance(runtime.store, MemoryMetadataStore)
    assert isinstance(runtime.kv, MemoryKVStore)


def test_unknown_backend_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown metadata_backend"):
        WhirlwindRuntime(RuntimeConfig(data_dir=tmp_path, metadata_backend="sqlite"))
    with pytest.raises(ValueError, match="unknown kv_backend"):
        WhirlwindRuntime(RuntimeConfig(data_dir=tmp_path, kv_backend="etcd"))


def test_backend_requires_its_address(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="postgres_dsn"):
        WhirlwindRuntime(RuntimeConfig(data_dir=tmp_path, metadata_backend="postgres"))
    with pytest.raises(ValueError, match="redis_url"):
        WhirlwindRuntime(RuntimeConfig(data_dir=tmp_path, kv_backend="redis"))


def test_backend_without_driver_fails_actionably(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # None in sys.modules makes `from whirlwind.storage.X import ...` raise ImportError
    monkeypatch.setitem(sys.modules, "whirlwind.storage.postgres", None)
    with pytest.raises(ValueError, match=r"pip install whirlwind\[postgres\]"):
        WhirlwindRuntime(
            RuntimeConfig(data_dir=tmp_path, metadata_backend="postgres", postgres_dsn="postgresql://x")
        )
    monkeypatch.setitem(sys.modules, "whirlwind.storage.redis", None)
    with pytest.raises(ValueError, match=r"pip install whirlwind\[redis\]"):
        WhirlwindRuntime(RuntimeConfig(data_dir=tmp_path, kv_backend="redis", redis_url="redis://x"))
