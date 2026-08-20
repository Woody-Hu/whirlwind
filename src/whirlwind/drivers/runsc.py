"""RunscDriver: gVisor-backed sandbox substrate (ADR D2, architecture 4.4).

gVisor's `runsc` is an OCI runtime: a sandbox is a container whose syscalls are
handled by a user-space kernel (Sentry) instead of the host kernel. Compared to
the process driver this is a real isolation boundary (LIGHT_VM class): the
guest kernel is a separate trust domain, so checkpoint/restore carries memory
state, not just workspace data.

This driver is a thin orchestration layer over the `runsc` CLI (subprocess,
architecture 8.2: Hostlet 对 runsc/firecracker 的二进制编排用 subprocess 封装):

- create:  an OCI bundle is rendered from the SandboxSpec (rootfs = the image
  bundle bind-mounted at `/`; workspace bind-mounted at the process cwd), then
  `runsc run --detach=true`. The process args are translated from host-absolute
  (inside bundle_root) to rootfs-relative.
- exec / pause / resume / checkpoint / restore / destroy: the matching runsc
  subcommands. `snapshot_full` is real: `runsc checkpoint` uses the embedded
  CRIU to dump memory+rootfs; `background_restore` is real: `runsc restore
  --background` returns before the page cache is warm (architecture 4.4).
- Capabilities are truthful (LIGHT_VM / snapshot_full / netstack network
  policy / HIGH density). Nothing is declared that runsc does not enforce.

runsc is Linux-only and not installed in every dev box, so the integration
tests below are gated on the binary being present (`shutil.which("runsc")`).
The capability report and bundle rendering are exercised unconditionally.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from whirlwind.core import SnapshotKind, new_snapshot_id
from whirlwind.core.errors import WhirlwindError

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

_RUNSC_CAPS = Caps(
    isolation=Isolation.LIGHT_VM,     # user-space kernel = a separate trust domain
    snapshot_full=True,               # runsc checkpoint (CRIU) dumps memory + rootfs
    snapshot_data=True,               # workspace-layer checkpoint (host copy)
    background_restore=True,          # runsc restore --background (kernel-first)
    net_policy=True,                  # netstack: per-sandbox network policy
    density=Density.HIGH,             # process-granular, tens-hundreds per node
)

_OCI_VERSION = "1.0.2"

# The workspace is bind-mounted at this rootfs path (process cwd inside the
# sandbox; also the cwd for exec'd processes).
_WORKSPACE_MOUNT = "/workspace"

# minimal rootfs mounts the guest kernel needs to boot a python process
_BASE_MOUNTS: list[dict[str, Any]] = [
    {"destination": "/proc", "type": "proc", "source": "proc"},
    {"destination": "/dev", "type": "tmpfs", "source": "tmpfs"},
    {"destination": "/sys", "type": "sysfs", "source": "sysfs", "options": ["nosuid", "noexec", "nodev", "ro"]},
    {"destination": "/dev/pts", "type": "devpts", "source": "devpts"},
    {"destination": "/dev/shm", "type": "tmpfs", "source": "shm", "options": ["nosuid", "noexec", "nodev"]},
    {"destination": "/tmp", "type": "tmpfs", "source": "tmpfs", "options": ["nosuid", "noexec", "nodev"]},
]


def _merkle_root(root: Path) -> tuple[str, int, int]:
    """Content-addressed digest of a directory tree (shared with process driver)."""

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


def _in_rootfs(bundle_root: Path, host_path: str) -> str:
    """Translate a host-absolute path inside bundle_root to a rootfs-relative one.

    The image bundle becomes the sandbox root filesystem (bind-mounted at `/`),
    so `/bundle/venv/bin/python` is `/venv/bin/python` inside the sandbox.
    """
    path = Path(host_path)
    try:
        rel = path.resolve().relative_to(bundle_root.resolve())
    except ValueError:
        raise DriverError(f"launcher {host_path!r} is outside the bundle root {bundle_root}") from None
    return "/" + rel.as_posix()


def _render_oci_config(
    bundle_root: Path,
    spec: SandboxSpec,
    *,
    cwd: str = _WORKSPACE_MOUNT,
) -> dict[str, Any]:
    """Render an OCI runtime-spec config.json from a fully-resolved SandboxSpec.

    Layout decisions:
    - rootfs is the image bundle bind-mounted at `/` (cheap, no copy);
    - the private workspace is bind-mounted at `cwd` (the only writable area);
    - process args are rootfs-relative; env is exactly the spec whitelist;
    - spec.resources become process rlimits (always) plus cgroup-backed
      linux.resources for memory/pids (the primary gVisor enforcement path,
      ADR-0005 D1) — applied when runsc runs with cgroup support.
    """
    mounts: list[dict[str, Any]] = [
        {
            "destination": "/",
            "type": "bind",
            "source": str(bundle_root.resolve()),
            "options": ["rbind", "rprivate"],
        }
    ]
    for item in _BASE_MOUNTS:
        mounts.append(item)
    mounts.append(
        {
            "destination": cwd,
            "type": "bind",
            "source": str(spec.workspace.resolve()),
            "options": ["rbind", "rprivate", "rw"],
        }
    )
    rlimits: list[dict[str, Any]] = [{"type": "RLIMIT_NOFILE", "hard": 65536, "soft": 65536}]
    linux: dict[str, Any] = {
        "namespaces": [
            {"type": "pid"},
            {"type": "network"},
            {"type": "ipc"},
            {"type": "uts"},
            {"type": "mount"},
        ]
    }
    res = spec.resources
    if res.mem_limit_mb is not None:
        as_bytes = res.mem_limit_mb * 1024 * 1024
        rlimits.append({"type": "RLIMIT_AS", "hard": as_bytes, "soft": as_bytes})
        linux["resources"] = linux.get("resources") or {}
        linux["resources"]["memory"] = {"limit": as_bytes}
    if res.cpu_seconds is not None:
        rlimits.append({"type": "RLIMIT_CPU", "hard": res.cpu_seconds, "soft": res.cpu_seconds})
    if res.pids_max is not None:
        rlimits.append({"type": "RLIMIT_NPROC", "hard": res.pids_max, "soft": res.pids_max})
        linux["resources"] = linux.get("resources") or {}
        linux["resources"]["pids"] = {"limit": res.pids_max}
    return {
        "ociVersion": _OCI_VERSION,
        "process": {
            "terminal": False,
            "user": {"uid": 0, "gid": 0},
            "args": [_in_rootfs(bundle_root, spec.argv[0]), *spec.argv[1:]],
            "env": [f"{k}={v}" for k, v in spec.env.items()],
            "cwd": cwd,
            "rlimits": rlimits,
        },
        "root": {"path": "rootfs", "readonly": False},
        "hostname": spec.sandbox_id[:63] or "whirlwind",
        "mounts": mounts,
        "linux": linux,
    }


def _copy_tree(src: Path, dest: Path) -> None:
    for item in src.iterdir():
        target = dest / item.name
        if item.is_dir():
            shutil.copytree(item, target, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target, follow_symlinks=False)


class RunscDriver(SandboxDriver):
    """Sandbox = one gVisor sandbox (OCI container) + one private workspace."""

    name = "runsc"

    def __init__(
        self,
        *,
        runsc_bin: str = "runsc",
        state_root: Path | None = None,
        snapshots_root: Path | None = None,
        work_root: Path | None = None,
        net: str = "sandbox",
        platform: str | None = None,
        rootless: bool = False,
        ignore_cgroups: bool = False,
    ) -> None:
        self._runsc_bin = runsc_bin
        self._state_root = Path(state_root or "/run/whirlwind-runsc")
        self._snapshots_root = snapshots_root
        # OCI bundle staging area (config.json + rootfs dir per sandbox)
        self._work_root = work_root
        # runsc --network: "sandbox" (per-sandbox netstack, the net_policy
        # capability), "host", or "none". Rootless deployments must pick
        # host/none — runsc rejects sandbox networking without CAP_SYS_ADMIN.
        self._net = net
        self._platform = platform
        self._rootless = rootless
        self._ignore_cgroups = ignore_cgroups
        self._instances: dict[str, Instance] = {}

    # ------------------------------------------------------------ helpers

    def _cli(self, *args: str) -> list[str]:
        cli = [self._runsc_bin, "--root", str(self._state_root), "--network", self._net]
        if self._platform is not None:
            cli += ["--platform", self._platform]
        if self._rootless:
            cli += ["--rootless=true"]
        if self._ignore_cgroups:
            cli += ["--ignore-cgroups"]
        return cli + list(args)

    async def _run(self, *args: str, timeout_s: float = 60.0) -> tuple[int, str, str]:
        """One runsc subprocess; surfaces failures as DriverError.

        Output goes to temp files, not pipes: in --detach mode the runsc
        parent exits quickly while the sandbox processes it spawned inherit
        the stdout/stderr fds — a pipe read would block on their EOF forever.
        proc.wait() targets the parent's exit code, which is what we want.
        """
        with (
            tempfile.TemporaryFile() as out_f,
            tempfile.TemporaryFile() as err_f,
        ):
            try:
                proc = await asyncio.create_subprocess_exec(
                    *self._cli(*args),
                    stdout=out_f,
                    stderr=err_f,
                )
            except FileNotFoundError:
                raise DriverError(
                    f"runsc binary not found ({self._runsc_bin!r}); "
                    "install gVisor or use the process driver",
                    detail={"required": "runsc", "hint": "storage.googleapis.com/gvisor/releases"},
                ) from None
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout_s)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                raise DriverError(
                    f"runsc {args[0] if args else ''} timed out after {timeout_s}s"
                ) from None
            out_f.seek(0)
            err_f.seek(0)
            out = out_f.read().decode(errors="replace")
            err = err_f.read().decode(errors="replace")
            rc = proc.returncode if proc.returncode is not None else -1
        return rc, out, err

    def _bundle_dir(self, sandbox_id: str) -> Path:
        if self._work_root is None:
            raise DriverError("runsc driver has no work_root configured (OCI bundle staging)")
        return self._work_root / sandbox_id

    async def _render_bundle(self, spec: SandboxSpec, *, from_snapshot: Path | None = None) -> Path:
        bundle_dir = self._bundle_dir(spec.sandbox_id)
        if bundle_dir.exists():
            shutil.rmtree(bundle_dir)
        rootfs = bundle_dir / "rootfs"
        rootfs.mkdir(parents=True, exist_ok=True)
        workspace = spec.workspace
        workspace.mkdir(parents=True, exist_ok=True)
        if from_snapshot is not None:
            if not from_snapshot.is_dir():
                raise DriverError(f"snapshot artifact missing: {from_snapshot}")
            _copy_tree(from_snapshot, workspace)
        config = _render_oci_config(spec.bundle_root, spec)
        (bundle_dir / "config.json").write_text(json.dumps(config, indent=2))
        return bundle_dir

    def instance(self, sandbox_id: str) -> Instance:
        try:
            return self._instances[sandbox_id]
        except KeyError:
            raise SandboxNotFound(f"sandbox {sandbox_id} not managed by this driver") from None

    # -------------------------------------------------------------- sandbox

    def capabilities(self) -> Caps:
        return _RUNSC_CAPS

    async def create(
        self, spec: SandboxSpec, *, from_snapshot: SnapshotArtifact | None = None
    ) -> Instance:
        if not spec.argv:
            raise DriverError("sandbox spec requires a launcher argv")
        if self._state_root is not None:
            self._state_root.mkdir(parents=True, exist_ok=True)

        if from_snapshot is not None and from_snapshot.kind == SnapshotKind.FULL:
            # memory + rootfs restore: `runsc restore` boots the checkpointed
            # kernel state, so no fresh run is needed.
            bundle_dir = await self._render_bundle(spec)
            rc, out, err = await self._run(
                "restore",
                "--image-path",
                str(from_snapshot.path),
                "--bundle",
                str(bundle_dir),
                spec.sandbox_id,
            )
            if rc != 0:
                raise DriverError(f"runsc restore failed ({rc}): {err.strip()}")
        else:
            data_seed = from_snapshot.path if from_snapshot is not None else None
            bundle_dir = await self._render_bundle(spec, from_snapshot=data_seed)
            rc, out, err = await self._run(
                "run",
                "--detach=true",
                "--bundle",
                str(bundle_dir),
                spec.sandbox_id,
            )
            if rc != 0:
                raise DriverError(f"runsc run failed ({rc}): {err.strip()}")

        pid = await self._discover_pid(spec.sandbox_id)
        instance = Instance(
            id=spec.sandbox_id,
            spec=spec,
            pid=pid,
            process=None,  # runsc owns the sandbox process; no stdio handle to attach
            started_at=int(time.time() * 1000),
        )
        self._instances[spec.sandbox_id] = instance
        return instance

    async def _discover_pid(self, sandbox_id: str) -> int | None:
        rc, out, err = await self._run("state", sandbox_id)
        if rc != 0:
            raise DriverError(f"runsc state failed ({rc}): {err.strip()}")
        try:
            return int(json.loads(out)["pid"])
        except (ValueError, KeyError, TypeError):
            return None  # paused/stopped sandboxes may report no pid

    async def exec(self, sandbox_id: str, spec: ExecSpec) -> ExecResult:
        instance = self.instance(sandbox_id)
        # cwd must be the rootfs-relative mount point of the workspace
        # (the bind destination in the OCI config), not the host path.
        args = ["exec", "--cwd", _WORKSPACE_MOUNT]
        for key, value in spec.env_extra.items():
            args += ["--env", f"{key}={value}"]
        args += [sandbox_id, *spec.argv]
        rc, out, err = await self._run(*args, timeout_s=spec.timeout_s + 5.0)
        return ExecResult(exit_code=rc, stdout=out, stderr=err)

    async def pause(self, sandbox_id: str) -> None:
        self.instance(sandbox_id)
        rc, _, err = await self._run("pause", sandbox_id)
        if rc != 0:
            raise DriverError(f"runsc pause failed ({rc}): {err.strip()}")
        self._instances[sandbox_id].paused = True

    async def resume(self, sandbox_id: str) -> None:
        self.instance(sandbox_id)
        rc, _, err = await self._run("resume", sandbox_id)
        if rc != 0:
            raise DriverError(f"runsc resume failed ({rc}): {err.strip()}")
        self._instances[sandbox_id].paused = False

    async def checkpoint(
        self,
        sandbox_id: str,
        kind: SnapshotKind,
        *,
        base: SnapshotArtifact | None = None,
    ) -> SnapshotArtifact:
        if base is not None:
            # delta_snapshots=False in caps (ADR-0012 D1): refusing BEFORE any
            # work (not even an instance lookup) is the honest behavior — a
            # CRIU dump is not diffable as-is.
            raise UnsupportedCapability(
                "runsc driver does not support delta snapshots",
                detail={"caps": "delta_snapshots=False"},
            )
        instance = self.instance(sandbox_id)
        if self._snapshots_root is None:
            raise DriverError("driver has no snapshots_root configured")
        artifact_dir = self._snapshots_root / new_snapshot_id()
        artifact_dir.mkdir(parents=True, exist_ok=True)

        if kind == SnapshotKind.FULL:
            rc, _, err = await self._run(
                "checkpoint", "--image-path", str(artifact_dir), sandbox_id
            )
            if rc != 0:
                raise DriverError(f"runsc checkpoint failed ({rc}): {err.strip()}")
            return SnapshotArtifact(
                snapshot_id=artifact_dir.name,
                subject=sandbox_id,
                kind=SnapshotKind.FULL,
                path=artifact_dir,
                manifest={"launcher": instance.spec.argv, "backend": "runsc/CRIU"},
                size=_dir_bytes(artifact_dir),
                merkle="",
            )
        if kind == SnapshotKind.DATA:
            # workspace-layer checkpoint, same as the process driver
            _copy_tree(instance.spec.workspace, artifact_dir)
            merkle, files, total_bytes = _merkle_root(artifact_dir)
            return SnapshotArtifact(
                snapshot_id=artifact_dir.name,
                subject=sandbox_id,
                kind=SnapshotKind.DATA,
                path=artifact_dir,
                manifest={"files": files, "launcher": instance.spec.argv},
                size=total_bytes,
                merkle=merkle,
            )
        raise UnsupportedCapability(
            f"runsc driver cannot take {kind} snapshots",
            detail={"supported": [SnapshotKind.FULL, SnapshotKind.DATA]},
        )

    async def materialize(self, artifact: SnapshotArtifact, dest: Path) -> None:
        """DATA artifacts are plain full trees (this driver never produces
        deltas — caps honesty, ADR-0012 D1) so materialization is a copy.
        FULL artifacts are CRIU image sets consumed by `runsc restore` in
        create(); they are not tree-seedable and refuse honestly here."""
        if artifact.kind != SnapshotKind.DATA or artifact.delta:
            raise UnsupportedCapability(
                f"runsc driver materializes full DATA trees only "
                f"(kind={artifact.kind}, delta={artifact.delta})",
                detail={"supported": [SnapshotKind.DATA]},
            )
        if not artifact.path.is_dir():
            raise DriverError(f"snapshot artifact missing: {artifact.path}")
        dest.mkdir(parents=True, exist_ok=True)
        _copy_tree(artifact.path, dest)

    async def destroy(self, sandbox_id: str, grace_s: float = 5.0) -> None:
        instance = self.instance(sandbox_id)
        rc, _, err = await self._run("kill", sandbox_id, "SIGTERM")
        if rc == 0:
            await asyncio.sleep(min(grace_s, 5.0))
        rc, _, err = await self._run("kill", sandbox_id, "SIGKILL")
        rc, _, err = await self._run("delete", "--force", sandbox_id)
        if rc != 0:
            raise DriverError(f"runsc delete failed ({rc}): {err.strip()}")
        bundle_dir = self._bundle_dir(sandbox_id)
        if bundle_dir.exists():
            shutil.rmtree(bundle_dir, ignore_errors=True)
        self._instances.pop(sandbox_id, None)


def _dir_bytes(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
