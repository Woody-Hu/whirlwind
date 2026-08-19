"""Edge-hardening benchmarks (ADR-0005): what the new gates cost.

Three hot paths, measured with the same manual-timing style as the other
pipeline benches, no mocks:
- idempotency middleware passthrough (requests without a key) — the tax every
  plain request pays;
- idempotency first-execute vs replay — the retry-safety path;
- session-quota admission check (list_sessions over the MetadataStore) — the
  O(live-sessions) cost paid once per create_session;
- cold start with per-sandbox Resources applied — proves the D1 rlimits do
  not regress the 250ms ADR line.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from whirlwind.bus import InProcessEventBus
from whirlwind.core import AgentDefinition, AgentSession, AgentVersion, new_agent_id, new_session_id, new_version_id
from whirlwind.drivers import ProcessDriver, Resources
from whirlwind.gateway.idempotency import IdempotencyMiddleware
from whirlwind.harness.adapter import default_registry
from whirlwind.hostlet import Hostlet, HostletConfig
from whirlwind.imaging import LocalRegistry, echo_image_build
from whirlwind.seam.model import SeamRenderer
from whirlwind.storage.memory import MemoryKVStore, MemoryMetadataStore
from whirlwind.storage.wal_eventlog import WALEventLog

REPO_ROOT = Path(__file__).resolve().parents[2]

PASSTHROUGH_N = 10_000
PASSTHROUGH_MIN_RATE = 20_000  # middleware-level req/s for keyless requests

IDEM_N = 1_000
IDEM_FIRST_MIN_RATE = 300     # claim + execute + store, per request
IDEM_REPLAY_MIN_RATE = 500    # KV read + verbatim replay, per request

QUOTA_SESSIONS = 1_000
QUOTA_MIN_RATE = 1_000        # admission-check (list_sessions) calls per second


# --------------------------------------------------------------- ASGI plumbing

async def _ok_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": b'{"ok": true}'})


def _scope(path: str = "/widgets", idem_key: str | None = None) -> dict[str, Any]:
    headers = []
    if idem_key:
        headers.append((b"idempotency-key", idem_key.encode()))
    return {"type": "http", "asgi": {"version": "3.0"}, "method": "POST",
            "path": path, "headers": headers}


async def _receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b'{"n": 1}', "more_body": False}


async def _null_send(message: dict[str, Any]) -> None:
    return None


# ------------------------------------------------------------------- benchmarks


@pytest.mark.asyncio
async def test_idempotency_passthrough_overhead() -> None:
    """Keyless POSTs pay only a header scan — assert the floor stays high."""
    middleware = IdempotencyMiddleware(_ok_app, MemoryKVStore())

    start = time.perf_counter()
    for _ in range(PASSTHROUGH_N):
        await middleware(_scope(), _receive, _null_send)
    elapsed = time.perf_counter() - start

    rate = PASSTHROUGH_N / elapsed
    print(f"\nidempotency passthrough: {PASSTHROUGH_N} keyless POSTs "
          f"in {elapsed:.3f}s = {rate:,.0f}/s")
    assert rate >= PASSTHROUGH_MIN_RATE, f"passthrough {rate:,.0f}/s below {PASSTHROUGH_MIN_RATE:,}/s"


@pytest.mark.asyncio
async def test_idempotency_first_execute_vs_replay() -> None:
    kv = MemoryKVStore()
    middleware = IdempotencyMiddleware(_ok_app, kv)

    start = time.perf_counter()
    for i in range(IDEM_N):
        await middleware(_scope(idem_key=f"first-{i}"), _receive, _null_send)
    first_elapsed = time.perf_counter() - start

    start = time.perf_counter()
    for i in range(IDEM_N):
        await middleware(_scope(idem_key=f"first-{i}"), _receive, _null_send)
    replay_elapsed = time.perf_counter() - start

    first_rate = IDEM_N / first_elapsed
    replay_rate = IDEM_N / replay_elapsed
    print(f"\nidempotency first-execute: {IDEM_N} keyed POSTs "
          f"in {first_elapsed:.3f}s = {first_rate:,.0f}/s")
    print(f"idempotency replay:        {IDEM_N} keyed POSTs "
          f"in {replay_elapsed:.3f}s = {replay_rate:,.0f}/s")

    assert first_rate >= IDEM_FIRST_MIN_RATE, f"first-execute {first_rate:,.0f}/s below {IDEM_FIRST_MIN_RATE}/s"
    assert replay_rate >= IDEM_REPLAY_MIN_RATE, f"replay {replay_rate:,.0f}/s below {IDEM_REPLAY_MIN_RATE}/s"


@pytest.mark.asyncio
async def test_quota_admission_check_cost() -> None:
    """create_session's gate is one list_sessions() over live sessions —
    the D2 design keeps counting store-derived, so this is the whole cost."""
    store = MemoryMetadataStore()
    agent = AgentDefinition(id=new_agent_id(), name="bench-quota")
    await store.create_agent(agent)
    version = AgentVersion(id=new_version_id(), agent_id=agent.id, version="1.0.0",
                           harness="echo", image_ref="echo")
    await store.create_version(version)
    for _ in range(QUOTA_SESSIONS):
        await store.create_session(AgentSession(id=new_session_id(), agent_id=agent.id,
                                                agent_version_id=version.id))

    rounds = 200
    start = time.perf_counter()
    for _ in range(rounds):
        live = sum(1 for s in await store.list_sessions() if s.status.value != "closed")
        assert live == QUOTA_SESSIONS
    elapsed = time.perf_counter() - start

    rate = rounds / elapsed
    print(f"\nquota admission check over {QUOTA_SESSIONS} live sessions: "
          f"{rounds} checks in {elapsed:.3f}s = {rate:,.0f}/s "
          f"({elapsed / rounds * 1_000:.2f}ms per create_session)")
    assert rate >= QUOTA_MIN_RATE, f"admission check {rate:,.0f}/s below {QUOTA_MIN_RATE:,}/s"


@pytest.fixture(scope="module")
def echo_registry(tmp_path_factory: pytest.TempPathFactory) -> LocalRegistry:
    registry = LocalRegistry(tmp_path_factory.mktemp("images"))

    async def _build() -> None:
        await registry.register(echo_image_build(REPO_ROOT))

    asyncio.new_event_loop().run_until_complete(_build())
    return registry


@pytest.mark.asyncio
async def test_cold_start_with_resource_limits(
    tmp_path: Path,
    echo_registry: LocalRegistry,
) -> None:
    """Same 250ms ADR line as test_pipeline_bench.test_cold_start_decision,
    but with per-sandbox Resources applied — the rlimits ride the fork path
    (preexec_fn) and must not move the line."""
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
            sandbox_resources=Resources(mem_limit_mb=256, cpu_seconds=30, pids_max=64),
        ),
    )
    await hostlet.start()
    try:
        agent = AgentDefinition(id=new_agent_id(), name="bench-cold-limited")
        await store.create_agent(agent)
        version = AgentVersion(id=new_version_id(), agent_id=agent.id, version="1.0.0",
                               harness="echo", image_ref="echo")
        await store.create_version(version)

        durations: list[float] = []
        for _ in range(5):
            session = AgentSession(id=new_session_id(), agent_id=agent.id, agent_version_id=version.id)
            await store.create_session(session)
            start = time.perf_counter()
            sandbox = await hostlet.ensure(session, version)
            durations.append(time.perf_counter() - start)
            assert sandbox.status.value == "active"
            await hostlet.destroy(sandbox.id)

        p50 = sorted(durations)[len(durations) // 2]
        print(f"\ncold start with Resources(mem=256mb,cpu=30s,pids=64): "
              f"p50={p50 * 1000:.0f}ms min={min(durations) * 1000:.0f}ms max={max(durations) * 1000:.0f}ms")
        assert p50 <= 0.250, f"cold start with limits p50 {p50 * 1000:.0f}ms exceeds 250ms"
    finally:
        await hostlet.aclose()
