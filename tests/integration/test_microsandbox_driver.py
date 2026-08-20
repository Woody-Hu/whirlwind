"""MicrosandboxDriver tests (ADR-0006).

Capability reporting, CLI rendering and the caps-honesty refusals are pure
logic and run unconditionally (no VM needed — same level as the runsc
OCI-config rendering tests). Lifecycle tests boot a REAL libkrun microVM
when `msb` and a working backend are available: on Linux that means a
/dev/kvm that actually opens (a node without the host kvm module fails
open(2) with ENODEV — mknod cannot fix that, verified 2026-08-20; there is
no TCG fallback in libkrun); on macOS an Apple-Silicon host (HVF).

In a container without KVM passthrough the lifecycle tests skip with the
probe's verdict as the reason — an honest skip, never a fake backend.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from whirlwind.core import SnapshotKind
from whirlwind.core.platform import current_facts
from whirlwind.drivers import (
    Density,
    DriverError,
    ExecSpec,
    Isolation,
    Resources,
    SandboxNotFound,
    SandboxSpec,
    SnapshotArtifact,
    UnsupportedCapability,
)
from whirlwind.drivers.microsandbox import (
    MicrosandboxDriver,
    _guest_path,
    _render_exec_argv,
    _render_run_argv,
    kvm_available,
)

HAS_MSB = shutil.which("msb") is not None
# Linux needs a KVM backend that really opens; macOS needs Apple Silicon.
BACKEND_OK = current_facts().system != "linux" or kvm_available()

MSB_REQUIRED = pytest.mark.skipif(
    not (HAS_MSB and BACKEND_OK),
    reason=(
        "msb backend unavailable: "
        + (
            "msb binary not installed"
            if not HAS_MSB
            else "/dev/kvm does not open (host kvm module missing / no passthrough; "
            "libkrun has no TCG fallback)"
        )
    ),
)


def _payload() -> Path:
    """A real executable for the launcher to reference (never executed by
    the pure rendering tests — mirrors the runsc suite's payload choice)."""
    if shutil.which("busybox"):
        return Path(shutil.which("busybox") or "/usr/bin/busybox")
    return Path(sys.executable)


def _spec(tmp_path: Path, sandbox_id: str, argv: list[str] | None = None) -> SandboxSpec:
    """A real bundle: payload copied in as the guest launcher."""
    bundle = tmp_path / "bundle"
    payload = _payload()
    target = bundle / "bin" / payload.name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(payload, target)
    target.chmod(0o755)
    return SandboxSpec(
        sandbox_id=sandbox_id,
        argv=argv or [str(target), "sleep", "300"],
        bundle_root=bundle,
        workspace=tmp_path / "sandboxes" / sandbox_id,
        env={"PATH": "/bin:/usr/bin"},
    )


def _driver(tmp_path: Path) -> MicrosandboxDriver:
    return MicrosandboxDriver(snapshots_root=tmp_path / "snapshots")


def _register(driver: MicrosandboxDriver, spec: SandboxSpec) -> None:
    """Put an instance under driver management without booting anything —
    the caps-honesty refusals under test fire before any subprocess call."""
    from whirlwind.drivers import Instance

    driver._instances[spec.sandbox_id] = Instance(
        id=spec.sandbox_id, spec=spec, pid=None, process=None, started_at=0
    )


# ------------------------------------------------------ capability honesty


def test_capabilities_reported_truthfully() -> None:
    caps = MicrosandboxDriver().capabilities()
    assert caps.isolation == Isolation.LIGHT_VM
    assert caps.snapshot_full is False     # msb --resumable: unsupported in v0.6.x
    assert caps.snapshot_data is True
    assert caps.background_restore is False
    assert caps.net_policy is False        # msb networking exists; driver wires none
    assert caps.density == Density.MEDIUM  # one microVM (own kernel + RAM) per sandbox
    assert caps.delta_snapshots is False


def test_render_run_argv_maps_spec_to_msb_invocation(tmp_path: Path) -> None:
    spec = _spec(tmp_path, "sbx_1")
    spec.resources = Resources(mem_limit_mb=512, cpu_seconds=10, pids_max=64)
    spec.env = {"WHIRLWIND_SANDBOX_ID": "sbx_1"}

    argv = _render_run_argv(spec)

    # the image bundle directory is the msb image (becomes the VM root fs)
    assert argv[0] == "run"
    assert argv[1] == str(spec.bundle_root)
    # resource mapping: VM memory + in-guest POSIX rlimits
    assert "-m" in argv and argv[argv.index("-m") + 1] == "512M"
    assert argv.count("--rlimit") == 2
    rl = {argv[i + 1] for i, a in enumerate(argv) if a == "--rlimit"}
    assert rl == {"nproc=64", "cpu=10"}
    # workspace host-mounted at /workspace, command cwd pinned there
    mount = f"{spec.workspace.resolve()}:/workspace"
    assert "--mount-dir" in argv and argv[argv.index("--mount-dir") + 1] == mount
    assert argv[argv.index("-w") + 1] == "/workspace"
    # env whitelist is exact
    i = argv.index("-e")
    assert argv[i + 1] == "WHIRLWIND_SANDBOX_ID=sbx_1"
    # detached, non-interactive
    assert "--detach" in argv and "--no-tty" in argv
    # launcher translated from host-absolute to guest-absolute (bundle = root fs)
    sep = argv.index("--")
    payload_name = _payload().name
    assert argv[sep + 1] == f"/bin/{payload_name}"
    assert argv[sep + 2:] == ["sleep", "300"]
    assert "--name" in argv and argv[argv.index("--name") + 1] == "sbx_1"


def test_render_run_argv_requires_launcher_inside_bundle(tmp_path: Path) -> None:
    spec = _spec(tmp_path, "sbx_out")
    spec.argv = ["/usr/bin/python3", "-c", "pass"]  # outside the bundle root
    with pytest.raises(DriverError, match="outside the bundle root"):
        _render_run_argv(spec)


def test_render_run_argv_requires_launcher_argv(tmp_path: Path) -> None:
    spec = _spec(tmp_path, "sbx_noargv")
    spec.argv = []
    with pytest.raises(DriverError, match="launcher argv"):
        _render_run_argv(spec)


def test_guest_path_translates_and_rejects(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    (bundle / "venv" / "bin").mkdir(parents=True)
    assert _guest_path(bundle, str(bundle / "venv" / "bin" / "python")) == "/venv/bin/python"
    with pytest.raises(DriverError):
        _guest_path(bundle, "/usr/bin/env")


def test_render_exec_argv(tmp_path: Path) -> None:
    argv = _render_exec_argv(
        "sbx_1", ExecSpec(argv=["/bin/busybox", "echo", "hi"], env_extra={"K": "V"})
    )
    assert argv[0] == "exec"
    assert argv[1] == "sbx_1"
    assert "--no-tty" in argv
    assert argv[argv.index("-w") + 1] == "/workspace"
    assert argv[argv.index("-e") + 1] == "K=V"
    sep = argv.index("--")
    assert argv[sep + 1:] == ["/bin/busybox", "echo", "hi"]  # argv passes through unchanged


def test_kvm_probe_agrees_with_a_real_open() -> None:
    """The probe must equal a raw open(2) attempt — whatever the host's
    verdict is (ENODEV without the kvm module, EPERM without passthrough,
    success on a real host). No assumption about THIS environment."""
    try:
        fd = os.open("/dev/kvm", os.O_RDWR)
    except OSError:
        expected = False
    else:
        os.close(fd)
        expected = True
    assert kvm_available() is expected


async def test_missing_binary_reports_driver_error(tmp_path: Path) -> None:
    driver = MicrosandboxDriver(msb_bin="definitely-not-msb")
    with pytest.raises(DriverError, match="msb binary not found"):
        await driver.create(_spec(tmp_path, "sbx_missing"))


# ------------------------------------------------- caps-honesty refusals


async def test_full_snapshot_restore_refused_before_any_work(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    artifact = SnapshotArtifact(
        snapshot_id="snap_full", subject="sbx_x", kind=SnapshotKind.FULL,
        path=tmp_path / "snap", manifest={}, size=0, merkle="",
    )
    with pytest.raises(UnsupportedCapability, match="cannot restore"):
        await driver.create(_spec(tmp_path, "sbx_r"), from_snapshot=artifact)


async def test_delta_checkpoint_refused_before_any_work(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    base = SnapshotArtifact(
        snapshot_id="snap_base", subject="sbx_x", kind=SnapshotKind.DATA,
        path=tmp_path / "base", manifest={}, size=0, merkle="",
    )
    with pytest.raises(UnsupportedCapability, match="delta"):
        await driver.checkpoint("sbx_never_seen", SnapshotKind.DATA, base=base)


async def test_full_checkpoint_refused(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    spec = _spec(tmp_path, "sbx_ck")
    _register(driver, spec)
    with pytest.raises(UnsupportedCapability, match="cannot take"):
        await driver.checkpoint(spec.sandbox_id, SnapshotKind.FULL)


async def test_materialize_rejects_delta_artifacts(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    artifact = SnapshotArtifact(
        snapshot_id="snap_d", subject="sbx_x", kind=SnapshotKind.DATA,
        path=tmp_path / "d", manifest={}, size=0, merkle="", delta=True,
    )
    with pytest.raises(UnsupportedCapability, match="full DATA trees only"):
        await driver.materialize(artifact, tmp_path / "dest")


def test_unknown_sandbox_reports_not_found() -> None:
    with pytest.raises(SandboxNotFound):
        MicrosandboxDriver().instance("sbx_ghost")


# ------------------------------------------------- real microVM lifecycle


@MSB_REQUIRED
@pytest.mark.asyncio
async def test_lifecycle_end_to_end(tmp_path: Path) -> None:
    """A real libkrun microVM: create, exec, pause/resume, DATA checkpoint,
    destroy. Pause is a STOP/BOOT cycle (processes do not survive; workspace
    data does) — the honest semantics the module docstring declares."""
    driver = _driver(tmp_path)
    spec = _spec(tmp_path, "sbx_lifecycle")

    instance = await driver.create(spec)
    assert instance.pid is None  # the VM belongs to msb; no host pid to own

    # a real exec inside the microVM
    result = await driver.exec(
        spec.sandbox_id,
        ExecSpec(argv=["/bin/busybox", "echo", "hello-whirlwind"], timeout_s=60.0),
    )
    assert result.exit_code == 0
    assert "hello-whirlwind" in result.stdout

    # exec ran with cwd pinned to the virtio-fs workspace mount, and guest
    # writes are visible on the host (the DATA-checkpoint contract)
    result = await driver.exec(
        spec.sandbox_id,
        ExecSpec(
            argv=["/bin/busybox", "sh", "-c", "pwd > pwd.txt && echo marker > note.txt"],
            timeout_s=60.0,
        ),
    )
    assert result.exit_code == 0
    assert (spec.workspace / "pwd.txt").read_text().strip() == "/workspace"
    assert (spec.workspace / "note.txt").read_text().strip() == "marker"

    await driver.pause(spec.sandbox_id)
    await driver.resume(spec.sandbox_id)
    # workspace data survives the STOP/BOOT cycle
    assert (spec.workspace / "note.txt").read_text().strip() == "marker"

    # workspace-layer checkpoint: host-side copy with a merkle root
    artifact = await driver.checkpoint(spec.sandbox_id, SnapshotKind.DATA)
    assert artifact.kind == SnapshotKind.DATA
    assert artifact.path.is_dir()
    assert (artifact.path / "note.txt").read_text().strip() == "marker"
    assert artifact.merkle != ""
    assert artifact.manifest["backend"] == "microsandbox/libkrun"

    # materialize reconstructs the tree (seeding path of ADR-0012 D3)
    dest = tmp_path / "seeded"
    await driver.materialize(artifact, dest)
    assert (dest / "note.txt").read_text().strip() == "marker"

    await driver.destroy(spec.sandbox_id)
    with pytest.raises(SandboxNotFound):
        driver.instance(spec.sandbox_id)


@MSB_REQUIRED
@pytest.mark.asyncio
async def test_sandbox_boots_a_dedicated_guest_kernel(tmp_path: Path) -> None:
    """The isolation claim behind Isolation.LIGHT_VM: each sandbox runs its
    own guest kernel (libkrunfw), not the host kernel."""
    driver = _driver(tmp_path)
    spec = _spec(tmp_path, "sbx_kernel")

    await driver.create(spec)
    try:
        result = await driver.exec(
            spec.sandbox_id,
            ExecSpec(argv=["/bin/busybox", "uname", "-r"], timeout_s=60.0),
        )
        assert result.exit_code == 0
        # the guest kernel string is its own — never the host release (the
        # exact libkrunfw version string is not asserted: it tracks msb)
        assert result.stdout.strip() != os.uname().release
    finally:
        await driver.destroy(spec.sandbox_id)
