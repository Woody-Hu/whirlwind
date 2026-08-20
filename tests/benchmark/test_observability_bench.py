"""Observability benchmark (ADR-0013): the /metrics scrape path throughput.

Numbers come from real runs (manual timing + hard asserts, matching the other
benchmarks). The metric set seeded is representative of a live /metrics scrape
after real traffic — a family per runtime signal with a handful of series each
(HTTP per-route, sessions per-status, a histogram). We measure:
  - `render()`: the synchronous scrape (what /metrics spends most of its time
    building) on that set.
  - `render_async()`: the full /metrics handler path including a session-count
    sampler (CPU-only, no I/O — guards against the sampler loop regressing a
    production scrape).

Floors are intentionally conservative; real numbers are printed below.
"""

from __future__ import annotations

import time

import pytest

from whirlwind.core.model import SessionStatus
from whirlwind.observability.collector import MetricsCollector

RENDERS = 5_000
# Floor on the representative ~120-sample scrape set. Measured ~2.8k/s in the
# dev environment; the floor keeps 2.5x headroom while a production /metrics is
# polled at most ~1/s — the constant is a regression gate, not a load target.
MIN_RENDERS_PER_SEC = 1_000

_HTTP_ROUTES = ("/healthz", "/sessions", "/sessions/{sid}/turns", "/agents", "/metrics")


def _seed_traffic(collector: MetricsCollector) -> None:
    for route in _HTTP_ROUTES:
        for _ in range(200):
            collector.record_http("GET", route, 200, 0.003)
    for ms in (2, 5, 11, 40, 150):  # a histogram-shaped spread of turn latencies
        for _ in range(50):
            collector.record_turn(seconds=ms / 1000.0)
    counts = {status: i for i, status in enumerate(SessionStatus)}
    collector.sample_sessions(counts)
    collector.sample_sandbox_population(12)
    collector.record_wal_append()


def test_metrics_render_throughput() -> None:
    collector = MetricsCollector()
    _seed_traffic(collector)

    collector.render()  # warm the family list once
    start = time.perf_counter()
    for _ in range(RENDERS):
        collector.render()
    elapsed = time.perf_counter() - start
    rate = RENDERS / elapsed

    samples = len(collector.render().rstrip().splitlines())
    print(f"\n[obs] /metrics render: {rate:,.0f} renders/s ({samples} samples per scrape)")
    assert rate >= MIN_RENDERS_PER_SEC, f"render too slow: {rate:,.0f}/s < {MIN_RENDERS_PER_SEC}"


@pytest.mark.asyncio
async def test_metrics_render_async_throughput() -> None:
    collector = MetricsCollector()

    async def _sample_sessions() -> None:
        collector.sample_sessions({status: 1 for status in SessionStatus})

    collector.add_async_sampler(_sample_sessions)
    _seed_traffic(collector)

    await collector.render_async()  # warm
    start = time.perf_counter()
    for _ in range(RENDERS):
        await collector.render_async()
    elapsed = time.perf_counter() - start
    rate = RENDERS / elapsed

    print(f"\n[obs] /metrics render_async (with sampler): {rate:,.0f} renders/s")
    assert rate >= MIN_RENDERS_PER_SEC // 2