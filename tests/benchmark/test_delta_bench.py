"""Delta snapshot benchmarks (ADR-0012): size win + wall time, sparse workload.

Representative suspend/resume shape: a workspace dominated by stable content
(seeded plan files, staged skills) plus one append-only session log (the dsh
pattern — .whirlwind/sessions/*.jsonl only appends). Measures, on the real
filesystem with the real process driver and no mocks:

- full checkpoint wall time + artifact size per cycle;
- delta checkpoint wall time + payload size per cycle (sparse mutation);
- materialize wall time: full artifact vs a chain of CHAIN deltas.

The only ASSERTED properties are structural (ADR-0012 test strategy): the
delta payload is smaller than the full copy for the sparse workload, and
chain materialization reproduces the end-state merkle. Absolute numbers are
environment-dependent — they land in .test-logs and the session-log, never
in asserts.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from whirlwind.core import SnapshotKind
from whirlwind.drivers import ProcessDriver, SandboxSpec, SnapshotArtifact
from whirlwind.drivers.process import _merkle_root

STABLE_FILES = 200        # seeded content that never changes across cycles
STABLE_FILE_KB = 8        # 8 KiB each -> 1.6 MiB of stable payload
APPEND_KB = 4             # session-log growth per cycle
CHAIN = 8                 # delta hops measured before compaction


def _seed(ws: Path) -> None:
    ws.mkdir(parents=True, exist_ok=True)
    blob = "x" * (STABLE_FILE_KB * 1024)
    for i in range(STABLE_FILES):
        (ws / f"seed-{i:03d}.txt").write_text(blob)


async def _boot(driver: ProcessDriver, workspace: Path) -> str:
    # idle /bin/cat sandbox: the driver only needs a real argv[0]; destroyed
    # right after the measured checkpoints (same pattern as the unit tests)
    spec = SandboxSpec(
        sandbox_id=f"sbx-{workspace.name}",
        argv=["/bin/cat"],
        bundle_root=Path("/"),
        workspace=workspace,
    )
    await driver.create(spec)
    return spec.sandbox_id


@pytest.mark.asyncio
async def test_delta_bench_sparse_workload(tmp_path: Path) -> None:
    driver = ProcessDriver(snapshots_root=tmp_path / "snaps")

    # -- full-mode lineage: CHAIN cycles, full checkpoint each time ---------
    ws_full = tmp_path / "ws-full"
    _seed(ws_full)
    sandbox_full = await _boot(driver, ws_full)
    full_sizes: list[int] = []
    full_times: list[float] = []
    full_last: SnapshotArtifact | None = None
    for cycle in range(CHAIN):
        with (ws_full / "log.jsonl").open("a") as fh:
            fh.write("l" * (APPEND_KB * 1024))
        t0 = time.perf_counter()
        full_last = await driver.checkpoint(sandbox_full, SnapshotKind.DATA)
        full_times.append(time.perf_counter() - t0)
        full_sizes.append(full_last.size)
        assert full_last.delta is False
    await driver.destroy(sandbox_full)
    assert full_last is not None

    # -- delta-mode lineage: same mutations, chained checkpoints ------------
    ws_delta = tmp_path / "ws-delta"
    _seed(ws_delta)
    sandbox_delta = await _boot(driver, ws_delta)
    delta_sizes: list[int] = []
    delta_times: list[float] = []
    prev: SnapshotArtifact | None = None
    for cycle in range(CHAIN):
        with (ws_delta / "log.jsonl").open("a") as fh:
            fh.write("l" * (APPEND_KB * 1024))
        t0 = time.perf_counter()
        prev = await driver.checkpoint(sandbox_delta, SnapshotKind.DATA, base=prev)
        delta_times.append(time.perf_counter() - t0)
        delta_sizes.append(prev.size)
        assert prev.delta is (cycle > 0)  # first of the lineage is full
    assert prev is not None
    end_merkle = prev.merkle
    await driver.destroy(sandbox_delta)

    # -- materialize: full artifact (one copy) vs the CHAIN-deep delta ------
    dest_full = tmp_path / "dest-full"
    t0 = time.perf_counter()
    await driver.materialize(full_last, dest_full)
    full_restore_ms = (time.perf_counter() - t0) * 1000
    dest_chain = tmp_path / "dest-chain"
    t0 = time.perf_counter()
    await driver.materialize(prev, dest_chain)
    chain_restore_ms = (time.perf_counter() - t0) * 1000

    # structural assertions (the ONLY ones — absolute numbers go to the log)
    assert delta_sizes[-1] < full_sizes[-1], "sparse delta must beat full copy"
    assert _merkle_root(dest_chain)[0] == end_merkle
    assert _merkle_root(dest_full)[0] == full_last.merkle

    def ms(xs: list[float]) -> str:
        return "[" + ", ".join(f"{x * 1000:.1f}" for x in xs) + "]"

    def kib(xs: list[int]) -> str:
        return "[" + ", ".join(f"{x / 1024:.0f}" for x in xs) + "]"

    print(
        f"[delta-bench] stable={STABLE_FILES}x{STABLE_FILE_KB}KiB "
        f"append={APPEND_KB}KiB/cycle chain={CHAIN}"
    )
    print(f"[delta-bench] full  size KiB/cycle: {kib(full_sizes)}")
    print(f"[delta-bench] delta size KiB/cycle: {kib(delta_sizes)}")
    print(f"[delta-bench] full  checkpoint ms : {ms(full_times)}")
    print(f"[delta-bench] delta checkpoint ms : {ms(delta_times)}")
    print(f"[delta-bench] materialize full  ms : {full_restore_ms:.1f}")
    print(f"[delta-bench] materialize chain ms : {chain_restore_ms:.1f}")
