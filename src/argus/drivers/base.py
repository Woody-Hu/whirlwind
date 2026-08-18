"""SandboxDriver abstraction (architecture 4.4): the execution substrate face.

A driver creates and manages sandbox *instances* — process groups, microVMs,
Wasm runtimes. It knows nothing about harnesses, sessions, or seams: the
Hostlet (its only caller) hands it a fully resolved `SandboxSpec` (absolute
launcher argv, bundle root, private workspace, explicit env) and gets back an
`Instance` whose stdio the Hostlet attaches the wire protocol to.

Capabilities are the scheduler's only decision input (ADR D2): each driver
reports them truthfully; nothing is declared that is not enforced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from argus.core import SnapshotKind
from argus.core.errors import ArgusError


class DriverError(ArgusError):
    code = "argus/driver"


class SandboxNotFound(DriverError):
    code = "argus/driver/not-found"


class UnsupportedCapability(DriverError):
    code = "argus/driver/unsupported"


class Isolation(StrEnum):
    PROCESS = "process"        # process group + filesystem scoping (config-level)
    MICRO_VM = "micro_vm"      # firecracker-class
    LIGHT_VM = "light_vm"      # libkrun / gVisor-class


class Density(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True, slots=True)
class Caps:
    """Truthful capability bits. The scheduler filters only on these."""

    isolation: Isolation
    snapshot_full: bool        # memory + disk checkpoint
    snapshot_data: bool        # disk-layer (workspace) checkpoint
    background_restore: bool   # kernel-first restore that returns immediately
    net_policy: bool           # user-space network policy enforcement
    density: Density


@dataclass(slots=True)
class SandboxSpec:
    """A fully-resolved launch request. `env` is a whitelist — nothing else
    from the host environment crosses the sandbox boundary."""

    sandbox_id: str
    argv: list[str]            # absolute launcher command (argv[0] inside bundle_root)
    bundle_root: Path          # self-contained runtime directory (image bundle)
    workspace: Path            # sandbox-private cwd; the only writable area
    env: dict[str, str] = field(default_factory=dict)
    pool_id: str = "default"


@dataclass(slots=True)
class Instance:
    id: str
    spec: SandboxSpec
    pid: int | None
    process: Any | None        # asyncio subprocess for stdio attach (Hostlet)
    started_at: int
    paused: bool = False


@dataclass(slots=True)
class SnapshotArtifact:
    snapshot_id: str
    subject: str               # sandbox_id the artifact was taken from
    kind: SnapshotKind
    path: Path                 # location of the artifact payload
    manifest: dict[str, Any] = field(default_factory=dict)
    size: int = 0
    merkle: str = ""


@dataclass(slots=True)
class ExecSpec:
    argv: list[str]
    timeout_s: float = 30.0
    env_extra: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


@runtime_checkable
class SandboxDriver(Protocol):
    """The one interface every sandbox technology implements (architecture 4.4)."""

    name: str

    def capabilities(self) -> Caps: ...

    async def create(
        self, spec: SandboxSpec, *, from_snapshot: SnapshotArtifact | None = None
    ) -> Instance: ...

    async def exec(self, sandbox_id: str, spec: ExecSpec) -> ExecResult: ...

    async def pause(self, sandbox_id: str) -> None: ...

    async def resume(self, sandbox_id: str) -> None: ...

    async def checkpoint(self, sandbox_id: str, kind: SnapshotKind) -> SnapshotArtifact: ...

    async def destroy(self, sandbox_id: str, grace_s: float = 5.0) -> None: ...
