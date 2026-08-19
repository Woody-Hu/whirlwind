"""Storage provider benchmarks (ADR-0004 D6): memory vs PostgreSQL vs Redis.

pytest-benchmark against real localhost services — skip when absent, same
"no fakes" policy as the integration suite. Recorded numbers (ADR-0004 §9)
are regression baselines: acceptance is "same order of magnitude as the
first recorded baseline on the same host class", never a fabricated line.

Each backend runs on its own event loop driven synchronously; the wrapper
cost is identical for every backend, so cross-backend comparison is fair.
"""

from __future__ import annotations

import asyncio
import itertools
import os
from pathlib import Path
from typing import Any, Callable

import pytest

from whirlwind.core import AgentDefinition, AgentSession, AgentVersion, SessionStatus, new_session_id
from whirlwind.storage.memory import MemoryKVStore, MemoryMetadataStore

POSTGRES_DSN = os.environ.get(
    "WHIRLWIND_TEST_POSTGRES_DSN", "postgresql://whirlwind:whirlwind@127.0.0.1:5432/whirlwind_test"
)
REDIS_URL = os.environ.get("WHIRLWIND_TEST_REDIS_URL", "redis://127.0.0.1:6379/15")
_TRUNCATE = "TRUNCATE agents, agent_versions, sessions, sandboxes, snapshots, crons RESTART IDENTITY"


class BenchStore:
    """A store started on its own loop; falsy when the backend is down."""

    def __init__(self, make: Callable[[], Any]) -> None:
        self.store = make()
        self.loop = asyncio.new_event_loop()
        try:
            self.loop.run_until_complete(asyncio.wait_for(self.store.start(), timeout=5.0))
        except Exception:
            self.store = None
            self.loop.close()
            self.loop = None  # type: ignore[assignment]

    def __bool__(self) -> bool:
        return self.store is not None

    def run(self, coro):
        return self.loop.run_until_complete(coro)

    def close(self) -> None:
        if self.store is not None:
            self.loop.run_until_complete(self.store.aclose())
        self.loop.close()


# --------------------------------------------------- MetadataStore: create+get


def _bench_agent_create_get(benchmark, b: BenchStore) -> None:
    n = itertools.count()

    def op() -> Any:
        i = next(n)
        agent = AgentDefinition(id=f"agt_{i}", name=f"bench-{i}")
        b.run(b.store.create_agent(agent))
        return b.run(b.store.get_agent(agent.id))

    benchmark(op)


def test_agent_create_get_memory(benchmark, tmp_path: Path) -> None:
    b = BenchStore(lambda: MemoryMetadataStore(skills_dir=tmp_path / "skills"))
    try:
        _bench_agent_create_get(benchmark, b)
    finally:
        b.close()


def test_agent_create_get_postgres(benchmark) -> None:
    from whirlwind.storage.postgres import PostgresMetadataStore

    b = BenchStore(lambda: PostgresMetadataStore(POSTGRES_DSN))
    if not b:
        pytest.skip(f"postgres not reachable at {POSTGRES_DSN!r}")
    try:
        b.run(b.store.pool.execute(_TRUNCATE))
        _bench_agent_create_get(benchmark, b)
    finally:
        b.close()


# ----------------------------------------------------- MetadataStore: session


def _bench_session_update(benchmark, b: BenchStore) -> None:
    agent = AgentDefinition(id="agt_bench", name="bench-session")
    version = AgentVersion(id="ver_bench", agent_id="agt_bench", version="1", harness="echo", image_ref="echo@1")
    session = AgentSession(id=new_session_id(), agent_id="agt_bench", agent_version_id="ver_bench")
    b.run(b.store.create_agent(agent))
    b.run(b.store.create_version(version))
    b.run(b.store.create_session(session))

    def op() -> Any:
        session.status = SessionStatus.IDLE if session.status == SessionStatus.RUNNING else SessionStatus.RUNNING
        return b.run(b.store.update_session(session))

    benchmark(op)


def test_session_update_memory(benchmark, tmp_path: Path) -> None:
    b = BenchStore(lambda: MemoryMetadataStore(skills_dir=tmp_path / "skills"))
    try:
        _bench_session_update(benchmark, b)
    finally:
        b.close()


def test_session_update_postgres(benchmark) -> None:
    from whirlwind.storage.postgres import PostgresMetadataStore

    b = BenchStore(lambda: PostgresMetadataStore(POSTGRES_DSN))
    if not b:
        pytest.skip(f"postgres not reachable at {POSTGRES_DSN!r}")
    try:
        b.run(b.store.pool.execute(_TRUNCATE))
        _bench_session_update(benchmark, b)
    finally:
        b.close()


# ------------------------------------------------------------- KV: put + get


def _bench_kv_put_get(benchmark, b: BenchStore) -> None:
    n = itertools.count()

    def op() -> Any:
        key = f"bench/{next(n)}"
        b.run(b.store.put(key, "sbx_bench"))
        return b.run(b.store.get(key))

    benchmark(op)


def test_kv_put_get_memory(benchmark) -> None:
    b = BenchStore(lambda: MemoryKVStore())
    try:
        _bench_kv_put_get(benchmark, b)
    finally:
        b.close()


def test_kv_put_get_redis(benchmark) -> None:
    from whirlwind.storage.redis import RedisKVStore

    b = BenchStore(lambda: RedisKVStore(REDIS_URL))
    if not b:
        pytest.skip(f"redis not reachable at {REDIS_URL!r}")
    try:
        _bench_kv_put_get(benchmark, b)
    finally:
        b.close()


# ------------------------------------------------------------------ KV: CAS


def _bench_kv_cas(benchmark, b: BenchStore) -> None:
    b.run(b.store.put("bench/cas", "0"))
    state = "0"

    def op() -> bool:
        nonlocal state
        new = "1" if state == "0" else "0"
        ok = b.run(b.store.cas("bench/cas", state, new))
        state = new
        return ok

    benchmark(op)


def test_kv_cas_memory(benchmark) -> None:
    b = BenchStore(lambda: MemoryKVStore())
    try:
        _bench_kv_cas(benchmark, b)
    finally:
        b.close()


def test_kv_cas_redis(benchmark) -> None:
    from whirlwind.storage.redis import RedisKVStore

    b = BenchStore(lambda: RedisKVStore(REDIS_URL))
    if not b:
        pytest.skip(f"redis not reachable at {REDIS_URL!r}")
    try:
        _bench_kv_cas(benchmark, b)
    finally:
        b.close()
