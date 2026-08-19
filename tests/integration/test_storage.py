"""Storage provider integration tests against real in-process state and real files."""

import asyncio

import pytest

from whirlwind.core import AgentDefinition, AgentSession, AgentVersion, CronJob, SkillRef, new_session_id
from whirlwind.core.errors import Conflict, NotFound
from whirlwind.storage import WALEventLog, LocalObjectStore, MemoryKVStore, MemoryLocks, MemoryMetadataStore


async def test_metadata_agent_crud_and_name_uniqueness(tmp_path):
    store = MemoryMetadataStore(skills_dir=tmp_path / "skills")
    agent = AgentDefinition(id="agt_1", name="helper")
    await store.create_agent(agent)
    with pytest.raises(Conflict):
        await store.create_agent(AgentDefinition(id="agt_2", name="helper"))
    assert (await store.get_agent_by_name("helper")).id == "agt_1"
    agent.default_version_id = "ver_9"
    await store.update_agent(agent)
    assert (await store.get_agent("agt_1")).default_version_id == "ver_9"
    with pytest.raises(NotFound):
        await store.update_agent(AgentDefinition(id="missing", name="x"))


async def test_metadata_versions_sessions_crons(tmp_path):
    store = MemoryMetadataStore(skills_dir=tmp_path / "skills")
    await store.create_agent(AgentDefinition(id="agt_1", name="helper"))
    v1 = AgentVersion(id="ver_1", agent_id="agt_1", version="1", harness="echo", image_ref="echo@1")
    await store.create_version(v1)
    assert [v.id for v in await store.list_versions("agt_1")] == ["ver_1"]

    ses = AgentSession(id=new_session_id(), agent_id="agt_1", agent_version_id="ver_1")
    await store.create_session(ses)
    assert (await store.get_session(ses.id)).status.value == "created"

    cron = CronJob(agent_id="agt_1", schedule="* * * * *", input_template="hi")
    saved = await store.save_cron(cron)
    assert saved.id
    assert len(await store.list_crons("agt_1")) == 1
    await store.delete_cron(saved.id)
    assert await store.list_crons("agt_1") == []


async def test_skill_persistence_roundtrip(tmp_path):
    store = MemoryMetadataStore(skills_dir=tmp_path / "skills")
    src = tmp_path / "pkg"
    (src / "sub").mkdir(parents=True)
    (src / "SKILL.md").write_text("# demo skill")
    (src / "sub" / "run.sh").write_text("echo run")
    ref = await store.save_skill("demo", "1.0.0", src)
    path = await store.skill_path(ref)
    assert path is not None
    assert (path / "SKILL.md").read_text() == "# demo skill"
    assert (path / "sub" / "run.sh").exists()
    assert await store.skill_path(SkillRef(name="demo", version="9.9")) is None


async def test_kv_cas_atomicity_under_concurrency():
    kv = MemoryKVStore()
    await kv.put("route/ses_1", "sbx_a")
    winners = 0
    losers = 0

    async def try_cas(expected: str, new: str) -> bool:
        return await kv.cas("route/ses_1", expected, new)

    results = await asyncio.gather(
        try_cas("sbx_a", "sbx_b"),
        try_cas("sbx_a", "sbx_c"),
    )
    # exactly one CAS from sbx_a succeeds
    assert results.count(True) == 1
    winners += results.count(True)
    losers += results.count(False)
    assert await kv.get("route/ses_1") in ("sbx_b", "sbx_c")
    assert winners == 1 and losers == 1


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
