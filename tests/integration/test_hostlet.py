"""Hostlet + SandboxAgent integration: the full M1 data path, all real.

Chain under test (no mocks anywhere):
    Hostlet.ensure -> ProcessDriver sandbox -> real SandboxAgent subprocess
    (uvicorn HTTP) -> real echo harness subprocess (image venv interpreter)
    -> notifications -> agent EventTap -> HTTP ingest -> EventLog + EventBus
    LLM relay: harness -> agent /relay/llm -> hostlet /secret/llm (+key) ->
    real local upstream HTTP service that demands the Authorization header.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from whirlwind.bus import InProcessEventBus
from whirlwind.core import (
    AgentDefinition,
    AgentSession,
    AgentVersion,
    SandboxStatus,
    SeamBindingDecl,
    SkillRef,
    new_agent_id,
    new_session_id,
    new_version_id,
)
from whirlwind.drivers import ProcessDriver
from whirlwind.harness.adapter import default_registry
from whirlwind.hostlet import Hostlet, HostletConfig
from whirlwind.imaging import LocalRegistry, echo_image_build
from whirlwind.seam.model import SeamRenderer
from whirlwind.storage.wal_eventlog import WALEventLog
from whirlwind.storage.memory import MemoryMetadataStore
from tests.integration.conftest import API_KEY_SENTINEL

# llm_upstream fixture: shared, see tests/integration/conftest.py

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def echo_registry(tmp_path_factory: pytest.TempPathFactory) -> LocalRegistry:
    registry = LocalRegistry(tmp_path_factory.mktemp("images"))

    async def _build() -> None:
        await registry.register(echo_image_build(REPO_ROOT))

    asyncio.new_event_loop().run_until_complete(_build())
    return registry


async def _collect_until_turn_end(stream, timeout_s: float = 15.0) -> list[dict]:
    events: list[dict] = []
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        payload = await stream.next(timeout=1.0)
        if payload is None:
            break
        events.append(payload)
        if payload.get("type") == "turn/end":
            break
    return events


@pytest.mark.asyncio
async def test_ensure_turn_events_relay_destroy(
    tmp_path: Path,
    echo_registry: LocalRegistry,
    llm_upstream: str,
) -> None:
    store = MemoryMetadataStore()
    event_log = WALEventLog(tmp_path / "events")
    bus = InProcessEventBus()
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
            api_key_env="WHIRLWIND_TEST_KEY",
            llm_upstream=llm_upstream,
        ),
    )
    await hostlet.start()
    try:
        agent = AgentDefinition(id=new_agent_id(), name="greeter")
        await store.create_agent(agent)
        version = AgentVersion(
            id=new_version_id(),
            agent_id=agent.id,
            version="1.0.0",
            harness="echo",
            image_ref="echo",
            seam_bindings=[SeamBindingDecl(seam="shell.v1", provider="sandbox-bash")],
            model_config_decl={"provider": "deepseek-official", "model": "deepseek-chat"},
        )
        await store.create_version(version)
        session = AgentSession(id=new_session_id(), agent_id=agent.id, agent_version_id=version.id)
        await store.create_session(session)

        sandbox = await hostlet.ensure(session, version)

        # -- sandbox active + bound; workspace carries the plan, never the key
        assert sandbox.status == SandboxStatus.ACTIVE
        assert session.bound_sandbox_id == sandbox.id
        runtime_json = (Path(sandbox.workspace) / ".whirlwind" / "runtime.json").read_text()
        assert API_KEY_SENTINEL not in runtime_json
        assert "/relay/llm" in runtime_json  # harness egress points at the agent relay
        for file in Path(sandbox.workspace).rglob("*"):
            if file.is_file():
                assert API_KEY_SENTINEL not in file.read_text(errors="ignore"), f"leak in {file}"

        # -- a full turn: events stream through ingest into bus + durable log
        stream = await bus.subscribe(f"sessions.{session.id}.stream")
        message_id = await hostlet.turn(sandbox.id, "hello relay")
        assert message_id
        events = await _collect_until_turn_end(stream)
        types = [e["type"] for e in events]
        assert types[0] == "turn/start"
        assert "assistant/message" in types
        assert types[-1] == "turn/end"
        # the reply traversed the real relay chain (upstream answered "relayed-reply")
        final_text = [e for e in events if e["type"] == "assistant/message"][-1]["data"]["message"]["content"][0]["text"]
        assert final_text == "relayed-reply"

        logged = await event_log.read(session.id)
        assert [e.type for e in logged] == types
        seqs = [e.seq for e in logged]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)

        # -- teardown
        await hostlet.destroy(sandbox.id)
        record = await store.get_sandbox(sandbox.id)
        assert record is not None and record.status == SandboxStatus.TERMINATED
        with pytest.raises(Exception):
            await hostlet.turn(sandbox.id, "gone")
    finally:
        await hostlet.aclose()


@pytest.mark.asyncio
async def test_skills_are_staged_into_workspace(
    tmp_path: Path,
    echo_registry: LocalRegistry,
) -> None:
    skills_root = tmp_path / "skills"
    skill_src = tmp_path / "src-skill" / "pdf-tools"
    skill_src.mkdir(parents=True)
    (skill_src / "SKILL.md").write_text("---\nname: pdf-tools\ndescription: PDF helpers\n---\n# PDF tools\n")
    store = MemoryMetadataStore(skills_dir=skills_root)
    bus = InProcessEventBus()
    hostlet = Hostlet(
        driver=ProcessDriver(),
        images=echo_registry,
        adapters=default_registry(),
        renderer=SeamRenderer(),
        store=store,
        event_log=WALEventLog(tmp_path / "events"),
        bus=bus,
        config=HostletConfig(data_dir=tmp_path / "data"),
    )
    await hostlet.start()
    try:
        agent = AgentDefinition(id=new_agent_id(), name="skilled")
        await store.create_agent(agent)
        version = AgentVersion(
            id=new_version_id(),
            agent_id=agent.id,
            version="1.0.0",
            harness="echo",
            image_ref="echo",
            skill_refs=[SkillRef(name="pdf-tools", version="1.2.0")],
        )
        await store.save_skill("pdf-tools", "1.2.0", skill_src)
        await store.create_version(version)
        session = AgentSession(id=new_session_id(), agent_id=agent.id, agent_version_id=version.id)
        await store.create_session(session)

        sandbox = await hostlet.ensure(session, version)
        try:
            staged = Path(sandbox.workspace) / ".whirlwind" / "skills" / "pdf-tools" / "SKILL.md"
            assert staged.is_file()
            assert "PDF tools" in staged.read_text()
        finally:
            await hostlet.destroy(sandbox.id)
    finally:
        await hostlet.aclose()


@pytest.mark.asyncio
async def test_suspend_snapshot_then_restore_workspace_and_turn(
    tmp_path: Path,
    echo_registry: LocalRegistry,
    llm_upstream: str,
) -> None:
    """M2 suspend/restore: data snapshot -> dead process -> fresh sandbox from
    the snapshot, workspace contents intact, turns flowing again through the
    relay chain."""
    store = MemoryMetadataStore()
    event_log = WALEventLog(tmp_path / "events")
    bus = InProcessEventBus()
    hostlet = Hostlet(
        driver=ProcessDriver(snapshots_root=tmp_path / "snapshots"),
        images=echo_registry,
        adapters=default_registry(),
        renderer=SeamRenderer(),
        store=store,
        event_log=event_log,
        bus=bus,
        config=HostletConfig(
            data_dir=tmp_path,
            api_key_env="WHIRLWIND_TEST_KEY",
            llm_upstream=llm_upstream,
        ),
    )
    await hostlet.start()
    try:
        agent = AgentDefinition(id=new_agent_id(), name="sleeper")
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

        first = await hostlet.ensure(session, version)
        old_workspace = Path(first.workspace)
        (old_workspace / "harness-state.jsonl").write_text('{"v":1}\n{"v":2}\n')  # harness-persisted state

        # -- suspend: snapshot taken, process gone, session unbound
        snapshot = await hostlet.suspend(first.id)
        assert snapshot.kind.value == "data"
        assert snapshot.merkle and snapshot.size > 0
        assert snapshot.manifest["session_id"] == session.id
        assert await store.latest_session_snapshot(session.id) is not None

        record = await store.get_sandbox(first.id)
        assert record is not None and record.status == SandboxStatus.SUSPENDED
        assert record.bound_session_id is None
        session = await store.get_session(session.id)
        assert session is not None and session.bound_sandbox_id is None
        assert not old_workspace.exists()  # data lives in the snapshot, not the dead workspace
        with pytest.raises(Exception):
            await hostlet.turn(first.id, "gone")

        # -- restore: fresh sandbox from the snapshot, same session, state intact
        second = await hostlet.restore(session, version)
        assert second.id != first.id
        assert second.status == SandboxStatus.ACTIVE
        session = await store.get_session(session.id)
        assert session is not None and session.bound_sandbox_id == second.id
        assert (Path(second.workspace) / "harness-state.jsonl").read_text() == '{"v":1}\n{"v":2}\n'
        old = await store.get_sandbox(first.id)
        assert old is not None and old.status == SandboxStatus.TERMINATED

        # -- the restored sandbox answers turns through the real relay chain
        stream = await bus.subscribe(f"sessions.{session.id}.stream")
        await hostlet.turn(second.id, "after restore")
        events = await _collect_until_turn_end(stream)
        assert events[-1]["type"] == "turn/end"
        final_text = [e for e in events if e["type"] == "assistant/message"][-1]["data"]["message"]["content"][0]["text"]
        assert final_text == "relayed-reply"

        await hostlet.destroy(second.id)
    finally:
        await hostlet.aclose()
