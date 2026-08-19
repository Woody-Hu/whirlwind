"""Control-plane integration: SessionManager lifecycle over the real stack.

Everything real: echo image sandbox, HarnessRpc turn, HTTP ingest, timing
wheel with a real-time ticker driving idle-timeout expiry and session close.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from argus.bus import InProcessEventBus
from argus.control import LifecycleManager, Scheduler, SessionManager, WarmPool, WarmPoolConfig
from argus.core import (
    AgentDefinition,
    AgentVersion,
    SandboxStatus,
    SessionStatus,
    new_agent_id,
    new_version_id,
)
from argus.core.errors import Conflict, NotFound
from argus.drivers import Density, ProcessDriver
from argus.harness.adapter import default_registry
from argus.hostlet import Hostlet, HostletConfig
from argus.imaging import LocalRegistry, echo_image_build
from argus.seam.model import SeamRenderer
from argus.storage.wal_eventlog import WALEventLog
from argus.storage.memory import MemoryKVStore, MemoryMetadataStore
from argus.timer.wheel import HierarchicalTimer

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def echo_registry(tmp_path_factory: pytest.TempPathFactory) -> LocalRegistry:
    registry = LocalRegistry(tmp_path_factory.mktemp("images"))

    async def _build() -> None:
        await registry.register(echo_image_build(REPO_ROOT))

    asyncio.new_event_loop().run_until_complete(_build())
    return registry


async def _wait_until(predicate, timeout_s: float = 15.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(interval)
    return False


@pytest.mark.asyncio
async def test_session_lifecycle_with_idle_timeout(
    tmp_path: Path,
    echo_registry: LocalRegistry,
    llm_upstream: str,
) -> None:
    store = MemoryMetadataStore()
    bus = InProcessEventBus()
    event_log = WALEventLog(tmp_path / "events")
    hostlet = Hostlet(
        driver=ProcessDriver(),
        images=echo_registry,
        adapters=default_registry(),
        renderer=SeamRenderer(),
        store=store,
        event_log=event_log,
        bus=bus,
        config=HostletConfig(
            data_dir=tmp_path,
            api_key_env="ARGUS_TEST_KEY",
            llm_upstream=llm_upstream,
        ),
    )
    await hostlet.start()
    wheel = HierarchicalTimer(tick_ms=10)
    wheel.start()
    lifecycle = LifecycleManager(wheel)
    manager = SessionManager(store, hostlet, bus, lifecycle)
    await manager.start()
    try:
        agent = AgentDefinition(id=new_agent_id(), name="lifecycle")
        await store.create_agent(agent)
        version = AgentVersion(
            id=new_version_id(),
            agent_id=agent.id,
            version="1.0.0",
            harness="echo",
            image_ref="echo",
            model_config_decl={"provider": "deepseek-official", "model": "deepseek-chat"},
        )
        await store.create_version(version)
        session = await manager.create_session(agent.id, version.id)
        session.idle_timeout_s = 1.0
        session.max_duration_s = 60.0
        await store.update_session(session)

        # -- first turn: dispatch -> running -> (turn) -> idle
        result = await manager.send_turn(session.id, "first")
        assert result["sandbox_id"]
        live = await manager.get_session(session.id)
        assert live.status == SessionStatus.RUNNING

        async def session_idle() -> bool:
            current = await store.get_session(session.id)
            return current is not None and current.status == SessionStatus.IDLE

        assert await _wait_until(session_idle), "session never reached IDLE after turn"

        # -- idle timeout (real wheel ticker) closes the session and sandbox
        async def closed() -> bool:
            current = await store.get_session(session.id)
            return current is not None and current.status == SessionStatus.CLOSED

        assert await _wait_until(closed, timeout_s=6.0), "idle timeout did not close the session"

        async def sandbox_terminated() -> bool:
            record = await store.get_sandbox(result["sandbox_id"])
            return record is not None and record.status == SandboxStatus.TERMINATED

        assert await _wait_until(sandbox_terminated, timeout_s=10.0), "sandbox was not torn down"

        # closed sessions reject turns
        with pytest.raises(Conflict):
            await manager.send_turn(session.id, "nope")
    finally:
        await manager.stop()
        await wheel.stop()
        await hostlet.aclose()


@pytest.mark.asyncio
async def test_second_turn_reuses_sandbox(
    tmp_path: Path,
    echo_registry: LocalRegistry,
    llm_upstream: str,
) -> None:
    store = MemoryMetadataStore()
    bus = InProcessEventBus()
    event_log = WALEventLog(tmp_path / "events")
    hostlet = Hostlet(
        driver=ProcessDriver(),
        images=echo_registry,
        adapters=default_registry(),
        renderer=SeamRenderer(),
        store=store,
        event_log=event_log,
        bus=bus,
        config=HostletConfig(
            data_dir=tmp_path,
            api_key_env="ARGUS_TEST_KEY",
            llm_upstream=llm_upstream,
        ),
    )
    await hostlet.start()
    wheel = HierarchicalTimer(tick_ms=10)
    wheel.start()
    manager = SessionManager(store, hostlet, bus, LifecycleManager(wheel))
    await manager.start()
    try:
        agent = AgentDefinition(id=new_agent_id(), name="reuse")
        await store.create_agent(agent)
        version = AgentVersion(
            id=new_version_id(),
            agent_id=agent.id,
            version="1.0.0",
            harness="echo",
            image_ref="echo",
        )
        await store.create_version(version)
        session = await manager.create_session(agent.id, version.id)
        session.idle_timeout_s = 60.0
        await store.update_session(session)

        first = await manager.send_turn(session.id, "one")

        async def session_idle() -> bool:
            current = await store.get_session(session.id)
            return current is not None and current.status == SessionStatus.IDLE

        assert await _wait_until(session_idle)

        # second turn: same sandbox, no re-dispatch
        before = await store.list_sessions()
        assert len(before) == 1
        second = await manager.send_turn(session.id, "two")
        assert second["sandbox_id"] == first["sandbox_id"]
        assert await _wait_until(session_idle)
        logged = await event_log.read(session.id)
        turn_starts = [e for e in logged if e.type == "turn/start"]
        assert len(turn_starts) == 2  # both turns really executed

        await manager.close_session(session.id)
        closed = await store.get_session(session.id)
        assert closed is not None and closed.status == SessionStatus.CLOSED
    finally:
        await manager.stop()
        await wheel.stop()
        await hostlet.aclose()


def test_scheduler_selects_on_capabilities() -> None:
    scheduler = Scheduler()
    scheduler.register(ProcessDriver())
    driver = scheduler.select(need_snapshot_data=True, min_density=Density.HIGH)
    assert driver.name == "process"
    with pytest.raises(NotFound):
        scheduler.select(need_snapshot_full=True)


@pytest.mark.asyncio
async def test_warm_pool_claim_beats_cold_start(
    tmp_path: Path,
    echo_registry: LocalRegistry,
    llm_upstream: str,
) -> None:
    """Pool maintains min_warm booted sandboxes; dispatch claims one instead
    of cold-starting; concurrent claimants never double-bind (CAS)."""
    store = MemoryMetadataStore()
    bus = InProcessEventBus()
    hostlet = Hostlet(
        driver=ProcessDriver(),
        images=echo_registry,
        adapters=default_registry(),
        renderer=SeamRenderer(),
        store=store,
        event_log=WALEventLog(tmp_path / "events"),
        bus=bus,
        config=HostletConfig(
            data_dir=tmp_path,
            api_key_env="ARGUS_TEST_KEY",
            llm_upstream=llm_upstream,
        ),
    )
    await hostlet.start()
    kv = MemoryKVStore()
    wheel = HierarchicalTimer(tick_ms=10)
    wheel.start()
    agent = AgentDefinition(id=new_agent_id(), name="pooled")
    await store.create_agent(agent)
    version = AgentVersion(
        id=new_version_id(),
        agent_id=agent.id,
        version="1.0.0",
        harness="echo",
        image_ref="echo",
        model_config_decl={"provider": "deepseek-official", "model": "deepseek-chat"},
    )
    await store.create_version(version)
    pool = WarmPool(store, hostlet, kv, WarmPoolConfig(versions={version.id: 2}, maintain_interval_s=3600))
    manager = SessionManager(store, hostlet, bus, LifecycleManager(wheel), pool=pool)
    await manager.start()
    await pool.start()
    try:
        # the maintainer's first pass tops the pool up to min_warm
        async def warm_count(n: int) -> bool:
            return len([s for s in await store.list_sandboxes() if s.status == SandboxStatus.WARM]) == n

        assert await _wait_until(lambda: warm_count(2)), "pool never reached min_warm"

        # two concurrent dispatches: each claims its own warm sandbox (CAS)
        s1 = await manager.create_session(agent.id, version.id)
        s2 = await manager.create_session(agent.id, version.id)
        results = await asyncio.gather(
            manager.send_turn(s1.id, "from pool 1"),
            manager.send_turn(s2.id, "from pool 2"),
        )
        assert results[0]["sandbox_id"] != results[1]["sandbox_id"]
        for r in results:
            record = await store.get_sandbox(r["sandbox_id"])
            assert record is not None and record.status == SandboxStatus.ACTIVE

        # pool drained: the next dispatch falls back to a cold start
        s3 = await manager.create_session(agent.id, version.id)
        third = await manager.send_turn(s3.id, "cold")
        assert third["sandbox_id"] not in {results[0]["sandbox_id"], results[1]["sandbox_id"]}

        # claimed sandboxes really work: turns flow to turn/end
        async def idle(sid: str) -> bool:
            current = await store.get_session(sid)
            return current is not None and current.status == SessionStatus.IDLE

        for s in (s1, s2, s3):
            assert await _wait_until(lambda sid=s.id: idle(sid)), f"session {s.id} never went idle"

        for s in (s1, s2, s3):
            await manager.close_session(s.id)
    finally:
        await pool.stop()
        await manager.stop()
        await wheel.stop()
        await hostlet.aclose()
