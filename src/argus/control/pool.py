"""Warm pool: pre-booted sandboxes claimed atomically by CAS (architecture 4.4).

The pool keeps `min_warm` booted-but-unbound sandboxes per configured agent
version. A dispatch first tries to claim one; the claim is a KV compare-and-
swap (`None -> session_id`) so exactly one claimer wins even under concurrency
(the cluster deployment swaps the in-memory KV for Redis without touching
callers). On a miss the dispatcher falls back to a cold start.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from argus.core import AgentSession, Sandbox, SandboxStatus
from argus.hostlet import Hostlet
from argus.storage.providers import KVStore, MetadataStore

logger = logging.getLogger(__name__)


@dataclass
class WarmPoolConfig:
    versions: dict[str, int] = field(default_factory=dict)  # agent_version_id -> min_warm
    maintain_interval_s: float = 5.0


class WarmPool:
    def __init__(
        self,
        store: MetadataStore,
        hostlet: Hostlet,
        kv: KVStore,
        config: WarmPoolConfig | None = None,
    ) -> None:
        self.store = store
        self.hostlet = hostlet
        self.kv = kv
        self.config = config or WarmPoolConfig()
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._maintain_loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _maintain_loop(self) -> None:
        while True:
            try:
                await self.maintain_once()
            except Exception:  # the maintainer must never take down the loop
                logger.exception("warm pool maintenance failed")
            await asyncio.sleep(self.config.maintain_interval_s)

    async def maintain_once(self) -> None:
        for version_id, min_warm in self.config.versions.items():
            warm = await self._warm_sandboxes(version_id)
            for _ in range(min_warm - len(warm)):
                version = await self.store.get_version(version_id)
                if version is None:
                    logger.warning("warm pool configured for unknown version %s", version_id)
                    break
                await self.hostlet.ensure(None, version)

    # ---------------------------------------------------------------- claim

    async def claim(self, version_id: str, session: AgentSession) -> Sandbox | None:
        """Atomically bind a warm sandbox to `session`; None on pool empty."""
        for sandbox in await self._warm_sandboxes(version_id):
            key = f"warm-claim:{sandbox.id}"
            if not await self.kv.cas(key, None, session.id):
                continue  # another claimant won this one
            try:
                bound = await self.hostlet.bind(sandbox.id, session)
            except Exception:
                await self.kv.delete(key)  # release the claim; sandbox state is the store's story
                raise
            await self.kv.delete(key)
            return bound
        return None

    async def _warm_sandboxes(self, version_id: str) -> list[Sandbox]:
        return [
            s
            for s in await self.store.list_sandboxes()
            if s.status == SandboxStatus.WARM and s.agent_version_id == version_id
        ]
