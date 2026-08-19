"""Storage provider contract tests (ADR-0004 D5).

The MetadataStore suite is parameterized over every backend (memory always;
PostgreSQL against a real service, skipped when absent) — parity between
implementations is enforced by running the same assertions, never assumed.
KV / locks / object store / event log still run against their in-process
implementations; Redis joins the KV contract suite in its own change.
"""

import asyncio

import pytest

from whirlwind.core import (
    AgentDefinition,
    AgentSession,
    AgentVersion,
    CronJob,
    Sandbox,
    SandboxStatus,
    SkillRef,
    Snapshot,
    SnapshotKind,
    new_session_id,
)
from whirlwind.core.errors import Conflict, NotFound
from whirlwind.storage import WALEventLog, LocalObjectStore, MemoryKVStore, MemoryLocks, MemoryMetadataStore


# ------------------------------------------------- MetadataStore contract

async def test_metadata_agent_crud_and_name_uniqueness(metadata_store):
    agent = AgentDefinition(id="agt_1", name="helper")
    await metadata_store.create_agent(agent)
    with pytest.raises(Conflict):
        await metadata_store.create_agent(AgentDefinition(id="agt_2", name="helper"))
    assert (await metadata_store.get_agent_by_name("helper")).id == "agt_1"
    agent.default_version_id = "ver_9"
    await metadata_store.update_agent(agent)
    assert (await metadata_store.get_agent("agt_1")).default_version_id == "ver_9"
    with pytest.raises(NotFound):
        await metadata_store.update_agent(AgentDefinition(id="missing", name="x"))


async def test_metadata_versions_sessions_crons(metadata_store):
    await metadata_store.create_agent(AgentDefinition(id="agt_1", name="helper"))
    v1 = AgentVersion(id="ver_1", agent_id="agt_1", version="1", harness="echo", image_ref="echo@1")
    await metadata_store.create_version(v1)
    assert [v.id for v in await metadata_store.list_versions("agt_1")] == ["ver_1"]

    ses = AgentSession(id=new_session_id(), agent_id="agt_1", agent_version_id="ver_1")
    await metadata_store.create_session(ses)
    assert (await metadata_store.get_session(ses.id)).status.value == "created"

    cron = CronJob(agent_id="agt_1", schedule="* * * * *", input_template="hi")
    saved = await metadata_store.save_cron(cron)
    assert saved.id
    assert len(await metadata_store.list_crons("agt_1")) == 1
    await metadata_store.delete_cron(saved.id)
    assert await metadata_store.list_crons("agt_1") == []


async def test_metadata_sessions_insertion_order(metadata_store):
    """Cron REUSE picks `live[-1]`; list order must be creation order on every backend."""
    await metadata_store.create_agent(AgentDefinition(id="agt_1", name="helper"))
    await metadata_store.create_version(
        AgentVersion(id="ver_1", agent_id="agt_1", version="1", harness="echo", image_ref="echo@1")
    )
    ids = []
    for i in range(3):
        ses = AgentSession(id=new_session_id(), agent_id="agt_1", agent_version_id="ver_1")
        await metadata_store.create_session(ses)
        ids.append(ses.id)
    assert [s.id for s in await metadata_store.list_sessions("agt_1")] == ids


async def test_metadata_snapshots_latest_wins(metadata_store):
    s1 = Snapshot(kind=SnapshotKind.DATA, subject="sbx_1", manifest={"session_id": "ses_1"}, location="/a")
    golden = Snapshot(kind=SnapshotKind.GOLDEN, subject="ver_1", manifest={}, location="/g")
    s2 = Snapshot(kind=SnapshotKind.FULL, subject="sbx_1", manifest={"session_id": "ses_1"}, location="/b")
    await metadata_store.save_snapshot(s1)
    await metadata_store.save_snapshot(golden)
    await metadata_store.save_snapshot(s2)
    latest = await metadata_store.latest_session_snapshot("ses_1")
    assert latest is not None and latest.location == "/b"  # last full/data wins
    # subject is keyed by manifest.session_id, not the sandbox id; golden never surfaces
    assert await metadata_store.latest_session_snapshot("sbx_1") is None
    assert await metadata_store.latest_session_snapshot("ses_missing") is None


async def test_metadata_sandbox_upsert(metadata_store):
    sbx = Sandbox(id="sbx_1", pool_id="pool_1")
    await metadata_store.upsert_sandbox(sbx)
    sbx.status = SandboxStatus.WARM
    await metadata_store.upsert_sandbox(sbx)
    fetched = await metadata_store.get_sandbox("sbx_1")
    assert fetched is not None and fetched.status == SandboxStatus.WARM
    assert [s.id for s in await metadata_store.list_sandboxes()] == ["sbx_1"]
    assert await metadata_store.get_sandbox("sbx_missing") is None


async def test_skill_persistence_roundtrip(metadata_store, tmp_path):
    src = tmp_path / "pkg"
    (src / "sub").mkdir(parents=True)
    (src / "SKILL.md").write_text("# demo skill")
    (src / "sub" / "run.sh").write_text("echo run")
    ref = await metadata_store.save_skill("demo", "1.0.0", src)
    path = await metadata_store.skill_path(ref)
    assert path is not None
    assert (path / "SKILL.md").read_text() == "# demo skill"
    assert (path / "sub" / "run.sh").exists()
    assert await metadata_store.skill_path(SkillRef(name="demo", version="9.9")) is None


# --------------------------------------------- PostgreSQL-specific proof

async def test_postgres_metadata_survives_restart(postgres_dsn, tmp_path):
    """The headline production property: metadata outlives the process."""
    from whirlwind.storage.postgres import PostgresMetadataStore

    store = PostgresMetadataStore(postgres_dsn, skills_dir=tmp_path / "skills")
    await store.start()
    await store.pool.execute(
        "TRUNCATE agents, agent_versions, sessions, sandboxes, snapshots, crons RESTART IDENTITY"
    )
    agent = AgentDefinition(id="agt_keep", name="keeper")
    await store.create_agent(agent)
    await store.create_version(
        AgentVersion(id="ver_keep", agent_id="agt_keep", version="1", harness="echo", image_ref="echo@1")
    )
    ses = AgentSession(id=new_session_id(), agent_id="agt_keep", agent_version_id="ver_keep")
    await store.create_session(ses)
    cron = await store.save_cron(CronJob(agent_id="agt_keep", schedule="* * * * *", input_template="hi"))
    await store.save_snapshot(
        Snapshot(kind=SnapshotKind.DATA, subject="sbx_k", manifest={"session_id": ses.id}, location="/snap")
    )
    await store.aclose()

    # a "restarted process": fresh instance, same DSN
    store2 = PostgresMetadataStore(postgres_dsn, skills_dir=tmp_path / "skills")
    await store2.start()
    try:
        assert (await store2.get_agent("agt_keep")).name == "keeper"
        assert [v.id for v in await store2.list_versions("agt_keep")] == ["ver_keep"]
        assert (await store2.get_session(ses.id)).id == ses.id
        assert (await store2.list_crons("agt_keep"))[0].id == cron.id
        latest = await store2.latest_session_snapshot(ses.id)
        assert latest is not None and latest.location == "/snap"
    finally:
        await store2.aclose()


# --------------------------------- KV / locks (in-process, Redis joins later)

async def test_kv_cas_atomicity_under_concurrency():
    kv = MemoryKVStore()
    await kv.put("route/ses_1", "sbx_a")
    results = await asyncio.gather(
        kv.cas("route/ses_1", "sbx_a", "sbx_b"),
        kv.cas("route/ses_1", "sbx_a", "sbx_c"),
    )
    # exactly one CAS from sbx_a succeeds
    assert results.count(True) == 1
    assert await kv.get("route/ses_1") in ("sbx_b", "sbx_c")


async def test_kv_ttl_expiry():
    kv = MemoryKVStore()
    await kv.put("lease/sbx_1", "alive", ttl_s=0.05)
    assert await kv.get("lease/sbx_1") == "alive"
    await asyncio.sleep(0.08)
    assert await kv.get("lease/sbx_1") is None


async def test_locks_mutual_exclusion():
    locks = MemoryLocks()
    assert await locks.acquire("op", ttl_s=5) is True
    assert await locks.acquire("op", ttl_s=5) is False
    await locks.release("op")
    assert await locks.acquire("op", ttl_s=5) is True


# ------------------------------------- object store / event log (unchanged)

async def test_object_store_roundtrip_and_escape_guard(tmp_path):
    store = LocalObjectStore(tmp_path / "objects")
    blob = tmp_path / "blob.bin"
    blob.write_bytes(b"\x00\x01\x02whirlwind")
    uri = await store.put("snaps/snap_1/data.tar", blob)
    assert uri.startswith("local://")
    dest = tmp_path / "out" / "data.tar"
    await store.fetch("snaps/snap_1/data.tar", dest)
    assert dest.read_bytes() == b"\x00\x01\x02whirlwind"
    stat = await store.stat("snaps/snap_1/data.tar")
    assert stat is not None and stat["size"] == 12
    await store.delete("snaps/snap_1/data.tar")
    assert await store.stat("snaps/snap_1/data.tar") is None
    with pytest.raises(FileNotFoundError):
        await store.fetch("snaps/snap_1/data.tar", dest)
    with pytest.raises(ValueError):
        await store.put("../escape", blob)


async def test_event_log_seq_monotonic_and_replay(tmp_path):
    log = WALEventLog(tmp_path / "events")
    e1 = await log.append("ses_1", "turn/start", {"input": "hello"})
    e2 = await log.append("ses_1", "assistant/chunk", {"delta": "wo"})
    e3 = await log.append("ses_1", "turn/end", {"usage": {}})
    assert (e1.seq, e2.seq, e3.seq) == (1, 2, 3)
    assert await log.last_seq("ses_1") == 3

    # concurrent appends keep seq strictly unique
    events = await asyncio.gather(*[log.append("ses_1", "error", {"i": i}) for i in range(20)])
    seqs = sorted(e.seq for e in events)
    assert seqs == list(range(4, 24))

    # replay from a cursor
    tail = await log.read("ses_1", from_seq=3)
    assert [e.seq for e in tail] == list(range(4, 24))
    assert await log.read("ses_missing") == []

    # reopen: seq continues from disk (durability across instances)
    log.close()
    log2 = WALEventLog(tmp_path / "events")
    assert await log2.last_seq("ses_1") == 23
    e = await log2.append("ses_1", "turn/start")
    assert e.seq == 24
    log2.close()
