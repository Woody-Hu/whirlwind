"""RuntimeConfig backend selection (ADR-0004 D4).

Defaults stay memory (byte-for-byte backward compatible); every other
selection validates eagerly at the composition root with actionable
errors — never halfway through a boot.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from whirlwind.core.platform import current_facts
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


# -- microsandbox substrate composition (ADR-0006) -------------------------
# These exercise the composition-root branch logic only (same level as the
# sys.modules monkeypatches above): the probes are stubbed to force each
# branch. The REAL backend verdict comes from the gated integration suite
# (tests/integration/test_microsandbox_driver.py) — an honest skip where
# /dev/kvm does not open.


def _msb_probes(monkeypatch: pytest.MonkeyPatch, *, msb: bool, kvm: bool) -> None:
    import shutil as _shutil

    real_which = _shutil.which
    # msb=True must FAKE presence (a dummy path), not fall through to the real
    # probe: composition-root tests assert the branch logic, independent of
    # whether the binary happens to be installed on this host. Falling through
    # made the suite green/un-green depending on the machine.
    monkeypatch.setattr(
        "shutil.which",
        lambda name: (real_which(name) if name != "msb" else ("/opt/msb/bin/msb" if msb else None)),
    )
    monkeypatch.setattr("whirlwind.drivers.microsandbox.kvm_available", lambda: kvm)


def test_microsandbox_driver_requires_msb_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _msb_probes(monkeypatch, msb=False, kvm=True)
    with pytest.raises(ValueError, match="msb binary"):
        WhirlwindRuntime(RuntimeConfig(data_dir=tmp_path, driver="microsandbox"))


@pytest.mark.skipif(
    current_facts().system != "linux",
    reason="the /dev/kvm requirement is Linux-only (macOS backend is HVF)",
)
def test_microsandbox_driver_requires_kvm_on_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _msb_probes(monkeypatch, msb=True, kvm=False)
    with pytest.raises(ValueError, match="/dev/kvm"):
        WhirlwindRuntime(RuntimeConfig(data_dir=tmp_path, driver="microsandbox"))


def test_microsandbox_driver_constructs_when_backend_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from whirlwind.drivers import MicrosandboxDriver

    _msb_probes(monkeypatch, msb=True, kvm=True)
    runtime = WhirlwindRuntime(RuntimeConfig(data_dir=tmp_path, driver="microsandbox"))
    assert isinstance(runtime.driver, MicrosandboxDriver)


def test_microsandbox_rejects_delta_snapshot_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # caps-vs-mode cross-check lives at the composition root (ADR-0012 D6):
    # the substrate truthfully reports delta_snapshots=False
    _msb_probes(monkeypatch, msb=True, kvm=True)
    with pytest.raises(ValueError, match="delta"):
        WhirlwindRuntime(
            RuntimeConfig(data_dir=tmp_path, driver="microsandbox", snapshot_mode="delta")
        )
