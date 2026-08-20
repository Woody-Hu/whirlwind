"""Delta snapshot mechanics (ADR-0012): pure driver-level behavior on the
real filesystem — diff/apply round-trips, chain integrity, honest refusals.

No mocks: trees are real directories under pytest's tmp_path; the process
driver is the real one. runsc refusal tests never touch the binary (the
capability check fires before any subprocess work).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from whirlwind.core import SnapshotKind
from whirlwind.drivers import (
    DriverError,
    ProcessDriver,
    RunscDriver,
    SandboxSpec,
    SnapshotArtifact,
    UnsupportedCapability,
)
from whirlwind.drivers.process import _DELTA_INDEX, _merkle_root


async def _boot(driver: ProcessDriver, workspace: Path, sandbox_id: str = "sbx"):
    # /bin/cat exists on every POSIX the repo supports; the driver only needs
    # argv[0] to be a real file to accept the launch (we destroy immediately
    # after checkpoints — the cat process sits idle on stdin).
    spec = SandboxSpec(
        sandbox_id=sandbox_id,
        argv=["/bin/cat"],
        bundle_root=Path("/"),
        workspace=workspace,
    )
    instance = await driver.create(spec)
    return instance


@pytest.mark.asyncio
async def test_delta_roundtrip_add_modify_delete_symlink_empty_dir(tmp_path: Path) -> None:
    driver = ProcessDriver(snapshots_root=tmp_path / "snaps")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("hello")
    (ws / "sub").mkdir()
    (ws / "sub" / "b.txt").write_text("world")
    await _boot(driver, ws)

    full = await driver.checkpoint("sbx", SnapshotKind.DATA)
    assert full.delta is False
    assert full.manifest["chain_depth"] == 0

    # mutate: modify, add, delete, symlink, empty dir
    (ws / "a.txt").write_text("hello v2")
    (ws / "new.txt").write_text("added")
    (ws / "sub" / "b.txt").unlink()
    (ws / "emptydir").mkdir()
    (ws / "link.lnk").symlink_to("a.txt")
    delta = await driver.checkpoint("sbx", SnapshotKind.DATA, base=full)
    assert delta.delta is True
    assert delta.manifest["chain_depth"] == 1
    assert delta.manifest["base"]["snapshot_id"] == full.snapshot_id
    assert delta.manifest["base"]["merkle"] == full.merkle

    dest = tmp_path / "dest"
    await driver.materialize(delta, dest)
    assert (dest / "a.txt").read_text() == "hello v2"
    assert (dest / "new.txt").read_text() == "added"
    assert not (dest / "sub" / "b.txt").exists()
    assert (dest / "emptydir").is_dir()
    assert (dest / "link.lnk").is_symlink()
    assert os_readlink(dest / "link.lnk") == "a.txt"

    # D2 invariant 1: the delta's merkle IS the materialized end state's
    got, _, _ = _merkle_root(dest)
    assert got == delta.merkle
    await driver.destroy("sbx")


def os_readlink(path: Path) -> str:
    import os

    return os.readlink(path)


@pytest.mark.asyncio
async def test_delta_and_full_of_same_state_share_merkle(tmp_path: Path) -> None:
    """Content-addressed end state (ADR-0012 D2 invariant 1): whether the
    snapshot is stored full or as a delta, the root hash identifies the tree.
    The size comparison uses the workload delta exists for — many files, a
    sparse mutation (file-level diff copies whole changed files, so a
    single-changed-file-of-one workspace cannot win; real harness workspaces
    are seeded plans + skills + append-only logs)."""
    driver = ProcessDriver(snapshots_root=tmp_path / "snaps")
    ws = tmp_path / "ws"
    ws.mkdir()
    for i in range(100):
        (ws / f"seed-{i:03d}.txt").write_text(f"stable seed content {i}\n" * 10)
    (ws / "log.jsonl").write_text('{"n":1}\n')
    await _boot(driver, ws)
    try:
        base = await driver.checkpoint("sbx", SnapshotKind.DATA)
        (ws / "log.jsonl").write_text('{"n":1}\n{"n":2}\n')  # sparse mutation
        delta = await driver.checkpoint("sbx", SnapshotKind.DATA, base=base)
        full = await driver.checkpoint("sbx", SnapshotKind.DATA)
        assert delta.merkle == full.merkle
        assert delta.size < full.size  # one changed file + index vs all files
    finally:
        await driver.destroy("sbx")


@pytest.mark.asyncio
async def test_empty_delta_is_tiny_but_correct(tmp_path: Path) -> None:
    """No changes between suspends -> payload is just the index."""
    driver = ProcessDriver(snapshots_root=tmp_path / "snaps")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "stable.txt").write_text("unchanged" * 100)
    await _boot(driver, ws)
    try:
        base = await driver.checkpoint("sbx", SnapshotKind.DATA)
        delta = await driver.checkpoint("sbx", SnapshotKind.DATA, base=base)
        assert delta.delta is True
        assert delta.size < 1024  # the index alone
        assert delta.merkle == base.merkle
        dest = tmp_path / "dest"
        await driver.materialize(delta, dest)
        assert (dest / "stable.txt").read_text() == "unchanged" * 100
    finally:
        await driver.destroy("sbx")


@pytest.mark.asyncio
async def test_chain_of_deltas_materializes_in_order(tmp_path: Path) -> None:
    driver = ProcessDriver(snapshots_root=tmp_path / "snaps")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "log.jsonl").write_text('{"i":0}\n')
    await _boot(driver, ws)
    try:
        prev = await driver.checkpoint("sbx", SnapshotKind.DATA)
        for i in range(1, 5):
            (ws / "log.jsonl").write_text((ws / "log.jsonl").read_text() + f'{{"i":{i}}}\n')
            prev = await driver.checkpoint("sbx", SnapshotKind.DATA, base=prev)
            assert prev.manifest["chain_depth"] == i
        dest = tmp_path / "dest"
        await driver.materialize(prev, dest)
        got, _, _ = _merkle_root(dest)
        assert got == prev.merkle
        assert (dest / "log.jsonl").read_text().count("\n") == 5
    finally:
        await driver.destroy("sbx")


@pytest.mark.asyncio
async def test_corrupt_base_fails_closed_naming_the_hop(tmp_path: Path) -> None:
    driver = ProcessDriver(snapshots_root=tmp_path / "snaps")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("one")
    await _boot(driver, ws)
    try:
        base = await driver.checkpoint("sbx", SnapshotKind.DATA)
        (ws / "a.txt").write_text("two")
        delta = await driver.checkpoint("sbx", SnapshotKind.DATA, base=base)
        # tamper with the base AFTER the delta was taken
        (base.path / "a.txt").write_text("tampered")
        with pytest.raises(DriverError, match="delta chain broken"):
            await driver.materialize(delta, tmp_path / "dest")
    finally:
        await driver.destroy("sbx")


@pytest.mark.asyncio
async def test_reserved_index_name_fails_closed(tmp_path: Path) -> None:
    driver = ProcessDriver(snapshots_root=tmp_path / "snaps")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / _DELTA_INDEX).write_text("{}")  # hostile/accidental root-level collision
    await _boot(driver, ws)
    try:
        base = await driver.checkpoint("sbx", SnapshotKind.DATA)
        with pytest.raises(DriverError, match="reserves the delta index name"):
            await driver.checkpoint("sbx", SnapshotKind.DATA, base=base)
    finally:
        await driver.destroy("sbx")


@pytest.mark.asyncio
async def test_materialize_delta_refuses_non_empty_dest(tmp_path: Path) -> None:
    """Chain verification is meaningless over foreign content — refuse."""
    driver = ProcessDriver(snapshots_root=tmp_path / "snaps")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("one")
    await _boot(driver, ws)
    try:
        base = await driver.checkpoint("sbx", SnapshotKind.DATA)
        (ws / "a.txt").write_text("two")
        delta = await driver.checkpoint("sbx", SnapshotKind.DATA, base=base)
        dest = tmp_path / "dest"
        dest.mkdir()
        (dest / "foreign.txt").write_text("noise")
        with pytest.raises(DriverError, match="file-empty dest"):
            await driver.materialize(delta, dest)
        # empty dirs are fine (the hostlet pre-creates .whirlwind/)
        dest2 = tmp_path / "dest2"
        (dest2 / ".whirlwind").mkdir(parents=True)
        await driver.materialize(delta, dest2)
        assert (dest2 / "a.txt").read_text() == "two"
    finally:
        await driver.destroy("sbx")


@pytest.mark.asyncio
async def test_path_type_flips_file_dir_both_ways(tmp_path: Path) -> None:
    driver = ProcessDriver(snapshots_root=tmp_path / "snaps")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "x").write_text("was a file")
    await _boot(driver, ws)
    try:
        base = await driver.checkpoint("sbx", SnapshotKind.DATA)
        (ws / "x").unlink()
        (ws / "x").mkdir()
        (ws / "x" / "inner.txt").write_text("now a dir")
        (ws / "y").mkdir()
        (ws / "y" / "deep.txt").write_text("dir content")
        delta1 = await driver.checkpoint("sbx", SnapshotKind.DATA, base=base)
        dest = tmp_path / "dest1"
        await driver.materialize(delta1, dest)
        assert (dest / "x" / "inner.txt").read_text() == "now a dir"

        # and back: dir -> file
        import shutil

        shutil.rmtree(ws / "x")
        (ws / "x").write_text("file again")
        shutil.rmtree(ws / "y")
        delta2 = await driver.checkpoint("sbx", SnapshotKind.DATA, base=delta1)
        dest2 = tmp_path / "dest2"
        await driver.materialize(delta2, dest2)
        assert (dest2 / "x").is_file()
        assert (dest2 / "x").read_text() == "file again"
        assert not (dest2 / "y").exists()
        got, _, _ = _merkle_root(dest2)
        assert got == delta2.merkle
    finally:
        await driver.destroy("sbx")


def test_runsc_refuses_delta_before_touching_the_binary(tmp_path: Path) -> None:
    """runsc caps say delta_snapshots=False (ADR-0012 D1): the refusal fires
    before any work — no instance lookup, no subprocess — so it is testable
    without runsc installed."""
    driver = RunscDriver(state_root=tmp_path / "state")
    assert driver.capabilities().delta_snapshots is False
    base = SnapshotArtifact(
        snapshot_id="snap_x",
        subject="other",
        kind=SnapshotKind.DATA,
        path=tmp_path / "nonexistent",
    )
    delta_artifact = SnapshotArtifact(
        snapshot_id="snap_d",
        subject="other",
        kind=SnapshotKind.DATA,
        path=tmp_path / "nonexistent-delta",
        delta=True,
    )
    import asyncio

    with pytest.raises(UnsupportedCapability):
        asyncio.run(driver.checkpoint("never-managed", SnapshotKind.DATA, base=base))
    with pytest.raises(UnsupportedCapability):
        asyncio.run(driver.materialize(delta_artifact, tmp_path / "dest"))


def test_process_caps_declare_delta() -> None:
    assert ProcessDriver(snapshots_root=None).capabilities().delta_snapshots is True
