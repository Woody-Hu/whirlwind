"""ProcessDriver: the M1 default sandbox substrate (ADR D2).

Isolation boundary, truthfully reported as `Isolation.PROCESS`:
- own process group (`start_new_session`) — group-wide stop/cont/term signals
- cwd pinned to the sandbox-private workspace; the only directory we create
- env is a strict whitelist (spec.env only — the host environment does not
  leak into the sandbox), secrets never enter this env (they live in Hostlet)
- resource ceilings via POSIX rlimits applied between fork and exec (ADR-0005 D1)

Data snapshots are real workspace copies with a content-hash merkle root;
`snapshot_full` is declared and enforced as unsupported. DATA snapshots may
be DELTAS against a caller-supplied base artifact (ADR-0012): the payload
stores only the file-level overlay (new/modified files + a WHIRLWIND_DELTA.json
index of deletions/dirs) and `materialize()` reconstructs the full tree by
walking the base chain with per-hop merkle verification.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import resource
import shutil
import signal
import tempfile
import time
from functools import partial
from pathlib import Path
from typing import Any

from whirlwind.core import SnapshotKind, new_snapshot_id
from whirlwind.core.platform import platform_impl, resolve_impl

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
    delta_snapshots=True,  # workspace trees diff/apply in pure logic (ADR-0012 D1)
)

# Reserved name at a delta artifact's payload root (ADR-0012 D2). A workspace
# that itself reserves this exact root-level name cannot be delta-checkpointed
# and fails closed (DriverError) instead of corrupting the index.
_DELTA_INDEX = "WHIRLWIND_DELTA.json"

# (rlimit, soft, hard, label) triples the platform policy decided to apply.
# The plan is resolved in the PARENT (ADR-0007 D3) — only the precomputed list
# crosses into preexec_fn.
_LimitPlan = list[tuple[int, int, int, str]]


@platform_impl("drivers.process.rlimits", "*")
def _rlimits_posix(res: Resources) -> _LimitPlan:
    """POSIX-default policy (Linux): all three ceilings map to rlimits."""
    plan: _LimitPlan = []
    if res.mem_limit_mb is not None:
        as_bytes = res.mem_limit_mb * 1024 * 1024
        plan.append((resource.RLIMIT_AS, as_bytes, as_bytes, "mem_limit_mb"))
    if res.cpu_seconds is not None:
        # soft triggers SIGXCPU, hard kills — equal means no grace period
        plan.append((resource.RLIMIT_CPU, res.cpu_seconds, res.cpu_seconds, "cpu_seconds"))
    if res.pids_max is not None:
        plan.append((resource.RLIMIT_NPROC, res.pids_max, res.pids_max, "pids_max"))
    return plan


@platform_impl("drivers.process.rlimits", "macos")
def _rlimits_macos(res: Resources) -> _LimitPlan:
    """macOS policy: the kernel rejects setrlimit(RLIMIT_AS, soft=hard) with
    "current limit exceeds maximum limit", so memory ceilings are honestly
    dropped instead of silently claimed (ADR-0005 D1 caveat); CPU/NPROC apply."""
    return [l for l in _rlimits_posix(res) if l[0] != resource.RLIMIT_AS]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scan_tree(root: Path) -> tuple[dict[str, tuple[str, str, int]], set[str]]:
    """One pass over a tree: rel path -> (kind, digest|target, size) for
    files/symlinks plus the set of directory rel paths. Symlinks are never
    followed (snapshot semantics: the link itself is the content)."""
    files: dict[str, tuple[str, str, int]] = {}
    dirs: set[str] = set()
    for path in root.rglob("*"):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            files[rel] = ("link", os.readlink(path), 0)
        elif path.is_dir():
            dirs.add(rel)
        elif path.is_file():
            files[rel] = ("file", _file_sha256(path), path.stat().st_size)
    return files, dirs


def _merkle_root(root: Path) -> tuple[str, int, int]:
    """Content-addressed digest of a directory tree: sha256 over the sorted
    (relative path, per-file sha256) pairs. Returns (root_hash, files, bytes).
    The SAME digest rules identify a delta artifact's *materialized* end state
    (ADR-0012 D2 invariant 1) — full and delta snapshots of one tree state
    share one root hash."""
    files, _dirs = _scan_tree(root)
    entries: list[str] = []
    total_bytes = 0
    for rel in sorted(files):
        kind, value, size = files[rel]
        if kind == "link":
            entries.append(f"{rel}:link:{value}")
        else:
            total_bytes += size
            entries.append(f"{rel}:sha256:{value}")
    root_hash = hashlib.sha256("\n".join(entries).encode()).hexdigest()
    return root_hash, len(entries), total_bytes


def _diff_trees(base_root: Path, live_root: Path) -> dict[str, Any]:
    """File-level diff of two trees (both must exist). Returns the overlay
    description: changed (added/modified rel paths), deleted files, added and
    deleted directories. Unchanged content is simply absent — that is the
    entire size win of delta snapshots."""
    base_files, base_dirs = _scan_tree(base_root)
    live_files, live_dirs = _scan_tree(live_root)
    changed = sorted(
        rel for rel, entry in live_files.items() if base_files.get(rel) != entry
    )
    deleted_files = sorted(rel for rel in base_files if rel not in live_files)
    return {
        "changed": changed,
        "deleted_files": deleted_files,
        "dirs_added": sorted(live_dirs - base_dirs),
        "dirs_deleted": sorted(base_dirs - live_dirs),
    }


def _write_delta_payload(artifact_dir: Path, live_root: Path, diff: dict[str, Any]) -> int:
    """Materialize the overlay payload: changed files copied at their rel
    paths + the WHIRLWIND_DELTA.json index. Returns payload bytes."""
    payload_bytes = 0
    for rel in diff["changed"]:
        src = live_root / rel
        target = artifact_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if src.is_symlink():
            os.symlink(os.readlink(src), target)
        else:
            shutil.copy2(src, target, follow_symlinks=False)
            payload_bytes += src.stat().st_size
    return payload_bytes


def _apply_delta(payload_dir: Path, dest: Path) -> None:
    """Apply a delta payload onto an already-materialized base tree at dest:
    deletions first, then directory creates, then overlay copies."""
    index_path = payload_dir / _DELTA_INDEX
    if not index_path.is_file():
        raise DriverError(f"delta artifact corrupt: missing {_DELTA_INDEX} in {payload_dir}")
    index = json.loads(index_path.read_text())
    for rel in index.get("deleted_files", []):
        (dest / rel).unlink(missing_ok=True)
    for rel in index.get("dirs_deleted", []):
        shutil.rmtree(dest / rel, ignore_errors=True)
    for rel in index.get("dirs_added", []):
        (dest / rel).mkdir(parents=True, exist_ok=True)
    for item in payload_dir.rglob("*"):
        rel = item.relative_to(payload_dir).as_posix()
        if rel == _DELTA_INDEX:
            continue
        target = dest / rel
        if item.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                target.unlink()
            os.symlink(os.readlink(item), target)
        elif item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target, follow_symlinks=False)


def _base_link(base: SnapshotArtifact) -> dict[str, Any]:
    """The manifest identity of a delta's direct predecessor (ADR-0012 D2)."""
    return {
        "snapshot_id": base.snapshot_id,
        "path": str(base.path),
        "merkle": base.merkle,
        "delta": base.delta,
        "chain_depth": base.manifest.get("chain_depth", 1 if base.delta else 0),
    }


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
            # delta artifacts carry a chain — reconstruction is driver-owned
            # (ADR-0012 D3), so seeding routes through materialize()
            await self.materialize(from_snapshot, spec.workspace)
        # Resolve the platform rlimit policy in the parent (ADR-0007 D3) and
        # validate each planned limit against the process hard cap pre-fork:
        # a mismatch must surface as a clean DriverError, not a preexec_fn crash.
        res = spec.resources
        limits = resolve_impl("drivers.process.rlimits")(res) if res != Resources() else []
        for which, soft, _hard, label in limits:
            _check_limit_feasible(which, soft, label)
        try:
            proc = await asyncio.create_subprocess_exec(
                *spec.argv,
                cwd=str(spec.workspace),
                env=dict(spec.env),  # whitelist: exactly what the spec says
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # own process group (POSIX: Linux + macOS)
                preexec_fn=partial(_apply_rlimits, limits) if limits else None,
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

    async def checkpoint(
        self,
        sandbox_id: str,
        kind: SnapshotKind,
        *,
        base: SnapshotArtifact | None = None,
    ) -> SnapshotArtifact:
        instance = self.instance(sandbox_id)
        if kind != SnapshotKind.DATA:
            raise UnsupportedCapability(
                f"process driver cannot take {kind} snapshots",
                detail={"supported": [SnapshotKind.DATA]},
            )
        if self._snapshots_root is None:
            raise DriverError("driver has no snapshots_root configured")
        if base is not None:
            return await self._checkpoint_delta(instance, base)
        artifact_dir = self._snapshots_root / new_snapshot_id()
        artifact_dir.mkdir(parents=True, exist_ok=True)
        _copy_tree(instance.spec.workspace, artifact_dir)
        merkle, files, total_bytes = _merkle_root(artifact_dir)
        return SnapshotArtifact(
            snapshot_id=artifact_dir.name,
            subject=sandbox_id,
            kind=SnapshotKind.DATA,
            path=artifact_dir,
            manifest={"files": files, "launcher": instance.spec.argv, "chain_depth": 0},
            size=total_bytes,
            merkle=merkle,
        )

    async def _checkpoint_delta(
        self, instance: Instance, base: SnapshotArtifact
    ) -> SnapshotArtifact:
        """Diff the live workspace against `base` and store only the overlay
        (ADR-0012 D2/D3). The artifact's merkle is the MATERIALIZED end state
        (= the live tree), so a later restore verifies equality for free."""
        if self._snapshots_root is None:
            raise DriverError("driver has no snapshots_root configured")
        live = instance.spec.workspace
        if (live / _DELTA_INDEX).exists():
            raise DriverError(
                f"workspace reserves the delta index name {_DELTA_INDEX!r}; "
                "cannot produce a delta snapshot (fail closed)"
            )
        # Diff base: a full base is diffed in place; a delta base must first be
        # materialized (chain reconstruction into a scratch dir).
        scratch: Path | None = None
        try:
            if base.delta:
                if not base.path.is_dir():
                    raise DriverError(f"base artifact missing: {base.path}")
                scratch = Path(tempfile.mkdtemp(prefix="ww-delta-diff-"))
                await self.materialize(base, scratch)
                base_root = scratch
            else:
                if not base.path.is_dir():
                    raise DriverError(f"base artifact missing: {base.path}")
                base_root = base.path
            diff = _diff_trees(base_root, live)
            artifact_dir = self._snapshots_root / new_snapshot_id()
            artifact_dir.mkdir(parents=True, exist_ok=True)
            payload_bytes = _write_delta_payload(artifact_dir, live, diff)
            link = _base_link(base)
            index = {
                "base": link,
                "deleted_files": diff["deleted_files"],
                "dirs_added": diff["dirs_added"],
                "dirs_deleted": diff["dirs_deleted"],
                "chain_depth": link["chain_depth"] + 1,
            }
            index_path = artifact_dir / _DELTA_INDEX
            index_path.write_text(json.dumps(index, indent=2, sort_keys=True))
            payload_bytes += index_path.stat().st_size
            merkle, files, _total = _merkle_root(live)
            return SnapshotArtifact(
                snapshot_id=artifact_dir.name,
                subject=instance.id,
                kind=SnapshotKind.DATA,
                path=artifact_dir,
                manifest={
                    "files": files,
                    "launcher": instance.spec.argv,
                    "base": link,
                    "chain_depth": index["chain_depth"],
                },
                size=payload_bytes,
                merkle=merkle,
                delta=True,
            )
        finally:
            if scratch is not None:
                shutil.rmtree(scratch, ignore_errors=True)

    async def materialize(self, artifact: SnapshotArtifact, dest: Path) -> None:
        """Reconstruct the artifact's full tree at dest (ADR-0012 D3). Full
        artifacts copy; delta artifacts recursively materialize their base
        chain (each hop merkle-verified) and apply overlays in order."""
        if artifact.kind != SnapshotKind.DATA:
            raise UnsupportedCapability(
                f"process driver materializes DATA trees, not {artifact.kind}"
            )
        if not artifact.delta:
            if not artifact.path.is_dir():
                raise DriverError(f"snapshot artifact missing: {artifact.path}")
            dest.mkdir(parents=True, exist_ok=True)
            _copy_tree(artifact.path, dest)
            return
        if not artifact.path.is_dir():
            raise DriverError(f"delta artifact missing: {artifact.path}")
        index_path = artifact.path / _DELTA_INDEX
        if not index_path.is_file():
            raise DriverError(f"delta artifact corrupt: missing {_DELTA_INDEX}")
        dest.mkdir(parents=True, exist_ok=True)
        existing, _dirs = _scan_tree(dest)
        if existing:
            raise DriverError(
                "materialize(delta) requires a file-empty dest — chain "
                f"verification is meaningless over {len(existing)} existing entries"
            )
        link = json.loads(index_path.read_text())["base"]
        base_artifact = SnapshotArtifact(
            snapshot_id=str(link.get("snapshot_id", "")),
            subject=artifact.subject,
            kind=SnapshotKind.DATA,
            path=Path(str(link["path"])),
            manifest={"chain_depth": link.get("chain_depth", 1)},
            merkle=str(link.get("merkle", "")),
            delta=bool(link.get("delta", False)),
        )
        await self.materialize(base_artifact, dest)
        got, _, _ = _merkle_root(dest)
        if got != base_artifact.merkle:
            raise DriverError(
                f"delta chain broken at base {base_artifact.snapshot_id!r}: "
                f"materialized merkle {got} != recorded {base_artifact.merkle}"
            )
        _apply_delta(artifact.path, dest)
        if artifact.merkle:
            got, _, _ = _merkle_root(dest)
            if got != artifact.merkle:
                raise DriverError(
                    f"delta artifact {artifact.snapshot_id!r} failed end-state "
                    f"verification: {got} != {artifact.merkle}"
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


def _check_limit_feasible(which: int, res_value: int, label: str) -> None:
    """Validate a requested soft/hard limit against the process hard cap in
    the *parent* before fork. Runs in the parent (async-signal-safe not
    required); failures surface as pre-spawn DriverError instead of a
    forked-child crash.
    """
    try:
        _, hard = resource.getrlimit(which)
    except (ValueError, OSError) as exc:
        raise DriverError(f"cannot read rlimit {label}: {exc}") from exc
    if hard != resource.RLIM_INFINITY and res_value > hard:
        raise DriverError(
            f"requested {label}={res_value} exceeds the hard cap {hard}"
        )


def _apply_rlimits(limits: _LimitPlan):
    """preexec_fn body: apply the platform-resolved rlimit plan before exec.

    Runs in the forked child between fork and exec (single-threaded there),
    which is the only correct place to apply per-process limits. The plan
    itself is resolved in the parent (`create`) — nothing here reads the
    environment or probes the system (async-signal-safety, ADR-0007 D3).

    Async-signal-safety: this body MUST NOT raise. `preexec_fn` running in a
    multithreaded parent can otherwise surface `SubprocessError: Exception
    occurred in preexec_fn` and the sandbox never boots (Platform portability:
    this repo runs on macOS and Linux). Feasibility is checked in the parent
    (`_check_limit_feasible`) before spawn, so a normal mismatch fails there
    rather than here; a *write* failure here (e.g. an unexpected EPERM) is
    swallowed so the child still execs. `resource.setrlimit` is a libc call
    that does not allocate, so it is safe to call verbatim.
    """

    def set_limit(which: int, soft: int, hard: int) -> None:
        try:
            resource.setrlimit(which, (soft, hard))
        except (ValueError, OSError):
            # Preexec must stay noexcept; a failed rlimit write must not kill
            # the launch. Feasibility was already validated pre-fork.
            return

    for which, soft, hard, _label in limits:
        set_limit(which, soft, hard)


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
