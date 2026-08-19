"""Warm-claim benchmark: the CAS bind decision against real prewarmed sandboxes.

ADR §7 acceptance: warm claim p50 ≤ 2ms. Each round claims a distinct real
booted sandbox (scan + KV CAS + bind); nothing is mocked. Timing is manual
(the path is async; pytest-benchmark's sync fixture can't drive this loop).
"""

from __future__ import annotations

import asyncio
import statistics
import time
from pathlib import Path

import pytest

from whirlwind.bus import InProcessEventBus
from whirlwind.control import WarmPool, WarmPoolConfig
from whirlwind.core import AgentDefinition, AgentSession, AgentVersion, SandboxStatus, new_agent_id, new_session_id, new_version_id
from whirlwind.drivers import ProcessDriver
from whirlwind.harness.adapter import default_registry
from whirlwind.hostlet import Hostlet, HostletConfig
from whirlwind.imaging import LocalRegistry, echo_image_build
from whirlwind.seam.model import SeamRenderer
from whirlwind.storage.wal_eventlog import WALEventLog
from whirlwind.storage.memory import MemoryKVStore, MemoryMetadataStore

REPO_ROOT = Path(__file__).resolve().parents[2]
WARM_COUNT = 6
CLAIM_P50_LIMIT_S = 0.002


@pytest.fixture(scope="module")
def echo_registry(tmp_path_factory: pytest.TempPathFactory) -> LocalRegistry:
    registry = LocalRegistry(tmp_path_factory.mktemp("images"))

    async def _build() -> None:
        await registry.register(echo_image_build(REPO_ROOT))

    asyncio.new_event_loop().run_until_complete(_build())
    return registry


@pytest.mark.asyncio
async def test_warm_claim_p50(
    tmp_path: Path,
    echo_registry: LocalRegistry,
) -> None:
    """No turn is ever sent, so no LLM traffic: the relay upstream is unused."""
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
            api_key_env="WHIRLWIND_TEST_KEY",
            llm_upstream="http://127.0.0.1:9",  # never reached: no turns are sent
        ),
    )
    await hostlet.start()
    kv = MemoryKVStore()
    pool = WarmPool(store, hostlet, kv, WarmPoolConfig(maintain_interval_s=3600))
    try:
        agent = AgentDefinition(id=new_agent_id(), name="bench-pool")
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

        # real prewarm: booted SandboxAgent + harness subprocesses
        for _ in range(WARM_COUNT):
            await hostlet.ensure(None, version)
        warm = [s for s in await store.list_sandboxes() if s.status == SandboxStatus.WARM]
        assert len(warm) == WARM_COUNT

        durations: list[float] = []
        for i in range(WARM_COUNT):
            session = AgentSession(id=new_session_id(), agent_id=agent.id, agent_version_id=version.id)
            await store.create_session(session)
            start = time.perf_counter()
            claimed = await pool.claim(version.id, session)
            durations.append(time.perf_counter() - start)
            assert claimed is not None

        p50 = statistics.median(durations)
        print(
            f"\nwarm claim over {WARM_COUNT} real sandboxes: "
            f"p50={p50 * 1000:.3f}ms min={min(durations) * 1000:.3f}ms max={max(durations) * 1000:.3f}ms"
        )
        assert p50 <= CLAIM_P50_LIMIT_S, f"warm claim p50 {p50 * 1000:.3f}ms exceeds {CLAIM_P50_LIMIT_S * 1000:.0f}ms"
    finally:
        for sandbox in await store.list_sandboxes():
            if sandbox.status in (SandboxStatus.WARM, SandboxStatus.ACTIVE):
                try:
                    await hostlet.destroy(sandbox.id)
                except Exception:
                    pass
        await hostlet.aclose()
