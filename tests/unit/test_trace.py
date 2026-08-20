"""Unit tests for W3C trace-context primitives (ADR-0013 P2.3).

Pure logic over real strings/bytes: parse/format round-trips, spec-invalid
rejection, id generation, child-span semantics. No mocks.
"""

from __future__ import annotations

import pytest

from whirlwind.observability.logconfig import request_scope, set_request_scope
from whirlwind.observability.middleware import outbound_traceparent
from whirlwind.observability.trace import (
    TraceContext,
    child,
    format_traceparent,
    format_tracestate,
    new_root,
    new_span_id,
    new_trace_id,
    parse_traceparent,
)

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"


def _is_hex_n(value: str, n: int) -> bool:
    return len(value) == n and all(c in "0123456789abcdef" for c in value)


def test_new_ids_are_spec_shaped() -> None:
    assert _is_hex_n(new_trace_id(), 32)
    assert _is_hex_n(new_span_id(), 16)
    # extremely unlikely, but the reserved all-zero span id must be rejected
    assert new_span_id() != "0" * 16


def test_round_trip() -> None:
    ctx = TraceContext(trace_id=TRACE_ID, span_id=SPAN_ID, sampled=True)
    header = format_traceparent(ctx)
    assert header == f"00-{TRACE_ID}-{SPAN_ID}-01"
    parsed = parse_traceparent(header)
    assert parsed is not None
    assert parsed == ctx
    assert parsed.sampled is True


def test_unsampled_flags() -> None:
    header = format_traceparent(TraceContext(trace_id=TRACE_ID, span_id=SPAN_ID, sampled=False))
    assert header.endswith("-00")
    assert parse_traceparent(header).sampled is False


@pytest.mark.parametrize(
    "bad",
    [
        "",                                   # empty
        "00-1234",                            # far too short
        "01-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",  # unsupported version
        "00-gbf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",  # non-hex trace id
        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7",      # missing flags
        f"00-{'0'*32}-00f067aa0ba902b7-01",                         # all-zero trace id
        f"00-4bf92f3577b34da6a3ce929d0e0e4736-{'0'*16}-01",          # all-zero span id
        "00-4bf92f3577b34da6a3ce929d0e0e47-00f067aa0ba902b7-01",    # trace id 31 hex
    ],
)
def test_parse_rejects_malformed(bad: str) -> None:
    assert parse_traceparent(bad) is None
    assert parse_traceparent(None) is None


def test_child_links_and_inherits() -> None:
    parent = TraceContext(trace_id=TRACE_ID, span_id=SPAN_ID, sampled=True, tracestate="k=v")
    probe = child(parent)
    assert probe.trace_id == TRACE_ID  # same trace
    assert probe.span_id != SPAN_ID  # fresh span
    assert _is_hex_n(probe.span_id, 16)
    assert probe.parent_span_id == SPAN_ID  # links back
    assert probe.sampled is True  # inheritance
    assert probe.tracestate == "k=v"


def test_new_root() -> None:
    root = new_root(sampled=True)
    assert _is_hex_n(root.trace_id, 32)
    assert _is_hex_n(root.span_id, 16)
    assert root.parent_span_id is None
    assert root.sampled is True


def test_parse_inbound_is_caller_span_not_ours() -> None:
    # an inbound traceparent is the CALLER's span; a service builds a child for
    # its own work (mirrors the TraceMiddleware flow).
    inbound = parse_traceparent(f"00-{TRACE_ID}-{SPAN_ID}-01")
    ours = child(inbound)
    assert ours.trace_id == TRACE_ID
    assert ours.parent_span_id == SPAN_ID
    assert ours.span_id != SPAN_ID


def test_tracestate_format() -> None:
    assert format_tracestate([("vendor", "v1"), ("congo", "t61rcWkgMzE")]) == (
        "vendor=v1,congo=t61rcWkgMzE"
    )
    assert format_tracestate([]) == ""


def test_validation_rejects_bad_ids_in_model() -> None:
    with pytest.raises(ValueError):
        TraceContext(trace_id="zz", span_id=SPAN_ID)
    with pytest.raises(ValueError):
        TraceContext(trace_id=TRACE_ID, span_id="0" * 16)


def test_outbound_traceparent_outside_scope_is_none() -> None:
    token = set_request_scope()  # empty scope
    try:
        assert outbound_traceparent() is None
    finally:
        request_scope.reset(token)


def test_outbound_traceparent_carries_request_span() -> None:
    # the wire header carries the ACTIVE request span (trace + span); the
    # downstream links its own child back to it (parent_span_id is not on the
    # wire — the neighbouring correct unit/behaviour tests cover that).
    token = set_request_scope(trace_id=TRACE_ID, span_id=SPAN_ID)
    try:
        header = outbound_traceparent()
        assert header is not None and header.startswith("00-")
        outbound = parse_traceparent(header)
        assert outbound is not None
        assert outbound.trace_id == TRACE_ID  # stays in the same trace
        assert outbound.span_id == SPAN_ID  # it IS the request span the next hop parents from
        assert outbound.sampled is True
    finally:
        request_scope.reset(token)