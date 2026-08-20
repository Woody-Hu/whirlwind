"""Minimal in-process metrics registry with a Prometheus text renderer.

Deliberately dependency-free (no `prometheus_client`): the exposition format is
a small, well-specified text format (one line per series) and hand-rolling it
keeps the hot `/metrics` path cheap and the dependency surface flat (ADR-0013
D1). Semantics follow the Prometheus data model:

  - Counter   monotonic, non-decreasing.
  - Gauge     arbitrary numeric value.
  - Histogram `_bucket{le=...}` cumulative buckets (+Inf sentinel) plus
              `_sum` and `_count`.

Bucket upper bounds are rendered as configured: the `le` label preserves the float
spelling supplied (1.0 stays `1.0`, matching the Prometheus client convention),
while values elsewhere drop a trailing `0` (3.0 renders as `3`).

All mutation happens under one lock, so a scrape can read while workers write
behind it (asyncio single loop or a small worker pool). Label values are
escaped per the exposition format (backslash, newline, double-quote).
"""

from __future__ import annotations

import threading
from typing import Any, Iterable

_POS_INF = float("inf")

# Default histogram buckets (seconds) — the conventional web-latency sweep.
DEFAULT_HISTOGRAM_BUCKETS = (
    0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0,
)


def _escape(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _fmt_number(value: float) -> str:
    if value == _POS_INF:
        return "+Inf"
    if value == float("-inf"):
        return "-Inf"
    return str(int(value)) if value == int(value) else repr(value)


def _fmt_le(value: float) -> str:
    """Bucket upper bound label: keep the configured float spelling (1.0 -> `1.0`)."""
    if value == _POS_INF:
        return "+Inf"
    return repr(value)


def _validate_name(name: str) -> None:
    if ":" in name or not name or not any(c.isalnum() for c in name):
        raise ValueError(f"invalid metric name {name!r}: no ':' and must be non-empty")


def _label_map_text(label_map: dict[str, Any]) -> str:
    if not label_map:
        return ""
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(label_map.items()))
    return "{" + inner + "}"


class Metric:
    """A single metric family (name + fixed label names). Register once."""

    kind: str = ""

    def __init__(self, name: str, help_text: str, label_names: Iterable[str] = ()) -> None:
        _validate_name(name)
        self.name = name
        self.help_text = help_text
        self.label_names = tuple(label_names)
        self._values: dict[tuple[tuple[str, str], ...], float] = {}

    def _key(self, labels: dict[str, Any] | None) -> tuple[tuple[str, str], ...]:
        labels = labels or {}
        if set(labels) != set(self.label_names):
            raise KeyError(
                f"{self.name}: expected labels {sorted(self.label_names)}, got {sorted(labels)}"
            )
        return tuple(sorted((k, str(v)) for k, v in labels.items()))

    def _map(self, key: tuple[tuple[str, str], ...]) -> dict[str, str]:
        return {k: v for k, v in key}

    def lines(self) -> list[tuple[str, dict[str, str], float]]:
        """Exposition samples: (series_name, label_map, value)."""
        raise NotImplementedError


class Counter(Metric):
    kind = "counter"

    def inc(self, amount: float = 1.0, labels: dict[str, Any] | None = None) -> None:
        key = self._key(labels)
        self._values[key] = self._values.get(key, 0.0) + amount

    def lines(self) -> list[tuple[str, dict[str, str], float]]:
        return [(self.name, self._map(k), v) for k, v in self._values.items()]


class Gauge(Metric):
    kind = "gauge"

    def set(self, value: float, labels: dict[str, Any] | None = None) -> None:
        self._values[self._key(labels)] = float(value)

    def inc(self, amount: float = 1.0, labels: dict[str, Any] | None = None) -> None:
        key = self._key(labels)
        self._values[key] = self._values.get(key, 0.0) + amount

    def dec(self, amount: float = 1.0, labels: dict[str, Any] | None = None) -> None:
        key = self._key(labels)
        self._values[key] = self._values.get(key, 0.0) - amount

    def lines(self) -> list[tuple[str, dict[str, str], float]]:
        return [(self.name, self._map(k), v) for k, v in self._values.items()]


class Histogram(Metric):
    """Prometheus histogram: `_bucket{le=...}` cumulative + `_sum` + `_count`."""

    kind = "histogram"

    def __init__(self, name: str, help_text: str, label_names: Iterable[str] = (),
                 buckets: Iterable[float] = DEFAULT_HISTOGRAM_BUCKETS) -> None:
        super().__init__(name, help_text, label_names)
        self.buckets = tuple(buckets)
        self._buckets: dict[tuple, list[int]] = {}
        self._sums: dict[tuple, float] = {}
        self._counts: dict[tuple, int] = {}

    def observe(self, value: float, labels: dict[str, Any] | None = None) -> None:
        key = self._key(labels)
        if key not in self._buckets:
            self._buckets[key] = [0] * len(self.buckets)
        self._sums[key] = self._sums.get(key, 0.0) + value
        self._counts[key] = self._counts.get(key, 0) + 1
        for i, bound in enumerate(self.buckets):
            if value <= bound:
                self._buckets[key][i] += 1

    def lines(self) -> list[tuple[str, dict[str, str], float]]:
        out: list[tuple[str, dict[str, str], float]] = []
        for key in self._buckets:
            m = self._map(key)
            for i, bound in enumerate(self.buckets):
                le_map = dict(m, le=_fmt_le(bound))
                out.append((self.name + "_bucket", le_map, float(self._buckets[key][i])))
            out.append((self.name + "_bucket", dict(m, le="+Inf"), float(self._counts[key])))
            out.append((self.name + "_sum", m, self._sums[key]))
            out.append((self.name + "_count", m, float(self._counts[key])))
        return out


class MetricsRegistry:
    """Owns metric families and renders the full Prometheus text exposition."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._metrics: dict[str, Metric] = {}

    def _register(self, metric: Metric) -> Metric:
        with self._lock:
            existing = self._metrics.get(metric.name)
            if existing is not None:
                if existing.kind != metric.kind:
                    raise ValueError(f"metric {metric.name!r} already registered as {existing.kind}")
                return existing
            self._metrics[metric.name] = metric
            return metric

    def counter(self, name: str, help_text: str, **kw: Any) -> Counter:
        return self._register(Counter(name, help_text, **kw))

    def gauge(self, name: str, help_text: str, **kw: Any) -> Gauge:
        return self._register(Gauge(name, help_text, **kw))

    def histogram(self, name: str, help_text: str, **kw: Any) -> Histogram:
        return self._register(Histogram(name, help_text, **kw))

    def render(self) -> str:
        """Prometheus text exposition format (0.0.4). Thread-safe."""
        with self._lock:
            metrics = sorted(self._metrics.values(), key=lambda m: m.name)
        out: list[str] = []
        for metric in metrics:
            out.append(f"# HELP {metric.name} {metric.help_text}")
            out.append(f"# TYPE {metric.name} {metric.kind}")
            for name, label_map, value in metric.lines():
                out.append(
                    f"{name}{_label_map_text(label_map)} {_fmt_number(value)}"
                )
        return "\n".join(out) + "\n"