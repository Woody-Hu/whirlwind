"""ProcessDriver: the M1 default sandbox substrate (ADR D2).

Isolation boundary, truthfully reported as `Isolation.PROCESS`:
- own process group (`start_new_session`) — group-wide stop/cont/term signals
- cwd pinned to the sandbox-private workspace; the only directory we create
- env is a strict whitelist (spec.env only — the host environment does not
  leak into the sandbox), secrets never enter this env (they live in Hostlet)
- resource ceilings via POSIX rlimits applied between fork and exec (ADR-0005 D1)

Data snapshots are real workspace copies with a content-hash merkle root;
`snapshot_full` is declared and enforced as unsupported.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import resource
import shutil
import signal
import time
from functools import partial
from pathlib import Path

from whirlwind.core import SnapshotKind, new_snapshot_id

from .base import (
    Caps,
    Density,
    DriverError,
    ExecResult,
    ExecSpec,
    Instance,
    Isolation,
    Resources,
    SandboxDriver,
    SandboxNotFound,
    SandboxSpec,
    SnapshotArtifact,
    UnsupportedCapability,
)

_PROCESS_CAPS = Caps(
    isolation=Isolation.PROCESS,
    snapshot_full=False,
    snapshot_data=True,
    background_restore=False,
    net_policy=False,
    density=Density.HIGH,
)


def _merkle_root(root: Path) -> tuple[str, int, int]:
    """Content-addressed digest of a directory tree: sha256 over the sorted
    (relative path, per-file sha256) pairs. Returns (root_hash, files, bytes)."""

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


class ProcessDriver(SandboxDriver):
    """Sandbox = one process group + one private workspace directory."""

    name = "process"

    def __init__(self, snapshots_root: Path | None = None) -> None:
        self._snapshots_root = snapshots_root
        self._instances: dict[str, Instance] = {}

    def capabilities(self) -> Caps:
        return _PROCESS_CAPS

    def instance(self, sandbox_id: str) -> Instance:
        try:
            return self._instances[sandbox_id]
        except KeyError:
            raise SandboxNotFound(f"sandbox {sandbox_id} not managed by this driver") from None

    async def create(
        self, spec: SandboxSpec, *, from_snapshot: SnapshotArtifact | None = None
    ) -> Instance:
        if not spec.argv:
            raise DriverError("sandbox spec requires a launcher argv")
        launcher = Path(spec.argv[0])
        if not launcher.is_file():
            raise DriverError(f"launcher not found in bundle: {launcher}")
        spec.workspace.mkdir(parents=True, exist_ok=True)
        if from_snapshot is not None:
            if not from_snapshot.path.is_dir():
                raise DriverError(f"snapshot artifact missing: {from_snapshot.path}")
            _copy_tree(from_snapshot.path, spec.workspace)
        try:
            proc = await asyncio.create_subprocess_exec(
                *spec.argv,
                cwd=str(spec.workspace),
                env=dict(spec.env),  # whitelist: exactly what the spec says
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # own process group (POSIX: Linux + macOS)
                preexec_fn=partial(_apply_rlimits, spec.resources)
                if spec.resources != Resources()
                else None,
            )
        except OSError as exc:
            raise DriverError(f"failed to launch sandbox: {exc}") from exc
        instance = Instance(
            id=spec.sandbox_id,
            spec=spec,
            pid=proc.pid,
            process=proc,
            started_at=int(time.time() * 1000),
        )
        self._instances[spec.sandbox_id] = instance
        return instance

    async def exec(self, sandbox_id: str, spec: ExecSpec) -> ExecResult:
        instance = self.instance(sandbox_id)
        env = dict(instance.spec.env)
        env.update(spec.env_extra)
        try:
            proc = await asyncio.create_subprocess_exec(
                *spec.argv,
                cwd=str(instance.spec.workspace),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise DriverError(f"exec failed: {exc}") from exc
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=spec.timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise DriverError(f"exec timed out after {spec.timeout_s}s") from None
        return ExecResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=out.decode(errors="replace"),
            stderr=err.decode(errors="replace"),
        )

    async def pause(self, sandbox_id: str) -> None:
        instance = self.instance(sandbox_id)
        _signal_group(instance, signal.SIGSTOP)
        instance.paused = True

    async def resume(self, sandbox_id: str) -> None:
        instance = self.instance(sandbox_id)
        _signal_group(instance, signal.SIGCONT)
        instance.paused = False

    async def checkpoint(self, sandbox_id: str, kind: SnapshotKind) -> SnapshotArtifact:
        instance = self.instance(sandbox_id)
        if kind != SnapshotKind.DATA:
            raise UnsupportedCapability(
                f"process driver cannot take {kind} snapshots",
                detail={"supported": [SnapshotKind.DATA]},
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
            manifest={"files": files, "launcher": instance.spec.argv},
            size=total_bytes,
            merkle=merkle,
        )

    async def destroy(self, sandbox_id: str, grace_s: float = 5.0) -> None:
        instance = self.instance(sandbox_id)
        proc = instance.process
        if proc is not None and proc.returncode is None:
            _signal_group(instance, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=grace_s)
            except TimeoutError:
                _signal_group(instance, signal.SIGKILL)
                await proc.wait()
        self._instances.pop(sandbox_id, None)


def _apply_rlimits(res: Resources):
    """preexec_fn body: set spec.resources as POSIX rlimits before exec.

    Runs in the forked child between fork and exec (single-threaded there),
    which is the only correct place to apply per-process limits.
    """

    def set_limit(which: int, soft: int, hard: int) -> None:
        try:
            resource.setrlimit(which, (soft, hard))
        except (ValueError, OSError) as exc:  # e.g. above the kernel hard cap
            raise DriverError(f"cannot apply rlimit {which}: {exc}") from exc

    if res.mem_limit_mb is not None:
        as_bytes = res.mem_limit_mb * 1024 * 1024
        set_limit(resource.RLIMIT_AS, as_bytes, as_bytes)
    if res.cpu_seconds is not None:
        # soft triggers SIGXCPU, hard kills — equal means no grace period
        set_limit(resource.RLIMIT_CPU, res.cpu_seconds, res.cpu_seconds)
    if res.pids_max is not None:
        set_limit(resource.RLIMIT_NPROC, res.pids_max, res.pids_max)


def _signal_group(instance: Instance, sig: signal.Signals) -> None:
    """Signal the whole process group. A dead group is not an error."""
    pid = instance.pid
    if pid is None:
        return
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def _copy_tree(src: Path, dest: Path) -> None:
    for item in src.iterdir():
        target = dest / item.name
        if item.is_dir():
            shutil.copytree(item, target, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target, follow_symlinks=False)
