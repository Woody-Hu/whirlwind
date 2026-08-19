"""Domain entities: definition group, run group, trigger group (architecture 05)."""

from __future__ import annotations

import time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from .events import SessionEvent


# ---------------------------------------------------------------- definition


class AgentDefinition(BaseModel):
    """A logical class of agent. Identity is immutable; the default version pointer moves."""

    id: str
    name: str
    display_name: str = ""
    owner: str = "default"
    default_version_id: str | None = None
    created_at: int = Field(default_factory=lambda: int(time.time() * 1000))


class SeamConsumerDecl(BaseModel):
    """How a seam is exposed to a harness: native mapping or MCP fallback."""

    harness: str  # "dsh" | "*" | any adapter id
    mode: str = "native"  # "native" | "mcp"
    config: dict[str, Any] = Field(default_factory=dict)


class SeamBindingDecl(BaseModel):
    """Declared in AgentVersion; input to the Seam Renderer (architecture 4.6)."""

    seam: str  # e.g. "fs.v1"
    provider: str  # e.g. "sandbox-fs"
    policy: dict[str, Any] = Field(default_factory=dict)
    consumers: list[SeamConsumerDecl] = Field(default_factory=list)


class SkillRef(BaseModel):
    """Reference to a versioned skill package in the Resource Registry."""

    name: str
    version: str


class AgentVersion(BaseModel):
    """The immutable deployment unit: harness, image, entrypoint, seams, skills."""

    id: str
    agent_id: str
    version: str
    harness: str  # "dsh" | "echo" | ...
    image_ref: str
    entrypoint: list[str] = Field(default_factory=list)
    seam_bindings: list[SeamBindingDecl] = Field(default_factory=list)
    skill_refs: list[SkillRef] = Field(default_factory=list)
    model_config_decl: dict[str, Any] = Field(default_factory=dict)  # provider/model/max_tokens
    created_at: int = Field(default_factory=lambda: int(time.time() * 1000))


# ---------------------------------------------------------------------- run


class SessionStatus(StrEnum):
    CREATED = "created"
    DISPATCHING = "dispatching"
    RUNNING = "running"
    IDLE = "idle"
    SUSPENDING = "suspending"
    SUSPENDED = "suspended"
    RESUMING = "resuming"
    CLOSED = "closed"


class AgentSession(BaseModel):
    """The single scheduling unit of the whole system (architecture 2.4)."""

    id: str
    agent_id: str
    agent_version_id: str
    status: SessionStatus = SessionStatus.CREATED
    bound_sandbox_id: str | None = None
    route_epoch: int = 0
    idle_timeout_s: float = 300.0
    max_duration_s: float = 3600.0
    idle_deadline: float | None = None  # monotonic clock deadline for keepalive
    last_event_seq: int = 0
    created_at: int = Field(default_factory=lambda: int(time.time() * 1000))


class SandboxStatus(StrEnum):
    PROVISIONING = "provisioning"
    WARM = "warm"
    BINDING = "binding"
    ACTIVE = "active"
    SNAPSHOTTING = "snapshotting"
    SUSPENDED = "suspended"
    RESUMING = "resuming"
    DRAINING = "draining"
    CRASHED = "crashed"
    TERMINATED = "terminated"


class Sandbox(BaseModel):
    """The physical execution unit. Lifecycle is independent of any session."""

    id: str
    pool_id: str
    agent_version_id: str | None = None
    status: SandboxStatus = SandboxStatus.PROVISIONING
    bound_session_id: str | None = None
    route_epoch: int = 0
    last_snapshot_id: str | None = None
    workspace: str | None = None
    created_at: int = Field(default_factory=lambda: int(time.time() * 1000))


class SnapshotKind(StrEnum):
    GOLDEN = "golden"
    FULL = "full"
    DATA = "data"


class Snapshot(BaseModel):
    kind: SnapshotKind
    subject: str  # agent_version_id (golden) | sandbox_id (full/data)
    manifest: dict[str, Any] = Field(default_factory=dict)
    location: str = ""
    size: int = 0
    merkle: str = ""
    created_at: int = Field(default_factory=lambda: int(time.time() * 1000))


# ----------------------------------------------------------------- trigger


class SessionPolicy(StrEnum):
    """What a cron trigger does with sessions: reuse one long-lived or spawn fresh."""

    REUSE = "reuse"
    FRESH = "fresh"


class CronJob(BaseModel):
    agent_id: str
    schedule: str  # 5-field cron
    input_template: str
    session_policy: SessionPolicy = SessionPolicy.FRESH
    session_id: str | None = None  # target when policy == REUSE
    id: str = ""
    enabled: bool = True


def event_from_parts(session_id: str, seq: int, type_: str, data: dict | None = None) -> SessionEvent:
    return SessionEvent(session_id=session_id, seq=seq, type=type_, data=data or {})
