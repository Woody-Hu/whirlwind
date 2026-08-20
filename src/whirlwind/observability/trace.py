"""W3C Trace Context primitives (ADR-0013 P2.3) — stdlib-only.

This is the dependency-free core of distributed tracing: model + parse + emit
for the `traceparent` and `tracestate` headers. It is deliberately small and
fully tested so a real collector/OTLP exporter can be layered on later without
reworking these primitives (the same contract the metrics/logging layers keep).

Header shapes (W3C Trace Context):
    traceparent = "00-<trace-id>-<span-id>-<flags>"   trace-id=32 hex, span-id=16 hex
    tracestate  = "key=value,key2=value2"             (opaque, <=32 PT2 entries)
Only the "00" version is supported; anything else parses to None (the caller
then starts a fresh root trace — the spec's "reject → new trace" rule).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Iterable

# 2 chars version + 1 dash + 32 + 1 dash + 16 + 1 dash + 2 flags
_TRACEPARENT_LEN = 55
_VERSION = "00"
_SAMPLED_FLAG = "01"


def _is_hex(value: str) -> bool:
    try:
        int(value, 16)
        return True
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class TraceContext:
    """One span within a trace; immutable so it can be shared across coroutines."""

    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    sampled: bool = False
    tracestate: str = ""

    def __post_init__(self) -> None:
        if len(self.trace_id) != 32 or not _is_hex(self.trace_id):
            raise ValueError(f"invalid trace_id {self.trace_id!r}")
        if len(self.span_id) != 16 or not _is_hex(self.span_id):
            raise ValueError(f"invalid span_id {self.span_id!r}")
        if self.trace_id == "0" * 32:
            raise ValueError("trace_id must be non-zero")
        if self.span_id == "0" * 16:
            raise ValueError("span_id must be non-zero")


def new_trace_id() -> str:
    return secrets.token_hex(16)


def new_span_id() -> str:
    span_id = secrets.token_hex(8)
    while span_id == "0" * 16:  # 0 is reserved as "invalid"
        span_id = secrets.token_hex(8)
    return span_id


def parse_traceparent(header: str | None) -> TraceContext | None:
    """Parse a `traceparent` header value; None on any malformed/foreign input.

    Inbound contexts carry the CALLER's parent span. Callers that need their own
    span use `child(...)` (or `new_root()`) rather than reusing this verbatim.
    """
    if not header:
        return None
    header = header.strip()
    if len(header) != _TRACEPARENT_LEN:
        return None
    version, trace_id, span_id, flags = header.split("-")
    if version != _VERSION:
        return None
    if len(trace_id) != 32 or len(span_id) != 16 or len(flags) != 2:
        return None
    if not (_is_hex(trace_id) and _is_hex(span_id) and _is_hex(flags)):
        return None
    if trace_id == "0" * 32 or span_id == "0" * 16:
        return None
    try:
        return TraceContext(
            trace_id=trace_id,
            span_id=span_id,
            sampled=int(flags, 16) & 0x01 == 0x01,
        )
    except ValueError:
        return None


def format_traceparent(tc: TraceContext) -> str:
    flag = _SAMPLED_FLAG if tc.sampled else "00"
    return f"{_VERSION}-{tc.trace_id}-{tc.span_id}-{flag}"


def child(parent: TraceContext) -> TraceContext:
    """A new child span under `parent`: keeps the trace, new span id, and links
    back to the parent as `parent_span_id`. Inheritance is immutable-friendly.
    """
    return TraceContext(
        trace_id=parent.trace_id,
        span_id=new_span_id(),
        parent_span_id=parent.span_id,
        sampled=parent.sampled,
        tracestate=parent.tracestate,
    )


def new_root(sampled: bool = True) -> TraceContext:
    return TraceContext(trace_id=new_trace_id(), span_id=new_span_id(), sampled=sampled)


def format_tracestate(entries: Iterable[tuple[str, str]]) -> str:
    """Normalize (key, value) pairs into a W3C `tracestate` value (opaque to us)."""
    return ",".join(f"{k}={v}" for k, v in entries)