"""PostgreSQL MetadataStore: asyncpg pool + JSONB documents (ADR-0004).

Typed columns exist only where the Protocol queries (ids, agent_id, name,
snapshot subject/kind); the Pydantic model is the single source of truth and
round-trips through a JSONB `doc` column. Insertion order is preserved via a
BIGSERIAL `seq` column on every table (parity with the dict-backed memory
store: the cron REUSE policy picks `live[-1]`). Semantics parity with the
memory implementation is pinned by the shared contract suite
(tests/integration/test_storage.py).

Requires the `whirlwind[postgres]` extra. Import this module directly; it is
not re-exported from `whirlwind.storage` so the base package never needs
asyncpg.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import asyncpg

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

_DDL = (
    """
    CREATE TABLE IF NOT EXISTS agents (
        id   TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        seq  BIGSERIAL,
        doc  JSONB NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_versions (
        id       TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        seq      BIGSERIAL,
        doc      JSONB NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_versions_agent ON agent_versions (agent_id)",
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id       TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        seq      BIGSERIAL,
        doc      JSONB NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_sessions_agent ON sessions (agent_id)",
    """
    CREATE TABLE IF NOT EXISTS sandboxes (
        id  TEXT PRIMARY KEY,
        seq BIGSERIAL,
        doc JSONB NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS snapshots (
        seq     BIGSERIAL PRIMARY KEY,
        subject TEXT NOT NULL,
        kind    TEXT NOT NULL,
        doc     JSONB NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_snapshots_subject ON snapshots (subject, seq DESC)",
    """
    CREATE TABLE IF NOT EXISTS crons (
        id       TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        seq      BIGSERIAL,
        doc      JSONB NOT NULL
    )
    """,
)


async def _init_conn(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


class PostgresMetadataStore:
    """Satisfies the `MetadataStore` Protocol against a real PostgreSQL."""

    def __init__(
        self,
        dsn: str,
        *,
        skills_dir: Path | None = None,
        min_size: int = 1,
        max_size: int = 8,
    ) -> None:
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None
        self._skills = SkillArchives(skills_dir) if skills_dir is not None else None
        self._min_size = min_size
        self._max_size = max_size

    async def start(self) -> None:
        """Create the pool (fail fast on a bad DSN) and apply idempotent DDL."""
        if self.pool is not None:
            return
        self.pool = await asyncpg.create_pool(
            self.dsn, min_size=self._min_size, max_size=self._max_size, init=_init_conn
        )
        for statement in _DDL:
            await self.pool.execute(statement)

    async def aclose(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    @property
    def _pg(self) -> asyncpg.Pool:
        if self.pool is None:
            raise RuntimeError("PostgresMetadataStore not started; call start() first")
        return self.pool

    # ------------------------------------------------------------ agents

    async def create_agent(self, agent: AgentDefinition) -> AgentDefinition:
        try:
            async with self._pg.acquire() as conn:
                await conn.execute(
                    "INSERT INTO agents (id, name, doc) VALUES ($1, $2, $3)",
                    agent.id,
                    agent.name,
                    agent.model_dump(mode="json"),
                )
        except asyncpg.UniqueViolationError as exc:
            if exc.constraint_name == "agents_pkey":
                raise Conflict(f"agent {agent.id} already exists") from None
            raise Conflict(f"agent name {agent.name!r} already exists") from None
        return agent

    async def get_agent(self, agent_id: str) -> AgentDefinition | None:
        async with self._pg.acquire() as conn:
            row = await conn.fetchrow("SELECT doc FROM agents WHERE id = $1", agent_id)
        return AgentDefinition.model_validate(row["doc"]) if row else None

    async def get_agent_by_name(self, name: str) -> AgentDefinition | None:
        async with self._pg.acquire() as conn:
            row = await conn.fetchrow("SELECT doc FROM agents WHERE name = $1", name)
        return AgentDefinition.model_validate(row["doc"]) if row else None

    async def list_agents(self) -> list[AgentDefinition]:
        async with self._pg.acquire() as conn:
            rows = await conn.fetch("SELECT doc FROM agents ORDER BY seq")
        return [AgentDefinition.model_validate(r["doc"]) for r in rows]

    async def update_agent(self, agent: AgentDefinition) -> AgentDefinition:
        try:
            async with self._pg.acquire() as conn:
                row = await conn.fetchrow(
                    "UPDATE agents SET doc = $1, name = $2 WHERE id = $3 RETURNING id",
                    agent.model_dump(mode="json"),
                    agent.name,
                    agent.id,
                )
        except asyncpg.UniqueViolationError:
            raise Conflict(f"agent name {agent.name!r} already exists") from None
        if row is None:
            raise NotFound(f"agent {agent.id}")
        return agent

    # ---------------------------------------------------------- versions

    async def create_version(self, version: AgentVersion) -> AgentVersion:
        async with self._pg.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO agent_versions (id, agent_id, doc) VALUES ($1, $2, $3)
                ON CONFLICT (id) DO UPDATE SET agent_id = EXCLUDED.agent_id, doc = EXCLUDED.doc
                """,
                version.id,
                version.agent_id,
                version.model_dump(mode="json"),
            )
        return version

    async def get_version(self, version_id: str) -> AgentVersion | None:
        async with self._pg.acquire() as conn:
            row = await conn.fetchrow("SELECT doc FROM agent_versions WHERE id = $1", version_id)
        return AgentVersion.model_validate(row["doc"]) if row else None

    async def list_versions(self, agent_id: str) -> list[AgentVersion]:
        async with self._pg.acquire() as conn:
            rows = await conn.fetch(
                "SELECT doc FROM agent_versions WHERE agent_id = $1 ORDER BY seq", agent_id
            )
        return [AgentVersion.model_validate(r["doc"]) for r in rows]

    # ---------------------------------------------------------- sessions

    async def create_session(self, session: AgentSession) -> AgentSession:
        async with self._pg.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO sessions (id, agent_id, doc) VALUES ($1, $2, $3)
                ON CONFLICT (id) DO UPDATE SET agent_id = EXCLUDED.agent_id, doc = EXCLUDED.doc
                """,
                session.id,
                session.agent_id,
                session.model_dump(mode="json"),
            )
        return session

    async def get_session(self, session_id: str) -> AgentSession | None:
        async with self._pg.acquire() as conn:
            row = await conn.fetchrow("SELECT doc FROM sessions WHERE id = $1", session_id)
        return AgentSession.model_validate(row["doc"]) if row else None

    async def update_session(self, session: AgentSession) -> AgentSession:
        return await self.create_session(session)  # parity: plain overwrite, no NotFound

    async def list_sessions(self, agent_id: str | None = None) -> list[AgentSession]:
        query = "SELECT doc FROM sessions"
        args: tuple = ()
        if agent_id is not None:
            query += " WHERE agent_id = $1"
            args = (agent_id,)
        async with self._pg.acquire() as conn:
            rows = await conn.fetch(query + " ORDER BY seq", *args)
        return [AgentSession.model_validate(r["doc"]) for r in rows]

    # ---------------------------------------------- sandboxes / snapshots

    async def upsert_sandbox(self, sandbox: Sandbox) -> Sandbox:
        async with self._pg.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO sandboxes (id, doc) VALUES ($1, $2)
                ON CONFLICT (id) DO UPDATE SET doc = EXCLUDED.doc
                """,
                sandbox.id,
                sandbox.model_dump(mode="json"),
            )
        return sandbox

    async def get_sandbox(self, sandbox_id: str) -> Sandbox | None:
        async with self._pg.acquire() as conn:
            row = await conn.fetchrow("SELECT doc FROM sandboxes WHERE id = $1", sandbox_id)
        return Sandbox.model_validate(row["doc"]) if row else None

    async def list_sandboxes(self) -> list[Sandbox]:
        async with self._pg.acquire() as conn:
            rows = await conn.fetch("SELECT doc FROM sandboxes ORDER BY seq")
        return [Sandbox.model_validate(r["doc"]) for r in rows]

    async def save_snapshot(self, snapshot: Snapshot) -> Snapshot:
        subject = snapshot.subject
        if snapshot.kind.value in ("full", "data"):
            subject = snapshot.manifest.get("session_id", snapshot.subject)
        async with self._pg.acquire() as conn:
            await conn.execute(
                "INSERT INTO snapshots (subject, kind, doc) VALUES ($1, $2, $3)",
                subject,
                snapshot.kind.value,
                snapshot.model_dump(mode="json"),
            )
        return snapshot

    async def latest_session_snapshot(self, session_id: str) -> Snapshot | None:
        async with self._pg.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT doc FROM snapshots
                WHERE subject = $1 AND kind IN ('full', 'data')
                ORDER BY seq DESC LIMIT 1
                """,
                session_id,
            )
        return Snapshot.model_validate(row["doc"]) if row else None

    # ------------------------------------------------- crons / resources

    async def save_cron(self, cron: CronJob) -> CronJob:
        if not cron.id:
            cron = cron.model_copy(update={"id": f"cron_{uuid.uuid4().hex[:12]}"})
        async with self._pg.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO crons (id, agent_id, doc) VALUES ($1, $2, $3)
                ON CONFLICT (id) DO UPDATE SET agent_id = EXCLUDED.agent_id, doc = EXCLUDED.doc
                """,
                cron.id,
                cron.agent_id,
                cron.model_dump(mode="json"),
            )
        return cron

    async def delete_cron(self, cron_id: str) -> None:
        async with self._pg.acquire() as conn:
            await conn.execute("DELETE FROM crons WHERE id = $1", cron_id)

    async def list_crons(self, agent_id: str | None = None) -> list[CronJob]:
        query = "SELECT doc FROM crons"
        args: tuple = ()
        if agent_id is not None:
            query += " WHERE agent_id = $1"
            args = (agent_id,)
        async with self._pg.acquire() as conn:
            rows = await conn.fetch(query + " ORDER BY seq", *args)
        return [CronJob.model_validate(r["doc"]) for r in rows]

    async def save_skill(self, name: str, version: str, archive: Path) -> SkillRef:
        if self._skills is None:
            raise NotFound("skills dir not configured")
        return self._skills.save(name, version, archive)

    async def skill_path(self, ref: SkillRef) -> Path | None:
        if self._skills is None:
            return None
        return self._skills.path(ref)
