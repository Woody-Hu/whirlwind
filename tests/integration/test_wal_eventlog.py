"""WALEventLog durability tests: group commit, crash recovery, error surfacing.

Uses real files and real fsync (monkeypatched only to *count* fsync calls in
the group-commit test). Crash recovery is simulated by writing torn records
to the WAL file directly, as a crash mid-write would leave them.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from argus.storage import WALEventLog


async def test_group_commit_bursts_into_few_fsyncs(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = WALEventLog(tmp_path / "events")
    calls = 0
    real_fsync = os.fsync

    def counting_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", counting_fsync)
    try:
        events = await asyncio.gather(
            *[log.append("ses_1", "turn/start", {"i": i}) for i in range(100)]
        )
        assert [e.seq for e in events] == list(range(1, 101))
        # one burst shares a handful of fsyncs, not one per record
        assert calls <= 5
    finally:
        log.close()


async def test_append_is_durable_immediately(tmp_path) -> None:
    log = WALEventLog(tmp_path / "events")
    try:
        event = await log.append("ses_1", "assistant/chunk", {"delta": "durable"})
        # once append() returns, the record must be on stable storage:
        # a fresh read of the file (no log in memory) sees it
        raw = (tmp_path / "events" / "ses_1.jsonl").read_text(encoding="utf-8")
        assert json.loads(raw)["seq"] == event.seq
        assert json.loads(raw)["type"] == "assistant/chunk"
    finally:
        log.close()


async def test_crash_recovery_truncates_torn_tail(tmp_path) -> None:
    log = WALEventLog(tmp_path / "events")
    await log.append("ses_1", "turn/start", {"n": 1})
    await log.append("ses_1", "turn/start", {"n": 2})
    log.close()

    # simulate a crash mid-write: a record torn after the last newline
    path = tmp_path / "events" / "ses_1.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"session_id": "ses_1", "seq": 3, "type": "turn/start", "data": {"n":')

    log2 = WALEventLog(tmp_path / "events")
    try:
        assert await log2.last_seq("ses_1") == 2
        assert [e.seq for e in await log2.read("ses_1")] == [1, 2]
        e = await log2.append("ses_1", "turn/start", {"n": 3})
        assert e.seq == 3
    finally:
        log2.close()

    # the torn bytes were actually truncated away, not left to confuse readers
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 3
    assert json.loads(lines[-1])["data"] == {"n": 3}


async def test_crash_recovery_drops_unterminated_complete_record(tmp_path) -> None:
    log = WALEventLog(tmp_path / "events")
    await log.append("ses_1", "turn/start", {"n": 1})
    log.close()

    # a complete JSON line whose trailing newline never hit disk is not
    # durable either: recovery must drop it
    path = tmp_path / "events" / "ses_1.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"session_id": "ses_1", "seq": 2, "type": "turn/start", "data": {"n": 2}}')

    log2 = WALEventLog(tmp_path / "events")
    try:
        assert await log2.last_seq("ses_1") == 1
        assert [e.seq for e in await log2.read("ses_1")] == [1]
        assert (await log2.append("ses_1", "turn/start")).seq == 2
    finally:
        log2.close()


async def test_read_skips_torn_line_without_open(tmp_path) -> None:
    path = tmp_path / "events" / "ses_1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"session_id": "ses_1", "seq": 1, "type": "turn/start", "data": {}}\n'
        '{"session_id": "ses_1", "seq": 2, "type": "assistant/chunk", "data": {"d":',  # torn
        encoding="utf-8",
    )
    log = WALEventLog(tmp_path / "events")
    try:
        assert [e.seq for e in await log.read("ses_1")] == [1]
    finally:
        log.close()


async def test_per_session_seq_independent(tmp_path) -> None:
    log = WALEventLog(tmp_path / "events")
    try:
        a1 = await log.append("ses_a", "turn/start")
        b1 = await log.append("ses_b", "turn/start")
        a2 = await log.append("ses_a", "turn/end")
        assert (a1.seq, b1.seq, a2.seq) == (1, 1, 2)
        assert await log.last_seq("ses_a") == 2
        assert await log.last_seq("ses_b") == 1
    finally:
        log.close()


async def test_fsync_failure_surfaces_to_appender(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = WALEventLog(tmp_path / "events")
    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("disk gone")))
    try:
        with pytest.raises(OSError):
            await log.append("ses_1", "turn/start")
    finally:
        log.close()
