"""RunscDriver tests.

Capability reporting and OCI bundle rendering are exercised unconditionally
(no runsc binary needed). Lifecycle tests (create/exec/pause/checkpoint/
destroy) run a REAL gVisor sandbox — a static busybox as the rootfs payload —
when `runsc` and `busybox` are available.

The driver harness adapts itself to the environment the way a deployment
would: inside a restricted container (no CAP_SYS_ADMIN, read-only cgroups)
runsc must run rootless with --ignore-cgroups and cannot use per-sandbox
netstack networking; on a full-capability host it gets the defaults.
"""

from __future__ import annotations

import os
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
HAS_BUSYBOX = shutil.which("busybox") is not None

RUNSC_REQUIRED = pytest.mark.skipif(
    not (HAS_RUNSC and HAS_BUSYBOX), reason="runsc binary or static busybox not installed"
)

CAP_SYS_ADMIN = 21


def _have_cap(cap_bit: int) -> bool:
    try:
        status = Path("/proc/self/status").read_text()
        cap_eff = int(
            next(l.split()[1] for l in status.splitlines() if l.startswith("CapEff")), 16
        )
        return bool(cap_eff & (1 << cap_bit))
    except (StopIteration, ValueError, OSError):
        return False


# restricted container: rootless containerd/docker drops CAP_SYS_ADMIN, so
# runsc must run rootless, skip cgroups, and use non-sandbox networking.
IS_RESTRICTED = os.geteuid() != 0 or not _have_cap(CAP_SYS_ADMIN)


def _busybox() -> Path:
    return Path(shutil.which("busybox") or "/usr/bin/busybox")


def _spec(tmp_path: Path, sandbox_id: str, argv: list[str] | None = None) -> SandboxSpec:
    """A real bundle: static busybox copied in as the sandbox payload."""
    bundle = tmp_path / "bundle"
    bb = bundle / "bin" / "busybox"
    bb.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_busybox(), bb)
    bb.chmod(0o755)
    return SandboxSpec(
        sandbox_id=sandbox_id,
        argv=argv or [str(bb), "sh", "-c", "sleep 300"],
        bundle_root=bundle,
        workspace=tmp_path / "sandboxes" / sandbox_id,
        env={"PATH": "/bin"},
    )


def _driver(tmp_path: Path) -> RunscDriver:
    return RunscDriver(
        state_root=tmp_path / "runsc-state",
        work_root=tmp_path / "runsc-bundles",
        snapshots_root=tmp_path / "snapshots",
        net="none" if IS_RESTRICTED else "sandbox",
        platform="systrap",
        rootless=IS_RESTRICTED,
        ignore_cgroups=IS_RESTRICTED,
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
    assert config["process"]["args"] == ["/bin/busybox", "sh", "-c", "sleep 300"]
    # workspace bind-mounted at the process cwd
    assert config["process"]["cwd"] == "/workspace"
    bind_sources = {m["source"] for m in config["mounts"] if m.get("type") == "bind"}
    assert str(spec.workspace.resolve()) in bind_sources
    assert str(spec.bundle_root.resolve()) in bind_sources
    # env is exactly the spec whitelist
    assert config["process"]["env"] == ["PATH=/bin"]


def test_render_oci_config_rejects_launcher_outside_bundle(tmp_path: Path) -> None:
    from argus.drivers.runsc import _in_rootfs

    bundle = tmp_path / "bundle"
    with pytest.raises(DriverError):
        _in_rootfs(bundle, "/usr/bin/python")  # outside the bundle root


async def test_missing_binary_reports_driver_error(tmp_path: Path) -> None:
    driver = RunscDriver(runsc_bin="definitely-not-runsc")
    spec = _spec(tmp_path, "sbx_missing")
    with pytest.raises(DriverError):
        await driver.create(spec)


@RUNSC_REQUIRED
@pytest.mark.asyncio
async def test_lifecycle_end_to_end(tmp_path: Path) -> None:
    """A real gVisor sandbox: create, exec, pause, resume, checkpoint, destroy."""
    driver = _driver(tmp_path)
    spec = _spec(tmp_path, "sbx_lifecycle")

    instance = await driver.create(spec)
    assert instance.pid is not None

    # a real exec inside the sandbox (guest kernel = runsc Sentry)
    result = await driver.exec(
        spec.sandbox_id,
        ExecSpec(argv=["/bin/busybox", "echo", "hello-argus"], timeout_s=30.0),
    )
    assert result.exit_code == 0
    assert "hello-argus" in result.stdout

    # the exec ran with cwd pinned to the workspace bind mount
    result = await driver.exec(
        spec.sandbox_id,
        ExecSpec(argv=["/bin/busybox", "sh", "-c", "pwd > pwd.txt && echo marker > note.txt"], timeout_s=30.0),
    )
    assert result.exit_code == 0
    assert (spec.workspace / "pwd.txt").read_text().strip() == "/workspace"
    assert (spec.workspace / "note.txt").read_text().strip() == "marker"

    await driver.pause(spec.sandbox_id)
    await driver.resume(spec.sandbox_id)

    # workspace-layer checkpoint: the sandbox data survives as a directory copy
    artifact = await driver.checkpoint(spec.sandbox_id, SnapshotKind.DATA)
    assert artifact.kind == SnapshotKind.DATA
    assert artifact.path.is_dir()
    assert (artifact.path / "note.txt").read_text().strip() == "marker"
    assert artifact.merkle != ""

    await driver.destroy(spec.sandbox_id)
    # deleted: further lifecycle ops must say the sandbox is gone
    from argus.drivers import SandboxNotFound

    with pytest.raises(SandboxNotFound):
        driver.instance(spec.sandbox_id)


@RUNSC_REQUIRED
@pytest.mark.asyncio
async def test_full_snapshot_checkpoint_roundtrip(tmp_path: Path) -> None:
    """snapshot_full capability is real: runsc checkpoint (embedded CRIU)
    dumps memory + rootfs state into the artifact directory."""
    driver = _driver(tmp_path)
    spec = _spec(tmp_path, "sbx_ckpt")
    await driver.create(spec)
    try:
        await driver.exec(
            spec.sandbox_id,
            ExecSpec(argv=["/bin/busybox", "sh", "-c", "echo state=42 > ws.txt"], timeout_s=30.0),
        )
        artifact = await driver.checkpoint(spec.sandbox_id, SnapshotKind.FULL)
        assert artifact.kind == SnapshotKind.FULL
        assert artifact.path.is_dir()
        assert artifact.size > 0
        assert artifact.manifest["backend"] == "runsc/CRIU"
        # CRIU image files are really there
        assert (artifact.path / "checkpoint.img").exists()
        assert (artifact.path / "pages.img").exists()
    finally:
        await driver.destroy(spec.sandbox_id)


@RUNSC_REQUIRED
@pytest.mark.skipif(IS_RESTRICTED, reason="runsc restore is unsupported in rootless mode")
@pytest.mark.asyncio
async def test_full_snapshot_restore(tmp_path: Path) -> None:
    """checkpoint -> destroy -> restore boots the checkpointed kernel state
    (memory + rootfs) into a fresh sandbox id."""
    driver = _driver(tmp_path)
    spec = _spec(tmp_path, "sbx_src")
    await driver.create(spec)
    await driver.exec(
        spec.sandbox_id,
        ExecSpec(argv=["/bin/busybox", "sh", "-c", "echo state=42 > ws.txt"], timeout_s=30.0),
    )
    artifact = await driver.checkpoint(spec.sandbox_id, SnapshotKind.FULL)
    await driver.destroy(spec.sandbox_id)

    spec2 = _spec(tmp_path, "sbx_restored")
    instance = await driver.create(spec2, from_snapshot=artifact)
    try:
        assert instance.pid is not None
        result = await driver.exec(
            spec2.sandbox_id,
            ExecSpec(argv=["/bin/busybox", "echo", "alive"], timeout_s=30.0),
        )
        assert result.exit_code == 0
        assert "alive" in result.stdout
    finally:
        await driver.destroy(spec2.sandbox_id)


@RUNSC_REQUIRED
@pytest.mark.asyncio
async def test_sandbox_isolates_kernel_from_host(tmp_path: Path) -> None:
    """The guest kernel is the runsc Sentry, not the host kernel — the
    isolation claim behind Isolation.LIGHT_VM, verified from inside."""
    driver = _driver(tmp_path)
    spec = _spec(tmp_path, "sbx_isolation")

    await driver.create(spec)
    try:
        result = await driver.exec(
            spec.sandbox_id,
            ExecSpec(argv=["/bin/busybox", "uname", "-r"], timeout_s=30.0),
        )
        assert result.exit_code == 0
        assert "gvisor" in result.stdout  # Sentry reports its own kernel string
        assert result.stdout.strip() != os.uname().release
    finally:
        await driver.destroy(spec.sandbox_id)
