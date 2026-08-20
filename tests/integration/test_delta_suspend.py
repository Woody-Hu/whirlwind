"""Delta suspend/resume through the real hostlet (ADR-0012 D4).

Real processes (echo harness sandboxes), real filesystem snapshots, real
store records. Verifies the lineage policy: first snapshot of a session is
full, subsequent suspends chain deltas, chain_max compaction flips back to
full, every restore reproduces the exact pre-suspend workspace (merkle), and
the default full mode is byte-identical to the pre-ADR behavior.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from whirlwind.bus import InProcessEventBus
from whirlwind.core import (
    AgentDefinition,
    AgentSession,
    AgentVersion,
    SandboxStatus,
    new_agent_id,
    new_session_id,
    new_version_id,
)
from whirlwind.drivers import ProcessDriver
from whirlwind.drivers.process import _merkle_root
from whirlwind.harness.adapter import default_registry
from whirlwind.hostlet import Hostlet, HostletConfig
from whirlwind.imaging import LocalRegistry, echo_image_build
from whirlwind.seam.model import SeamRenderer
from whirlwind.storage.memory import MemoryMetadataStore
from whirlwind.storage.wal_eventlog import WALEventLog

REPO_ROOT = Path(__file__).resolve().parents[2]

_PLAN_DIR = ".whirlwind"  # platform-owned per-sandbox plan files (see below)


def _state_merkle(ws: Path) -> str:
    """Merkle over user/harness state, excluding the platform plan dir.

    `.whirlwind/` holds per-sandbox plan files (manifest.json with the fresh
    `llm_relay_url` port, runtime.json with ECHO_LLM_URL etc.). The hostlet
    DELIBERATELY overwrites them after snapshot seeding — the plan (ports,
    sandbox ids) is per-sandbox and must win over the snapshot's stale copies
    (see hostlet.ensure). They can never be bit-for-bit across restores; every
    other byte of the workspace can, and must.
    """
    scratch = ws.parent / f".{ws.name}-merkle-view"
    if scratch.exists():
        shutil.rmtree(scratch)
    shutil.copytree(ws, scratch, symlinks=True, ignore=shutil.ignore_patterns(_PLAN_DIR))
    try:
        return _merkle_root(scratch)[0]
    finally:
        shutil.rmtree(scratch)


@pytest.fixture(scope="module")
def echo_registry(tmp_path_factory: pytest.TempPathFactory) -> LocalRegistry:
    registry = LocalRegistry(tmp_path_factory.mktemp("images"))

    async def _build() -> None:
        await registry.register(echo_image_build(REPO_ROOT))

    asyncio.new_event_loop().run_until_complete(_build())
    return registry


async def _make_hostlet(tmp_path: Path, echo_registry: LocalRegistry, **config_overrides):
    store = MemoryMetadataStore()
    bus = InProcessEventBus()
    hostlet = Hostlet(
        driver=ProcessDriver(snapshots_root=tmp_path / "snapshots"),
        images=echo_registry,
        adapters=default_registry(),
        renderer=SeamRenderer(),
        store=store,
        event_log=WALEventLog(tmp_path / "events"),
        bus=bus,
        config=HostletConfig(data_dir=tmp_path, **config_overrides),
    )
    await hostlet.start()
    agent = AgentDefinition(id=new_agent_id(), name="delta-agent")
    await store.create_agent(agent)
    version = AgentVersion(
        id=new_version_id(),
        agent_id=agent.id,
        version="1.0.0",
        harness="echo",
        image_ref="echo",
    )
    await store.create_version(version)
    session = AgentSession(id=new_session_id(), agent_id=agent.id, agent_version_id=version.id)
    await store.create_session(session)
    return hostlet, store, session, version


@pytest.mark.asyncio
async def test_delta_lineage_chains_and_compacts(
    tmp_path: Path, echo_registry: LocalRegistry
) -> None:
    """snapshot_mode=delta + chain_max=2: full → delta → delta → full (compaction).
    Every cycle's restore must reproduce the pre-suspend tree bit-for-bit."""
    hostlet, store, session, version = await _make_hostlet(
        tmp_path, echo_registry, snapshot_mode="delta", snapshot_chain_max=2
    )
    try:
        sandbox = await hostlet.ensure(session, version)
        (Path(sandbox.workspace) / "seed-a.txt").write_text("A" * 4096)
        (Path(sandbox.workspace) / "seed-b.txt").write_text("B" * 4096)
        expected_contents: list[str] = []

        for cycle in range(4):
            ws = Path(sandbox.workspace)
            log = ws / "log.jsonl"
            log.write_text(log.read_text() + f'{{"cycle":{cycle}}}\n' if log.exists() else f'{{"cycle":{cycle}}}\n')
            expected_contents.append(log.read_text())
            pre_merkle = _merkle_root(ws)[0]  # full tree (incl. plan) — snapshot identity
            pre_state_merkle = _state_merkle(ws)  # user/harness state only

            snapshot = await hostlet.suspend(sandbox.id)
            if cycle == 0:
                # first snapshot of the lineage: full, chain depth 0
                assert snapshot.manifest["delta"] is False
                assert snapshot.manifest["chain_depth"] == 0
            elif cycle in (1, 2):
                # chained delta against the previous snapshot
                assert snapshot.manifest["delta"] is True, f"cycle {cycle}"
                assert snapshot.manifest["chain_depth"] == cycle
                assert snapshot.manifest["base"]["merkle"]
            else:
                # chain_max=2 reached: compaction flips back to full
                assert snapshot.manifest["delta"] is False, "compaction cycle"
                assert snapshot.manifest["chain_depth"] == 0
            assert snapshot.merkle == pre_merkle  # end-state identity holds either way

            sandbox = await hostlet.restore(session, version)
            assert sandbox.status == SandboxStatus.ACTIVE
            ws2 = Path(sandbox.workspace)
            assert (ws2 / "log.jsonl").read_text() == expected_contents[-1]
            assert _state_merkle(ws2) == pre_state_merkle  # bit-for-bit state restore
            assert (ws2 / "seed-a.txt").read_text() == "A" * 4096

        await hostlet.destroy(sandbox.id)
    finally:
        await hostlet.aclose()


@pytest.mark.asyncio
async def test_full_mode_is_the_unchanged_default(
    tmp_path: Path, echo_registry: LocalRegistry
) -> None:
    """Default config keeps every snapshot a full copy (regression pin)."""
    hostlet, store, session, version = await _make_hostlet(tmp_path, echo_registry)
    try:
        sandbox = await hostlet.ensure(session, version)
        (Path(sandbox.workspace) / "state.txt").write_text("v1")
        first = await hostlet.suspend(sandbox.id)
        sandbox = await hostlet.restore(session, version)
        (Path(sandbox.workspace) / "state.txt").write_text("v2")
        second = await hostlet.suspend(sandbox.id)
        for snapshot in (first, second):
            assert snapshot.manifest.get("delta", False) is False
            assert snapshot.manifest.get("chain_depth", 0) == 0
            assert "base" not in snapshot.manifest
        sandbox = await hostlet.restore(session, version)
        assert (Path(sandbox.workspace) / "state.txt").read_text() == "v2"
        await hostlet.destroy(sandbox.id)
    finally:
        await hostlet.aclose()
