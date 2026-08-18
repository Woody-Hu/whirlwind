"""SessionManager + Scheduler: the control plane above the Hostlet (ADR M1-7).

SessionManager owns the AgentSession state machine end to end:
    CREATED -> DISPATCHING -> RUNNING <-> IDLE -> CLOSED
Turns route through the session's bound sandbox; when none is bound the
Scheduler cold-starts one via the Hostlet (warm pool claiming is M2).

Wiring: the Hostlet control face publishes harness status to
`sessions.{id}.status`; SessionManager subscribes once with the wildcard and
drives transitions + lifecycle timers from it.
"""

from __future__ import annotations

import asyncio
import logging

from argus.bus import InProcessEventBus
from argus.core import (
    AgentSession,
    Sandbox,
    SandboxStatus,
    SessionStatus,
    new_session_id,
)
from argus.core.errors import Conflict, InvalidTransition, NotFound
from argus.core.statemachine import SESSION_TRANSITIONS, check_transition
from argus.hostlet import Hostlet
from argus.storage.providers import MetadataStore

from .lifecycle import LifecycleManager

logger = logging.getLogger(__name__)

STATUS_TOPIC = "sessions.*.status"


class SessionManager:
    def __init__(self, store: MetadataStore, hostlet: Hostlet, bus: InProcessEventBus, lifecycle: LifecycleManager) -> None:
        self.store = store
        self.hostlet = hostlet
        self.bus = bus
        self.lifecycle = lifecycle
        self._dispatch_locks: dict[str, asyncio.Lock] = {}
        lifecycle.on_expire(self._on_expire)
        self._status_task: asyncio.Task | None = None

    async def start(self) -> None:
        sub = await self.bus.subscribe(STATUS_TOPIC)
        self._status_task = asyncio.get_running_loop().create_task(self._pump_status(sub))

    async def stop(self) -> None:
        if self._status_task is not None:
            self._status_task.cancel()
            try:
                await self._status_task
            except asyncio.CancelledError:
                pass

    async def _pump_status(self, sub) -> None:
        while True:
            payload = await sub.next()
            if payload is None:
                return
            try:
                await self._on_harness_status(str(payload["session_id"]), str(payload["status"]))
            except Exception:
                logger.exception("status handling failed: %s", payload)

    # ------------------------------------------------------------- session

    async def create_session(self, agent_id: str, version_id: str) -> AgentSession:
        if await self.store.get_agent(agent_id) is None:
            raise NotFound(f"agent {agent_id}")
        if await self.store.get_version(version_id) is None:
            raise NotFound(f"version {version_id}")
        session = AgentSession(id=new_session_id(), agent_id=agent_id, agent_version_id=version_id)
        return await self.store.create_session(session)

    async def get_session(self, session_id: str) -> AgentSession:
        session = await self.store.get_session(session_id)
        if session is None:
            raise NotFound(f"session {session_id}")
        return session

    async def send_turn(self, session_id: str, text: str, content_blocks: list | None = None) -> dict:
        session = await self.get_session(session_id)
        lock = self._dispatch_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            if session.status == SessionStatus.CREATED:
                sandbox = await self._dispatch(session)
            elif session.status in (SessionStatus.IDLE, SessionStatus.RUNNING):
                sandbox = await self._require_live_sandbox(session)
                if session.status == SessionStatus.IDLE:
                    await self._transition(session, SessionStatus.RUNNING)
            else:
                raise Conflict(f"session {session_id} is {session.status}; create a new session")
        self.lifecycle.cancel_idle_timeout(session.id)
        message_id = await self.hostlet.turn(sandbox.id, text, content_blocks)
        return {"message_id": message_id, "sandbox_id": sandbox.id}

    async def close_session(self, session_id: str) -> AgentSession:
        session = await self.get_session(session_id)
        await self._transition(session, SessionStatus.CLOSED)
        self.lifecycle.cancel_all(session.id)
        if session.bound_sandbox_id:
            await self.hostlet.destroy(session.bound_sandbox_id)
        return session

    # ---------------------------------------------------------- status pump

    async def _on_harness_status(self, session_id: str, status: str) -> None:
        session = await self.store.get_session(session_id)
        if session is None:
            return  # not ours (e.g. a harness session without an AgentSession)
        if status == "idle" and session.status == SessionStatus.RUNNING:
            await self._transition(session, SessionStatus.IDLE)
            self.lifecycle.arm_idle_timeout(session)
        self.bus.publish(f"sessions.{session_id}.lifecycle", {"status": session.status})

    async def _on_expire(self, session_id: str, event: str) -> None:
        session = await self.store.get_session(session_id)
        if session is None or session.status == SessionStatus.CLOSED:
            return
        logger.info("session %s expired via %s", session_id, event)
        await self.close_session(session_id)

    # ------------------------------------------------------------ internals

    async def _dispatch(self, session: AgentSession) -> Sandbox:
        await self._transition(session, SessionStatus.DISPATCHING)
        version = await self.store.get_version(session.agent_version_id)
        if version is None:
            raise NotFound(f"version {session.agent_version_id}")
        sandbox = await self.hostlet.ensure(session, version)
        await self._transition(session, SessionStatus.RUNNING)
        self.lifecycle.arm_max_duration(session)
        return sandbox

    async def _require_live_sandbox(self, session: AgentSession) -> Sandbox:
        if session.bound_sandbox_id is None:
            raise Conflict("session has no bound sandbox")
        sandbox = await self.store.get_sandbox(session.bound_sandbox_id)
        if sandbox is None or sandbox.status != SandboxStatus.ACTIVE:
            raise Conflict("session sandbox is gone; create a new session")
        return sandbox

    async def _transition(self, session: AgentSession, wanted: SessionStatus) -> None:
        try:
            check_transition("session", SESSION_TRANSITIONS, session.status, wanted)
        except InvalidTransition:
            if wanted == SessionStatus.CLOSED:
                return  # close is idempotent
            raise
        session.status = wanted
        await self.store.update_session(session)
