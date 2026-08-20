"""First-class metrics + wiring seam for the runtime (ADR-0013 P2.1).

`MetricsCollector` owns one `MetricsRegistry` and declares the process-wide
metric families the runtime reports on /metrics:

  Gauges are *sampled* — pulled from the source of truth at render time via
  registered samplers (sessions by status come from the MetadataStore, sandbox
  population from the Hostlet), so a value heals across restarts instead of
  trusting incremental deltas.
  Counters/histograms are *event-based* — incremented at the call site (turns,
  WAL appends, HTTP requests).

The collector is an opaque handle wired at the composition root; `/metrics`
only calls `render()`.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable

from whirlwind.core.model import SessionStatus
from whirlwind.observability.metrics import Gauge, Histogram, Counter, MetricsRegistry


class MetricsCollector:
    def __init__(self, registry: MetricsRegistry | None = None) -> None:
        reg = registry or MetricsRegistry()
        self.registry = reg
        self.sessions_by_status = reg.gauge(
            "whirlwind_sessions_by_status", "live sessions, by status", label_names=["status"]
        )
        self.turns_total = reg.counter("whirlwind_turns_total", "harness turns dispatched")
        self.turn_errors_total = reg.counter("whirlwind_turn_errors_total", "harness turns that raised")
        self.turn_seconds = reg.histogram("whirlwind_turn_seconds", "end-to-end turn latency seconds")
        self.sandbox_population = reg.gauge(
            "whirlwind_sandbox_population", "sandboxes currently tracked by this hostlet"
        )
        self.wal_appends_total = reg.counter(
            "whirlwind_wal_appends_total", "durable WAL event-log records committed"
        )
        self.http_requests_total = reg.counter(
            "whirlwind_http_requests_total", "gateway HTTP requests", label_names=["method", "route", "status"]
        )
        self.http_request_seconds = reg.histogram(
            "whirlwind_http_request_seconds", "gateway request latency seconds", label_names=["method", "route"]
        )
        self._samplers: list[Callable[[], None]] = []
        self._async_samplers: list[Callable[[], Awaitable[None]]] = []

    # ------------------------------------------------------------ samplers

    def add_sampler(self, fn: Callable[[], None]) -> None:
        """A no-arg callable run at render() time; sets gauges from live state."""
        self._samplers.append(fn)

    def add_async_sampler(self, fn: Callable[[], Awaitable[None]]) -> None:
        """An async sampler — the source of truth (e.g. the MetadataStore) is
        async, so gauges that must heal across restarts are pulled here at
        render time via coroutines. Run from `render_async()`."""
        self._async_samplers.append(fn)

    def sample_sessions(self, counts: dict[SessionStatus, int]) -> None:
        """Zero-out every known status then set the observed ones (self-healing)."""
        for status in SessionStatus:
            self.sessions_by_status.set(counts.get(status, 0), labels={"status": str(status)})

    def sample_sandbox_population(self, population: int) -> None:
        self.sandbox_population.set(population)

    # ------------------------------------------------------------ event hooks

    def record_turn(self, seconds: float, *, error: bool = False) -> None:
        self.turns_total.inc()
        self.turn_seconds.observe(seconds)
        if error:
            self.turn_errors_total.inc()

    def record_wal_append(self) -> None:
        self.wal_appends_total.inc()

    def record_http(self, method: str, route: str, status: int, seconds: float) -> None:
        self.http_requests_total.inc(labels={"method": method, "route": route, "status": str(status)})
        self.http_request_seconds.observe(seconds, labels={"method": method, "route": route})

    def render(self) -> str:
        for sampler in self._samplers:
            sampler()
        return self.registry.render()

    async def render_async(self) -> str:
        """Render with async samplers pulled first (source-of-truth gauges),
        then the synchronous sampler + registry path."""
        for sampler in self._async_samplers:
            await sampler()
        return self.render()


# helpers used by call sites that want to time an awaitable and report it
@contextlib.asynccontextmanager
async def time_it(record: Callable[[float], None]):
    import asyncio

    start = asyncio.get_running_loop().time()
    try:
        yield
    finally:
        record(asyncio.get_running_loop().time() - start)