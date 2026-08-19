"""CronScheduler: cron-triggered session turns on the shared timing wheel (G3).

Jobs persist in the MetadataStore; the scheduler arms one wheel timer per job
at the cron-derived deadline (wall clock converted to the wheel's monotonic
domain at arm time). Firing creates a fresh session (or reuses the pinned one)
and sends the input as a real turn. Re-arming happens after each fire; manual
`trigger()` exercises exactly the same path as a scheduled fire.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Awaitable, Callable

from argus.control.manager import SessionManager
from argus.core import CronJob, SessionPolicy
from argus.core.errors import NotFound
from argus.storage.providers import MetadataStore
from argus.timer.cron import CronExpr
from argus.timer.wheel import HierarchicalTimer, TimerHandle

logger = logging.getLogger(__name__)


class CronScheduler:
    def __init__(self, store: MetadataStore, wheel: HierarchicalTimer, manager: SessionManager) -> None:
        self.store = store
        self.wheel = wheel
        self.manager = manager
        self._jobs: dict[str, CronJob] = {}
        self._handles: dict[str, TimerHandle] = {}

    async def start(self) -> None:
        """(Re)arm every enabled persisted job. Idempotent."""
        for job in await self.store.list_crons():
            if job.enabled:
                self._arm(job)
            else:
                self._jobs[job.id] = job

    async def stop(self) -> None:
        for handle in self._handles.values():
            handle.cancel()
        self._handles.clear()
        self._jobs.clear()

    async def add(self, job: CronJob) -> CronJob:
        if not job.id:
            job = job.model_copy(update={"id": f"cron_{int(time.time() * 1000)}_{len(self._jobs):04d}"})
        await self.store.save_cron(job)
        if job.enabled:
            self._arm(job)
        else:
            self._jobs[job.id] = job
        return job

    async def remove(self, cron_id: str) -> None:
        handle = self._handles.pop(cron_id, None)
        if handle is not None:
            handle.cancel()
        if cron_id not in self._jobs:
            raise NotFound(f"cron {cron_id}")
        self._jobs.pop(cron_id, None)
        await self.store.delete_cron(cron_id)

    async def trigger(self, cron_id: str) -> dict:
        """Fire now (ops/debug face); identical code path to a scheduled fire."""
        job = self._jobs.get(cron_id)
        if job is None:
            raise NotFound(f"cron {cron_id}")
        return await self.fire(job)

    async def fire(self, job: CronJob) -> dict:
        try:
            result = await self._execute(job)
        except Exception:
            logger.exception("cron %s fire failed", job.id)
            raise
        finally:
            if job.enabled:
                self._arm(job)  # next period regardless of this fire's outcome
        return result

    def next_fire(self, job: CronJob) -> float:
        """Monotonic deadline (s) of the next cron fire."""
        expr = CronExpr.parse(job.schedule)
        now_wall = datetime.now()
        nxt = expr.next_after(now_wall)
        return time.monotonic() + max(0.0, (nxt - now_wall).total_seconds())

    # ------------------------------------------------------------ internals

    def _arm(self, job: CronJob) -> None:
        self._jobs[job.id] = job
        old = self._handles.pop(job.id, None)
        if old is not None:
            old.cancel()
        delay_s = self.next_fire(job)
        self._handles[job.id] = self.wheel.schedule(delay_s, self._fire(job.id))

    def _fire(self, cron_id: str) -> Callable[[], Awaitable[None]]:
        async def run() -> None:
            self._handles.pop(cron_id, None)
            job = self._jobs.get(cron_id)
            if job is None or not job.enabled:
                return
            try:
                await self.fire(job)
            except Exception:  # never take the wheel down
                logger.exception("cron %s fire crashed", cron_id)

        return run

    async def _execute(self, job: CronJob) -> dict:
        agent = await self.store.get_agent(job.agent_id)
        if agent is None:
            raise NotFound(f"agent {job.agent_id}")
        version_id = agent.default_version_id
        if version_id is None:
            versions = await self.store.list_versions(agent.id)
            if not versions:
                raise NotFound(f"agent {agent.id} has no versions")
            version_id = versions[0].id

        if job.session_policy == SessionPolicy.REUSE:
            session_id = job.session_id
            if session_id is None:
                sessions = await self.store.list_sessions(agent_id=agent.id)
                live = [s for s in sessions if str(s.status) not in ("closed",)]
                if not live:
                    raise NotFound(f"cron {job.id}: no live session to reuse")
                session_id = live[-1].id
        else:
            session = await self.manager.create_session(agent.id, version_id)
            session_id = session.id

        result = await self.manager.send_turn(session_id, job.input_template)
        return {"session_id": session_id, "message_id": result["message_id"]}
