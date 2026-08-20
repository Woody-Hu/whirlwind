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