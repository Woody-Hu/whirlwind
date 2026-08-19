"""Lifecycle manager: idle-timeout and max-duration enforcement (architecture G4).

Both timers ride the hierarchical timing wheel (ADR D9) — one wheel drives
cron, keepalive, and lifecycle alike. Callbacks are async; the wheel fires
them on the event loop.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from argus.core import AgentSession
from argus.timer.wheel import HierarchicalTimer, TimerHandle

logger = logging.getLogger(__name__)

OnSessionEvent = Callable[[str, str], Awaitable[None]]  # (session_id, event_name)


class LifecycleManager:
    """Schedules and cancels per-session deadlines. Single authority for timers."""

    def __init__(self, wheel: HierarchicalTimer) -> None:
        self.wheel = wheel
        self._idle_handles: dict[str, TimerHandle] = {}
        self._max_handles: dict[str, TimerHandle] = {}
        self._on_expire: OnSessionEvent | None = None

    def on_expire(self, callback: OnSessionEvent) -> None:
        self._on_expire = callback

    def arm_max_duration(self, session: AgentSession) -> None:
        self.cancel_max_duration(session.id)
        self._max_handles[session.id] = self.wheel.schedule(
            session.max_duration_s, self._fire(session.id, "max_duration")
        )

    def arm_idle_timeout(self, session: AgentSession) -> None:
        self.cancel_idle_timeout(session.id)
        self._idle_handles[session.id] = self.wheel.schedule(
            session.idle_timeout_s, self._fire(session.id, "idle_timeout")
        )

    def cancel_idle_timeout(self, session_id: str) -> None:
        handle = self._idle_handles.pop(session_id, None)
        if handle is not None:
            handle.cancel()

    def cancel_max_duration(self, session_id: str) -> None:
        handle = self._max_handles.pop(session_id, None)
        if handle is not None:
            handle.cancel()

    def cancel_all(self, session_id: str) -> None:
        self.cancel_idle_timeout(session_id)
        self.cancel_max_duration(session_id)

    def _fire(self, session_id: str, event: str) -> Callable[[], Awaitable[None]]:
        async def run() -> None:
            if event == "idle_timeout":
                self._idle_handles.pop(session_id, None)
            else:
                self._max_handles.pop(session_id, None)
            if self._on_expire is not None:
                try:
                    await self._on_expire(session_id, event)
                except Exception:  # lifecycle must never take down the loop
                    logger.exception("lifecycle expiry handler failed for %s", session_id)

        return run
