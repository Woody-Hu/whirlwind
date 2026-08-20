"""Metrics registry + Prometheus text renderer (ADR-0013 P2.1, metrics core)."""

from __future__ import annotations

from whirlwind.observability.metrics import (
    Gauge,
    Histogram,
    MetricsRegistry,
    _fmt_number,
    _label_map_text,
)


def test_counter_monotonic_and_multi_label() -> None:
    reg = MetricsRegistry()
    c = reg.counter("sessions_total", "sessions created", label_names=["agent"])
    c.inc(labels={"agent": "a"})
    c.inc(2.0, labels={"agent": "a"})
    c.inc(labels={"agent": "b"})
    body = reg.render()
    assert 'sessions_total{agent="a"} 3' in body
    assert 'sessions_total{agent="b"} 1' in body


def test_gauge_set_inc_dec() -> None:
    g = Gauge("sandbox_population", "active sandboxes")
    g.set(4)
    g.inc()
    g.dec(2)
    assert g.lines()[0] == ("sandbox_population", {}, 3.0)
    g.set(7.5)
    assert g.lines()[0] == ("sandbox_population", {}, 7.5)


def test_histogram_bucket_distribution_sum_count() -> None:
    h = Histogram("turn_latency", "call latency", buckets=[0.1, 0.5, 1.0])
    for v in (0.05, 0.12, 0.6, 2.0):
        h.observe(v)
    by = {(name, frozenset((labels or {}).items())): value for name, labels, value in h.lines()}

    def line(series: str, **labels):
        return by[(series, frozenset(labels.items()))]

    assert line("turn_latency_bucket", le="0.1") == 1.0
    assert line("turn_latency_bucket", le="0.5") == 2.0
    assert line("turn_latency_bucket", le="1.0") == 3.0
    assert line("turn_latency_bucket", le="+Inf") == 4.0
    assert abs(line("turn_latency_sum") - (0.05 + 0.12 + 0.6 + 2.0)) < 1e-9
    assert line("turn_latency_count") == 4.0


def test_histogram_with_labels() -> None:
    h = Histogram("h", "help", label_names=["route"], buckets=[1.0])
    h.observe(0.5, labels={"route": "/sessions"})
    lines = [(n, lbl.get("route", "")) for n, lbl, _ in h.lines() if n.endswith("_count")]
    assert lines == [("h_count", "/sessions")]


def test_label_value_escaping_in_render() -> None:
    # exposition format escapes backslash, newline and double-quote
    assert _label_map_text({"path": 'a"b\\c\nd'}) == r'{path="a\"b\\c\nd"}'


def test_registry_renders_escaped_label() -> None:
    reg = MetricsRegistry()
    g = reg.gauge("esc", "help", label_names=["path"])
    g.set(1, labels={"path": 'a"b\\c\nd'})
    assert r'esc{path="a\"b\\c\nd"} 1' in reg.render()


def test_duplicate_kind_mismatch_rejected() -> None:
    reg = MetricsRegistry()
    reg.counter("x_total", "h")
    try:
        reg.gauge("x_total", "h")
    except ValueError as exc:
        assert "already registered" in str(exc)
    else:
        raise AssertionError("expected ValueError on kind mismatch")


def test_re_registration_same_kind_is_idempotent() -> None:
    reg = MetricsRegistry()
    c1 = reg.counter("c_total", "h")
    c2 = reg.counter("c_total", "h")
    assert c1 is c2


def test_label_mismatch_raises_keyerror() -> None:
    g = Gauge("g", "h", label_names=["route"])
    try:
        g.set(1, labels={"method": "GET"})
    except KeyError as exc:
        assert "expected labels" in str(exc)
    else:
        raise AssertionError("expected KeyError on unknown label")


def test_render_help_and_type_lines() -> None:
    reg = MetricsRegistry()
    reg.counter("req_total", "http requests")
    body = reg.render()
    assert "# HELP req_total http requests" in body
    assert "# TYPE req_total counter" in body


def test_render_is_deterministic_sorted() -> None:
    reg = MetricsRegistry()
    for name in ("zeta", "alpha", "mid"):
        reg.gauge(name, "h").set(1)
    body = reg.render()
    names = [ln.split()[1] for ln in body.splitlines() if ln.startswith("# TYPE")]
    assert names == sorted(names)


def test_fmt_number() -> None:
    assert _fmt_number(3.0) == "3"
    assert _fmt_number(3.5) == "3.5"
    assert _fmt_number(float("inf")) == "+Inf"