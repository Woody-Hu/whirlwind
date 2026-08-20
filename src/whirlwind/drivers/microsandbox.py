"""MicrosandboxDriver: libkrun microVM substrate (ADR-0006).

microsandbox (`msb`) boots each sandbox as a real microVM with a dedicated
guest kernel (libkrun: KVM on Linux, HVF on Apple Silicon) — a genuine VM
isolation boundary reported as `Isolation.LIGHT_VM`. The driver is a thin
CLI orchestration layer over `msb` (architecture 8.2), mirroring the runsc
driver's integration level:

- create:  the image bundle directory becomes the VM root filesystem (msb
  clones it into a private root disk), the workspace is host-mounted at
  `/workspace` (virtio-fs) and is the command cwd; `msb run --detach` owns
  the launcher argv as the sandbox's main command.
- exec / pause / resume / destroy: `msb exec` / `msb stop` / `msb start` /
  `msb remove --force`. pause is honestly a STOP/BOOT cycle, not a memory
  freeze (processes do not survive; workspace data does) — the memory-freeze
  class of behavior is what `snapshot_full=False` already declares absent.
- checkpoint: DATA snapshots are host-side workspace copies with a merkle
  root (the exact contract of the process driver — the workspace is a host
  directory by construction, so no VM interaction is involved).

Capability honesty (probed against the real `msb` 0.6.12 CLI, 2026-08-20):
- `snapshot_full=False`: `msb snapshot create --resumable` — the only
  memory-state surface — returns an explicit unsupported-feature error in
  v0.6.x ("reserved by the public contract"). Flipping this bit requires a
  passing restore test on a real backend, per the every-driver rule.
- `snapshot_data=True` with a caveat: the copy/merkle mechanism is
  driver-agnostic and unit-tested, but guest→host write coherence through
  the virtio-fs workspace mount is only provable where the VM actually runs
  (integration suite gates on a real KVM/HVF backend).
- `net_policy=False`: msb ships programmable networking, but this driver
  wires none of it — nothing is claimed that is not enforced.
- `density=MEDIUM`: one microVM (own kernel + memory) per sandbox, unlike
  the shared-kernel process/runsc substrates.

Linux runtime prerequisite (not installable): a real `/dev/kvm` backed by
a loaded host kvm module. A node without the module fails open(2) with
ENODEV — mknod cannot fix that (verified 2026-08-20); there is no TCG/QEMU
fallback in microsandbox. Integration tests gate on the backend and skip
honestly where it is absent.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from whirlwind.core import SnapshotKind, new_snapshot_id

from .base import (
    Caps,
    Density,
    DriverError,
    ExecResult,
    ExecSpec,
    Instance,
    Isolation,
    SandboxDriver,
    SandboxNotFound,
    SandboxSpec,
    SnapshotArtifact,
    UnsupportedCapability,
)

_MICROSANDBOX_CAPS = Caps(
    isolation=Isolation.LIGHT_VM,   # dedicated guest kernel behind a real VM boundary
    snapshot_full=False,            # msb --resumable: unsupported-feature in v0.6.x
    snapshot_data=True,             # workspace-layer checkpoint (host-side copy)
    background_restore=False,       # no kernel-first restore mode in the CLI surface
    net_policy=False,               # msb networking exists; this driver enforces none
    density=Density.MEDIUM,         # one microVM per sandbox (own kernel + memory)
    delta_snapshots=False,          # no diff checkpoints on this substrate
)

# The workspace is host-mounted at this guest path (command cwd, the only
# coordinated writable area — same contract as the runsc bind mount).
_WORKSPACE_MOUNT = "/workspace"


def kvm_available() -> bool:
    """Honest Linux backend probe: /dev/kvm must exist AND open. A node
    without a loaded host kvm module fails open(2) with ENODEV (mknod cannot
    fix that — verified 2026-08-20); a container without device passthrough
    gets ENOENT/EPERM. Not overrideable by WHIRLWIND_PLATFORM: probes stay
    real (ADR-0007 honest-edge rule)."""
    try:
        fd = os.open("/dev/kvm", os.O_RDWR)
    except OSError:
        return False
    os.close(fd)
    return True


def _guest_path(bundle_root: Path, host_path: str) -> str:
    """Translate a host-absolute path inside bundle_root to a guest-absolute
    one: the image bundle becomes the VM root filesystem, so
    `/bundle/venv/bin/python` is `/venv/bin/python` inside the guest."""
    path = Path(host_path)
    try:
        rel = path.resolve().relative_to(bundle_root.resolve())
    except ValueError:
        raise DriverError(f"launcher {host_path!r} is outside the bundle root {bundle_root}") from None
    return "/" + rel.as_posix()


def _render_run_argv(spec: SandboxSpec) -> list[str]:
    """Pure rendering of the `msb run` invocation for a fully-resolved spec
    (unit-tested without any VM):

        msb run <bundle_root> --name <id> [-m <N>M] [--rlimit nproc=N]
                [--rlimit cpu=N] [-e K=V]... --mount-dir <ws>:/workspace
                -w /workspace --no-tty --detach -- <argv...>

    Resource mapping: mem_limit_mb is the VM memory allocation (`-m`, a
    genuine physical cap — stronger than the process driver's RLIMIT_AS);
    pids_max / cpu_seconds map to in-guest POSIX rlimits. Caveat: the
    rlimit surface is accepted by the CLI but only validated where the
    backend actually runs (gated integration suite).
    """
    if not spec.argv:
        raise DriverError("sandbox spec requires a launcher argv")
    argv: list[str] = [
        "run", str(spec.bundle_root),
        "--name", spec.sandbox_id,
        "--no-tty", "--detach",
        "--mount-dir", f"{spec.workspace.resolve()}:{_WORKSPACE_MOUNT}",
        "-w", _WORKSPACE_MOUNT,
    ]
    res = spec.resources
    if res.mem_limit_mb is not None:
        argv += ["-m", f"{res.mem_limit_mb}M"]
    if res.pids_max is not None:
        argv += ["--rlimit", f"nproc={res.pids_max}"]
    if res.cpu_seconds is not None:
        argv += ["--rlimit", f"cpu={res.cpu_seconds}"]
    for key, value in spec.env.items():
        argv += ["-e", f"{key}={value}"]
    argv += ["--", _guest_path(spec.bundle_root, spec.argv[0]), *spec.argv[1:]]
    return argv


def _render_exec_argv(sandbox_id: str, spec: ExecSpec) -> list[str]:
    """Pure rendering of `msb exec` (argv pass through unchanged, cwd pinned
    to the workspace mount — same semantics as the runsc exec mapping)."""
    argv = ["exec", sandbox_id, "--no-tty", "-w", _WORKSPACE_MOUNT]
    for key, value in spec.env_extra.items():
        argv += ["-e", f"{key}={value}"]
    return argv + ["--", *spec.argv]


def _merkle_root(root: Path) -> tuple[str, int, int]:
    """Content-addressed digest of a directory tree (shared contract with the
    other drivers: full and delta artifacts of one tree state share one root)."""

    def file_hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()

    entries: list[str] = []
    total_bytes = 0
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            entries.append(f"{rel}:link:{os.readlink(path)}")
        elif path.is_file():
            total_bytes += path.stat().st_size
            entries.append(f"{rel}:sha256:{file_hash(path)}")
    root_hash = hashlib.sha256("\n".join(entries).encode()).hexdigest()
    return root_hash, len(entries), total_bytes


def _copy_tree(src: Path, dest: Path) -> None:
    for item in src.iterdir():
        target = dest / item.name
        if item.is_dir():
            shutil.copytree(item, target, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target, follow_symlinks=False)


class MicrosandboxDriver(SandboxDriver):
    """Sandbox = one libkrun microVM + one host-mounted private workspace."""

    name = "microsandbox"

    def __init__(
        self,
        *,
        msb_bin: str = "msb",
        snapshots_root: Path | None = None,
    ) -> None:
        self._msb_bin = msb_bin
        self._snapshots_root = snapshots_root
        self._instances: dict[str, Instance] = {}

    # ------------------------------------------------------------ helpers

    async def _run(self, *args: str, timeout_s: float = 60.0) -> tuple[int, str, str]:
        """One msb subprocess; surfaces failures as DriverError. Output goes
        to temp files (detached sandboxes keep the fds alive past the parent
        CLI exit — a pipe read would block on their EOF forever)."""
        with (
            tempfile.TemporaryFile() as out_f,
            tempfile.TemporaryFile() as err_f,
        ):
            try:
                proc = await asyncio.create_subprocess_exec(
                    self._msb_bin, *args,
                    stdout=out_f,
                    stderr=err_f,
                )
            except FileNotFoundError:
                raise DriverError(
                    f"msb binary not found ({self._msb_bin!r}); install "
                    "microsandbox (scripts/setup/install-microsandbox.sh) "
                    "or pick another driver",
                    detail={"required": "msb", "hint": "github.com/superradcompany/microsandbox"},
                ) from None
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout_s)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                raise DriverError(
                    f"msb {args[0] if args else ''} timed out after {timeout_s}s"
                ) from None
            out_f.seek(0)
            err_f.seek(0)
            out = out_f.read().decode(errors="replace")
            err = err_f.read().decode(errors="replace")
            rc = proc.returncode if proc.returncode is not None else -1
        return rc, out, err

    def instance(self, sandbox_id: str) -> Instance:
        try:
            return self._instances[sandbox_id]
        except KeyError:
            raise SandboxNotFound(f"sandbox {sandbox_id} not managed by this driver") from None

    # -------------------------------------------------------------- sandbox

    def capabilities(self) -> Caps:
        return _MICROSANDBOX_CAPS

    async def create(
        self, spec: SandboxSpec, *, from_snapshot: SnapshotArtifact | None = None
    ) -> Instance:
        if from_snapshot is not None:
            if from_snapshot.kind != SnapshotKind.DATA:
                # no memory-state restore exists on this substrate (caps
                # honesty): refuse BEFORE any work, like every driver.
                raise UnsupportedCapability(
                    f"microsandbox driver cannot restore {from_snapshot.kind} snapshots",
                    detail={"supported": [SnapshotKind.DATA], "caps": "snapshot_full=False"},
                )
            if not from_snapshot.path.is_dir():
                raise DriverError(f"snapshot artifact missing: {from_snapshot.path}")
        spec.workspace.mkdir(parents=True, exist_ok=True)
        if from_snapshot is not None:
            # the workspace is host-mounted, so seeding is a host-side copy —
            # the same seeding path as the process driver (ADR-0012 D3).
            _copy_tree(from_snapshot.path, spec.workspace)
        rc, _, err = await self._run(*_render_run_argv(spec), timeout_s=120.0)
        if rc != 0:
            raise DriverError(f"msb run failed ({rc}): {err.strip()}")
        instance = Instance(
            id=spec.sandbox_id,
            spec=spec,
            pid=None,     # the VM belongs to msb; there is no host-pid to own
            process=None, # no stdio handle to attach (exec-based surface)
            started_at=int(time.time() * 1000),
        )
        self._instances[spec.sandbox_id] = instance
        return instance

    async def exec(self, sandbox_id: str, spec: ExecSpec) -> ExecResult:
        self.instance(sandbox_id)
        rc, out, err = await self._run(
            *_render_exec_argv(sandbox_id, spec), timeout_s=spec.timeout_s + 5.0
        )
        return ExecResult(exit_code=rc, stdout=out, stderr=err)

    async def pause(self, sandbox_id: str) -> None:
        """Honest semantics: a graceful STOP, not a memory freeze — processes
        do not survive, workspace data does (see module docstring)."""
        self.instance(sandbox_id)
        rc, _, err = await self._run("stop", sandbox_id, timeout_s=60.0)
        if rc != 0:
            raise DriverError(f"msb stop failed ({rc}): {err.strip()}")
        self._instances[sandbox_id].paused = True

    async def resume(self, sandbox_id: str) -> None:
        self.instance(sandbox_id)
        rc, _, err = await self._run("start", sandbox_id, timeout_s=120.0)
        if rc != 0:
            raise DriverError(f"msb start failed ({rc}): {err.strip()}")
        self._instances[sandbox_id].paused = False

    async def checkpoint(
        self,
        sandbox_id: str,
        kind: SnapshotKind,
        *,
        base: SnapshotArtifact | None = None,
    ) -> SnapshotArtifact:
        if base is not None:
            # delta_snapshots=False (ADR-0012 D1): refuse before any work.
            raise UnsupportedCapability(
                "microsandbox driver does not support delta snapshots",
                detail={"caps": "delta_snapshots=False"},
            )
        instance = self.instance(sandbox_id)
        if kind != SnapshotKind.DATA:
            raise UnsupportedCapability(
                f"microsandbox driver cannot take {kind} snapshots",
                detail={"supported": [SnapshotKind.DATA], "caps": "snapshot_full=False"},
            )
        if self._snapshots_root is None:
            raise DriverError("driver has no snapshots_root configured")
        artifact_dir = self._snapshots_root / new_snapshot_id()
        artifact_dir.mkdir(parents=True, exist_ok=True)
        _copy_tree(instance.spec.workspace, artifact_dir)
        merkle, files, total_bytes = _merkle_root(artifact_dir)
        return SnapshotArtifact(
            snapshot_id=artifact_dir.name,
            subject=sandbox_id,
            kind=SnapshotKind.DATA,
            path=artifact_dir,
            manifest={"files": files, "launcher": instance.spec.argv, "backend": "microsandbox/libkrun"},
            size=total_bytes,
            merkle=merkle,
        )

    async def materialize(self, artifact: SnapshotArtifact, dest: Path) -> None:
        """DATA artifacts are plain full trees (this driver never produces
        deltas — caps honesty, ADR-0012 D1) so materialization is a copy."""
        if artifact.kind != SnapshotKind.DATA or artifact.delta:
            raise UnsupportedCapability(
                f"microsandbox driver materializes full DATA trees only "
                f"(kind={artifact.kind}, delta={artifact.delta})",
                detail={"supported": [SnapshotKind.DATA]},
            )
        if not artifact.path.is_dir():
            raise DriverError(f"snapshot artifact missing: {artifact.path}")
        dest.mkdir(parents=True, exist_ok=True)
        _copy_tree(artifact.path, dest)

    async def destroy(self, sandbox_id: str, grace_s: float = 5.0) -> None:
        self.instance(sandbox_id)
        # --force stops a running sandbox first, then removes it and its disk
        rc, _, err = await self._run("remove", "--force", sandbox_id, timeout_s=60.0)
        if rc != 0:
            raise DriverError(f"msb remove failed ({rc}): {err.strip()}")
        self._instances.pop(sandbox_id, None)
