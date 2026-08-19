"""Pipeline benchmarks (ADR §7): bus fan-out, EventLog append/replay, and the
scheduler's cold-start decision (no sandbox -> ensure complete, real echo image).

All paths are async, so timing is manual with hard asserts at the ADR
acceptance lines; nothing is mocked.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from whirlwind.bus import InProcessEventBus
from whirlwind.core import AgentDefinition, AgentSession, AgentVersion, new_agent_id, new_session_id, new_version_id
from whirlwind.drivers import ProcessDriver
from whirlwind.harness.adapter import default_registry
from whirlwind.hostlet import Hostlet, HostletConfig
from whirlwind.imaging import LocalRegistry, echo_image_build
from whirlwind.seam.model import SeamRenderer
from whirlwind.storage.wal_eventlog import WALEventLog
from whirlwind.storage.memory import MemoryMetadataStore

REPO_ROOT = Path(__file__).resolve().parents[2]

BUS_TOTAL = 10_000
BUS_SUBSCRIBERS = 10
BUS_MIN_RATE = 20_000  # events/s aggregate deliveries

LOG_EVENTS = 10_000
LOG_MIN_RATE = 1_000  # events/s appended sequentially (per-record fsync durability floor)
LOG_BURST = 10_000
LOG_BURST_MIN_RATE = 5_000  # events/s amortized over a group-commit burst (ADR line)


@pytest.mark.asyncio
async def test_bus_fanout_10k() -> None:
    bus = InProcessEventBus()
    subs = [await bus.subscribe("bench.topic") for _ in range(BUS_SUBSCRIBERS)]
    counts = [0] * BUS_SUBSCRIBERS

    async def consume(index: int) -> None:
        while True:
            payload = await subs[index].next()
            if payload is None or payload.get("stop"):
                return
            counts[index] += 1

    tasks = [asyncio.get_running_loop().create_task(consume(i)) for i in range(BUS_SUBSCRIBERS)]
    start = time.perf_counter()
    for i in range(BUS_TOTAL):
        bus.publish("bench.topic", {"seq": i})
        if i % 64 == 0:
            await asyncio.sleep(0)  # real publishers await between turns; pure-sync bursts starve consumers by design (drop-oldest + EventLog catch-up)
    bus.publish("bench.topic", {"stop": True})
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=60)
    elapsed = time.perf_counter() - start

    delivered = sum(counts)
    rate = delivered / elapsed
    print(
        f"\nbus fanout: {BUS_TOTAL} events x {BUS_SUBSCRIBERS} subs -> "
        f"{delivered} deliveries in {elapsed:.3f}s = {rate:,.0f}/s"
    )
    assert delivered == BUS_TOTAL * BUS_SUBSCRIBERS  # fast consumers: zero drops
    assert rate >= BUS_MIN_RATE, f"bus fanout {rate:,.0f}/s below {BUS_MIN_RATE:,}/s"
    await bus.close()


@pytest.mark.asyncio
async def test_eventlog_append_replay(tmp_path: Path) -> None:
    log = WALEventLog(tmp_path / "events")
    session_id = new_session_id()
    payload = {"text": "benchmark event", "n": 0}

    start = time.perf_counter()
    for i in range(LOG_EVENTS):
        payload["n"] = i
        await log.append(session_id, "assistant/chunk", payload)
    elapsed = time.perf_counter() - start

    rate = LOG_EVENTS / elapsed
    print(f"\neventlog append: {LOG_EVENTS} events in {elapsed:.3f}s = {rate:,.0f}/s")
    assert rate >= LOG_MIN_RATE, f"eventlog append {rate:,.0f}/s below {LOG_MIN_RATE:,}/s"

    replayed = await log.read(session_id, from_seq=0, limit=LOG_EVENTS + 10)
    assert len(replayed) == LOG_EVENTS
    assert replayed[0].seq == 1 and replayed[-1].seq == LOG_EVENTS
    log.close()


@pytest.mark.asyncio
async def test_eventlog_group_commit_burst(tmp_path: Path) -> None:
    """ADR ≥5k/s append line on the durable provider: a concurrent burst shares
    group-commit fsyncs, so the amortized rate clears the line even though
    every record is fsync'd before append() returns."""
    log = WALEventLog(tmp_path / "events")
    session_id = new_session_id()
    start = time.perf_counter()
    await asyncio.gather(
        *[log.append(session_id, "assistant/chunk", {"text": "burst", "n": i}) for i in range(LOG_BURST)]
    )
    elapsed = time.perf_counter() - start

    rate = LOG_BURST / elapsed
    print(f"\neventlog group-commit burst: {LOG_BURST} events in {elapsed:.3f}s = {rate:,.0f}/s")
    assert rate >= LOG_BURST_MIN_RATE, f"eventlog burst {rate:,.0f}/s below {LOG_BURST_MIN_RATE:,}/s"

    replayed = await log.read(session_id, from_seq=0, limit=LOG_BURST + 10)
    assert len(replayed) == LOG_BURST
    assert replayed[-1].seq == LOG_BURST
    log.close()


@pytest.fixture(scope="module")
def echo_registry(tmp_path_factory: pytest.TempPathFactory) -> LocalRegistry:
    registry = LocalRegistry(tmp_path_factory.mktemp("images"))

    async def _build() -> None:
        await registry.register(echo_image_build(REPO_ROOT))

    asyncio.new_event_loop().run_until_complete(_build())
    return registry


@pytest.mark.asyncio
async def test_cold_start_decision(
    tmp_path: Path,
    echo_registry: LocalRegistry,
) -> None:
    """ADR line: no-sandbox -> ensure complete <= 250ms (real echo boot; the
    SandboxAgent subprocess, its uvicorn control face, the harness child, and
    the initialize RPC all really run). No turns -> no LLM traffic."""
    store = MemoryMetadataStore()
    hostlet = Hostlet(
        driver=ProcessDriver(),
        images=echo_registry,
        adapters=default_registry(),
        renderer=SeamRenderer(),
        store=store,
        event_log=WALEventLog(tmp_path / "events"),
        bus=InProcessEventBus(),
        config=HostletConfig(
            data_dir=tmp_path,
            api_key_env="WHIRLWIND_TEST_KEY",
            llm_upstream="http://127.0.0.1:9",  # never reached: no turns are sent
        ),
    )
    await hostlet.start()
    try:
        agent = AgentDefinition(id=new_agent_id(), name="bench-cold")
        await store.create_agent(agent)
        version = AgentVersion(
            id=new_version_id(),
            agent_id=agent.id,
            version="1.0.0",
            harness="echo",
            image_ref="echo",
        )
        await store.create_version(version)

        durations: list[float] = []
        for round_ in range(5):
            session = AgentSession(id=new_session_id(), agent_id=agent.id, agent_version_id=version.id)
            await store.create_session(session)
            start = time.perf_counter()
            sandbox = await hostlet.ensure(session, version)
            durations.append(time.perf_counter() - start)
            assert sandbox.status.value == "active"
            await hostlet.destroy(sandbox.id)

        p50 = sorted(durations)[len(durations) // 2]
        print(
            f"\ncold start (ensure, {len(durations)} rounds): p50={p50 * 1000:.0f}ms "
            f"min={min(durations) * 1000:.0f}ms max={max(durations) * 1000:.0f}ms"
        )
        assert p50 <= 0.250, f"cold start p50 {p50 * 1000:.0f}ms exceeds 250ms"
    finally:
        await hostlet.aclose()
