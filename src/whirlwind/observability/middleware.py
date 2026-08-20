"""Lightweight ASGI middleware that reports HTTP metrics to the collector
(ADR-0013 P2.3).

Implemented as a plain ASGI wrapper (not starlette's `BaseHTTPMiddleware`)
so the hot path stays cheap and — critically — streaming responses (the SSE
`/sessions/{id}/stream` face, healths, `/metrics` itself) pass through without
being buffered head-of-stream. Every completed request is timed; the status
code is captured from the `http.response.start` ASGI message, so a raising
handler still records the 500 the error middleware ultimately returns.

Route labeling: matched against the router so cardinality stays bounded
(`/sessions/{id}/turns` not `/sessions/s_123/turns`); unknown paths fall back
to their raw URL path.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI
from starlette.routing import Match

from whirlwind.observability.collector import MetricsCollector
from whirlwind.observability.logconfig import (
    request_scope,
    reset_request_scope,
    set_request_scope,
)
from whirlwind.observability.trace import (TraceContext, child, format_traceparent,
                                           new_root, parse_traceparent)


def route_template(app: FastAPI, scope: dict[str, Any]) -> str:
    """Resolve the scoped request to its matched route template, else raw path.

    Re-matching mirrors the router's first-match-wins contract, so a 404 for an
    unknown path (no `Match.FULL`) labels the raw path rather than inventing a
    bucket.
    """
    raw = scope.get("path", "")
    for route in app.router.routes:
        match, _ = route.matches(scope)
        if match == Match.FULL:
            template = getattr(route, "path", None)
            return template if template else raw
    return raw


class MetricsMiddleware:
    """Timing wrapper for gateway HTTP requests into the metrics collector."""

    def __init__(
        self,
        app: Any,
        collector: MetricsCollector,
        route_resolver: Any,
    ) -> None:
        self.app = app
        self.collector = collector
        self.resolve_route = route_resolver

    async def __call__(
        self, scope: dict[str, Any], receive: Any, send: Any
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "")
        route = self.resolve_route(scope)
        start = time.perf_counter()
        status: int = 500

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal status
            if message.get("type") == "http.response.start":
                status = message.get("status", status)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            self.collector.record_http(
                method, route, status, time.perf_counter() - start
            )


def outbound_traceparent() -> str | None:
    """Traceparent for an outbound call made inside the current request scope.

    The header carries the active request span itself, so a downstream hop (an
    HTTP client inside the gateway task) links its child span back to us and
    stays in the same trace. Notes: the wire format has no parent-span field,
    so relationship is set by the downstream when it creates its child. Returns
    None outside any request context (callers then skip the header and the
    downstream starts its own root trace).
    """
    scope = request_scope.get()
    trace_id, span_id = scope.get("trace_id"), scope.get("span_id")
    if not trace_id or not span_id:
        return None
    span = TraceContext(trace_id=trace_id, span_id=span_id, sampled=True)
    return format_traceparent(span)


def _header(scope: dict[str, Any], name: str) -> str | None:
    wanted = name.lower().encode("latin-1")
    for raw_name, raw_value in scope.get("headers", []) or []:
        if raw_name.lower() == wanted:
            return raw_value.decode("latin-1")
    return None


class TraceMiddleware:
    """Injects W3C trace correlation into the request scope + a `traceresponse`
    header (ADR-0013 P2.3).

    Reads an inbound `traceparent` header if present (W3C propagate-or-reuse
    rule); otherwise starts a fresh root trace. Either way a child span becomes
    the trace_id/span_id/parent_span_id visible to every structured log line
    logged while this request is active, and `traceresponse` carries that span
    back to the caller. The scope is restored in a finally so contextvars never
    leak across requests on a shared event loop.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(
        self, scope: dict[str, Any], receive: Any, send: Any
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        inbound = _header(scope, "traceparent")
        ctx = parse_traceparent(inbound) or new_root(sampled=True)
        span = child(ctx)
        token = set_request_scope(
            trace_id=span.trace_id,
            span_id=span.span_id,
            parent_span_id=span.parent_span_id,
        )
        traceparent = format_traceparent(span)
        already_sent = False

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal already_sent
            if message.get("type") == "http.response.start" and not already_sent:
                already_sent = True
                headers = message.get("headers")
                if headers is not None:
                    headers = headers + [(b"traceresponse", traceparent.encode("latin-1"))]
                    message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            reset_request_scope(token)