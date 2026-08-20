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
    # Provenance (ADR-0011 D2): set by instance resolution to the SeamInstance
    # name that materialized this decl; empty for plain inline declarations.
    instance: str = ""


class SkillRef(BaseModel):
    """Reference to a versioned skill package in the Resource Registry."""

    name: str
    version: str


class SeamParamSpec(BaseModel):
    """One declared parameter of a SeamTemplate (ADR-0011 D1)."""

    name: str
    type: str = "string"  # string | int | number | bool | list
    required: bool = True
    default: Any = None
    description: str = ""


class SeamTemplate(BaseModel):
    """A named, parameterized seam-binding declaration (ADR-0011 D1).

    `policy` and `consumer.config` bodies may contain `${param}` placeholders;
    `params` declares them. Catalog entity addressed by `name`.
    """

    name: str
    seam: str
    provider: str
    policy: dict[str, Any] = Field(default_factory=dict)
    consumers: list[SeamConsumerDecl] = Field(default_factory=list)
    params: list[SeamParamSpec] = Field(default_factory=list)
    description: str = ""
    created_at: int = Field(default_factory=lambda: int(time.time() * 1000))


class SeamInstance(BaseModel):
    """A template + concrete params; the unit a sandbox binds (ADR-0011 D2)."""

    name: str
    template: str
    params: dict[str, Any] = Field(default_factory=dict)
    description: str = ""
    created_at: int = Field(default_factory=lambda: int(time.time() * 1000))


class HarnessBundle(BaseModel):
    """The integral harness image combination, first-class (ADR-0011 D5)."""

    name: str
    harness: str  # adapter id: "dsh" | "echo" | ...
    image_ref: str  # built image in the ImageRegistry
    version: str = ""
    description: str = ""
    entrypoint: list[str] = Field(default_factory=list)  # optional launcher override
    env: dict[str, str] = Field(default_factory=dict)  # image-level defaults; secrets never here
    native_seams: list[str] = Field(default_factory=list)  # declarative surface of the adapter
    created_at: int = Field(default_factory=lambda: int(time.time() * 1000))


class AgentVersion(BaseModel):
    """The immutable deployment unit: harness, image, entrypoint, seams, skills."""

    id: str
    agent_id: str
    version: str
    harness: str  # "dsh" | "echo" | ... (from harness_bundle when bound, ADR-0011 D4)
    image_ref: str
    entrypoint: list[str] = Field(default_factory=list)
    # New binding model (ADR-0011 D4): 0..1 named harness bundle, 0..N named
    # seam instances. Legacy inline `seam_bindings` stays fully supported;
    # resolution merges both and fails closed on seam collisions. Instance
    # references are live (resolved at provision, ConfigMap semantics).
    harness_bundle: str = ""
    seam_instances: list[str] = Field(default_factory=list)
    seam_bindings: list[SeamBindingDecl] = Field(default_factory=list)
    skill_refs: list[SkillRef] = Field(default_factory=list)
    model_config_decl: dict[str, Any] = Field(default_factory=dict)  # provider/model/max_tokens
    # Declared env secret NAMES only (ADR-0010 D1): values live in the
    # SecretStore as encrypted envelopes, so every model_dump() surface
    # (REST, JSONB, logs) can leak names at worst, never values.
    env_secrets: list[str] = Field(default_factory=list)
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
