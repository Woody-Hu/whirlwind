"""Observability core (ADR-0013): P2.1 metrics, P2.2 structured logging, P2.3
trace context.

Everything here is stdlib-only — the Prometheus text exposition format is
rendered by hand instead of pulling `prometheus_client`, and the JSON log
formatter and W3C trace-context parsing are small, fully-tested, dependency-free
layers. Adding a real collector (OTLP, etc.) later must not require reworking
these primitives.
"""