"""RunscDriver tests.

Capability reporting and OCI bundle rendering are exercised unconditionally
(no runsc binary needed). Lifecycle tests (create/exec/pause/checkpoint/
destroy against a real gVisor sandbox) require `runsc` on PATH and are skipped
otherwise — runsc is Linux-only and not installed in every dev box.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from argus.core import SnapshotKind
from argus.drivers import (
    Density,
    DriverError,
    ExecSpec,
    Isolation,
    RunscDriver,
    SandboxSpec,
)

HAS_RUNSC = shutil.which("runsc") is not None

RUNSC_REQUIRED = pytest.mark.skipif(not HAS_RUNSC, reason="runsc binary not installed")


def _spec(tmp_path: Path, sandbox_id: str, argv: list[str] | None = None) -> SandboxSpec:
    bundle = tmp_path / "bundle"
    venv = bundle / "venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").write_text("#!/bin/sh\n")
    return SandboxSpec(
        sandbox_id=sandbox_id,
        argv=argv or [str(venv / "python"), "-m", "argus.harness.echo_server"],
        bundle_root=bundle,
        workspace=tmp_path / "sandboxes" / sandbox_id,
        env={"PYTHONUNBUFFERED": "1"},
    )


def _driver(tmp_path: Path) -> RunscDriver:
    return RunscDriver(
        state_root=tmp_path / "runsc-state",
        work_root=tmp_path / "runsc-bundles",
        snapshots_root=tmp_path / "snapshots",
    )


def test_capabilities_reported_truthfully(tmp_path: Path) -> None:
    caps = _driver(tmp_path).capabilities()
    assert caps.isolation == Isolation.LIGHT_VM
    assert caps.snapshot_full is True
    assert caps.snapshot_data is True
    assert caps.background_restore is True
    assert caps.net_policy is True
    assert caps.density == Density.HIGH


def test_render_oci_config_maps_paths_into_rootfs(tmp_path: Path) -> None:
    from argus.drivers.runsc import _render_oci_config

    spec = _spec(tmp_path, "sbx_1")
    config = _render_oci_config(spec.bundle_root, spec)

    assert config["ociVersion"] == "1.0.2"
    # argv translated from host-absolute to rootfs-relative
    assert config["process"]["args"] == ["/venv/bin/python", "-m", "argus.harness.echo_server"]
    # workspace bind-mounted at the process cwd
    assert config["process"]["cwd"] == "/workspace"
    bind_sources = {m["source"] for m in config["mounts"] if m.get("type") == "bind"}
    assert str(spec.workspace.resolve()) in bind_sources
    assert str(spec.bundle_root.resolve()) in bind_sources
    # env is exactly the spec whitelist
    assert config["process"]["env"] == ["PYTHONUNBUFFERED=1"]


def test_render_oci_config_rejects_launcher_outside_bundle(tmp_path: Path) -> None:
    from argus.drivers.runsc import _in_rootfs

    bundle = tmp_path / "bundle"
    with pytest.raises(DriverError):
        _in_rootfs(bundle, "/usr/bin/python")  # outside the bundle root


@RUNSC_REQUIRED
@pytest.mark.asyncio
async def test_lifecycle_end_to_end(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    spec = _spec(tmp_path, "sbx_lifecycle")

    instance = await driver.create(spec)
    assert instance.pid is not None

    # a real exec inside the sandbox
    result = await driver.exec(spec.sandbox_id, ExecSpec(argv=["echo", "hello-argus"], timeout_s=15.0))
    assert result.exit_code == 0
    assert "hello-argus" in result.stdout

    await driver.pause(spec.sandbox_id)
    await driver.resume(spec.sandbox_id)

    artifact = await driver.checkpoint(spec.sandbox_id, SnapshotKind.DATA)
    assert artifact.kind == SnapshotKind.DATA
    assert artifact.path.is_dir()

    await driver.destroy(spec.sandbox_id)


@RUNSC_REQUIRED
@pytest.mark.asyncio
async def test_missing_binary_reports_driver_error(tmp_path: Path) -> None:
    from argus.drivers import SandboxSpec as S

    driver = RunscDriver(runsc_bin="definitely-not-runsc")
    spec = _spec(tmp_path, "sbx_missing")
    with pytest.raises(DriverError):
        await driver.create(S(sandbox_id=spec.sandbox_id, argv=spec.argv, bundle_root=spec.bundle_root, workspace=spec.workspace, env=spec.env))
