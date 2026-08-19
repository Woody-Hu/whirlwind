"""In-process implementations of MetadataStore / KVStore / LockProvider."""

from __future__ import annotations

import asyncio
import shutil
import time
import uuid
from pathlib import Path

from argus.core import (
    AgentDefinition,
    AgentSession,
    AgentVersion,
    CronJob,
    Sandbox,
    Snapshot,
    SkillRef,
)
from argus.core.errors import Conflict, NotFound


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
        self._skills: dict[tuple[str, str], Path] = {}
        self._skills_dir = skills_dir
        self._lock = asyncio.Lock()

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
        if self._skills_dir is None:
            raise NotFound("skills dir not configured")
        dest_dir = self._skills_dir / name / version
        dest_dir.mkdir(parents=True, exist_ok=True)
        if archive.is_dir():
            shutil.copytree(archive, dest_dir, dirs_exist_ok=True)
        else:
            shutil.copy(archive, dest_dir / archive.name)
        ref = SkillRef(name=name, version=version)
        self._skills[(name, version)] = dest_dir
        return ref

    async def skill_path(self, ref: SkillRef) -> Path | None:
        if (ref.name, ref.version) in self._skills:
            return self._skills[(ref.name, ref.version)]
        if self._skills_dir is not None:
            candidate = self._skills_dir / ref.name / ref.version
            if candidate.exists():
                return candidate
        return None


class MemoryKVStore:
    """Dict + lock KV with atomic CAS. Values are strings; TTL is advisory in-proc."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._expiry: dict[str, float] = {}
        self._lock = asyncio.Lock()

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
