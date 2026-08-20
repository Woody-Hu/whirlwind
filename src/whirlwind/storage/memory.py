"""In-process implementations of MetadataStore / KVStore / LockProvider."""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

from whirlwind.core import (
    AgentDefinition,
    AgentSession,
    AgentVersion,
    CronJob,
    Sandbox,
    Snapshot,
    SkillRef,
)
from whirlwind.core.errors import Conflict, NotFound
from whirlwind.storage.skills import SkillArchives


class MemoryMetadataStore:
    """Dict-backed authoritative metadata. Single-process authority for M1."""

    def __init__(self, skills_dir: Path | None = None) -> None:
        self._agents: dict[str, AgentDefinition] = {}
        self._agents_by_name: dict[str, str] = {}
        self._versions: dict[str, AgentVersion] = {}
        self._sessions: dict[str, AgentSession] = {}
        self._sandboxes: dict[str, Sandbox] = {}
        self._snapshots: dict[str, list[Snapshot]] = {}  # session_id -> newest last
        self._crons: dict[str, CronJob] = {}
        self._skills = SkillArchives(skills_dir) if skills_dir is not None else None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """No-op; uniform lifecycle shape with the database-backed stores."""

    async def aclose(self) -> None:
        """No-op; uniform lifecycle shape with the database-backed stores."""

    async def create_agent(self, agent: AgentDefinition) -> AgentDefinition:
        async with self._lock:
            if agent.name in self._agents_by_name:
                raise Conflict(f"agent name {agent.name!r} already exists")
            self._agents[agent.id] = agent
            self._agents_by_name[agent.name] = agent.id
            return agent

    async def get_agent(self, agent_id: str) -> AgentDefinition | None:
        return self._agents.get(agent_id)

    async def get_agent_by_name(self, name: str) -> AgentDefinition | None:
        agent_id = self._agents_by_name.get(name)
        return self._agents.get(agent_id) if agent_id else None

    async def list_agents(self) -> list[AgentDefinition]:
        return list(self._agents.values())

    async def update_agent(self, agent: AgentDefinition) -> AgentDefinition:
        async with self._lock:
            if agent.id not in self._agents:
                raise NotFound(f"agent {agent.id}")
            self._agents[agent.id] = agent
            return agent

    async def create_version(self, version: AgentVersion) -> AgentVersion:
        async with self._lock:
            self._versions[version.id] = version
            return version

    async def get_version(self, version_id: str) -> AgentVersion | None:
        return self._versions.get(version_id)

    async def list_versions(self, agent_id: str) -> list[AgentVersion]:
        return [v for v in self._versions.values() if v.agent_id == agent_id]

    async def create_session(self, session: AgentSession) -> AgentSession:
        async with self._lock:
            self._sessions[session.id] = session
            return session

    async def get_session(self, session_id: str) -> AgentSession | None:
        return self._sessions.get(session_id)

    async def update_session(self, session: AgentSession) -> AgentSession:
        async with self._lock:
            self._sessions[session.id] = session
            return session

    async def list_sessions(self, agent_id: str | None = None) -> list[AgentSession]:
        if agent_id is None:
            return list(self._sessions.values())
        return [s for s in self._sessions.values() if s.agent_id == agent_id]

    async def upsert_sandbox(self, sandbox: Sandbox) -> Sandbox:
        async with self._lock:
            self._sandboxes[sandbox.id] = sandbox
            return sandbox

    async def get_sandbox(self, sandbox_id: str) -> Sandbox | None:
        return self._sandboxes.get(sandbox_id)

    async def list_sandboxes(self) -> list[Sandbox]:
        return list(self._sandboxes.values())

    async def save_snapshot(self, snapshot: Snapshot) -> Snapshot:
        async with self._lock:
            if snapshot.kind.value in ("full", "data"):
                subject = snapshot.manifest.get("session_id", snapshot.subject)
                self._snapshots.setdefault(subject, []).append(snapshot)
            return snapshot

    async def latest_session_snapshot(self, session_id: str) -> Snapshot | None:
        snaps = self._snapshots.get(session_id)
        return snaps[-1] if snaps else None

    async def save_cron(self, cron: CronJob) -> CronJob:
        async with self._lock:
            if not cron.id:
                cron = cron.model_copy(update={"id": f"cron_{uuid.uuid4().hex[:12]}"})
            self._crons[cron.id] = cron
            return cron

    async def delete_cron(self, cron_id: str) -> None:
        async with self._lock:
            self._crons.pop(cron_id, None)

    async def list_crons(self, agent_id: str | None = None) -> list[CronJob]:
        if agent_id is None:
            return list(self._crons.values())
        return [c for c in self._crons.values() if c.agent_id == agent_id]

    async def save_skill(self, name: str, version: str, archive: Path) -> SkillRef:
        if self._skills is None:
            raise NotFound("skills dir not configured")
        return self._skills.save(name, version, archive)

    async def skill_path(self, ref: SkillRef) -> Path | None:
        if self._skills is None:
            return None
        return self._skills.path(ref)


class MemorySecretStore:
    """In-process envelope store (ADR-0010 D6): for tests and single-proc dev.

    Stores envelope strings only — this class never sees plaintext and holds no
    key material; sealing/unsealing stays in `whirlwind/secrets.py`.
    """

    def __init__(self) -> None:
        self._env: dict[str, dict[str, str]] = {}  # version_id -> {name: envelope}
        self._lock = asyncio.Lock()

    async def put_version_env(self, version_id: str, envelopes: dict[str, str]) -> None:
        async with self._lock:
            self._env[version_id] = dict(envelopes)

    async def get_version_env(self, version_id: str) -> dict[str, str]:
        return dict(self._env.get(version_id, {}))

    async def delete_version_env(self, version_id: str) -> None:
        async with self._lock:
            self._env.pop(version_id, None)


class MemoryKVStore:
    """Dict + lock KV with atomic CAS. Values are strings; TTL is advisory in-proc."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._expiry: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """No-op; uniform lifecycle shape with the database-backed stores."""

    async def aclose(self) -> None:
        """No-op; uniform lifecycle shape with the database-backed stores."""

    async def _sweep(self) -> None:
        now = time.monotonic()
        expired = [k for k, exp in self._expiry.items() if exp <= now]
        for k in expired:
            self._data.pop(k, None)
            del self._expiry[k]

    async def get(self, key: str) -> str | None:
        async with self._lock:
            await self._sweep()
            return self._data.get(key)

    async def put(self, key: str, value: str, *, ttl_s: float | None = None) -> None:
        async with self._lock:
            self._data[key] = value
            if ttl_s is not None:
                self._expiry[key] = time.monotonic() + ttl_s
            else:
                self._expiry.pop(key, None)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._data.pop(key, None)
            self._expiry.pop(key, None)

    async def cas(self, key: str, expected: str | None, new: str) -> bool:
        async with self._lock:
            await self._sweep()
            if self._data.get(key) != expected:
                return False
            self._data[key] = new
            self._expiry.pop(key, None)
            return True


class MemoryLocks:
    """Named mutex set with TTL. Honest in-process singleflight."""

    def __init__(self) -> None:
        self._holders: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, name: str, ttl_s: float = 30.0) -> bool:
        async with self._lock:
            now = time.monotonic()
            exp = self._holders.get(name)
            if exp is not None and exp > now:
                return False
            self._holders[name] = now + ttl_s
            return True

    async def release(self, name: str) -> None:
        async with self._lock:
            self._holders.pop(name, None)
