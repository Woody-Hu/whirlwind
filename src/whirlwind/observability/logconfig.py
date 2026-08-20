"""Structured (JSON) logging + request/correlation context (ADR-0013 P2.2).

The gateway and control plane intentionally use the stdlib `logging` interface,
but we replace the default plain-text formatter with a single-line JSON
formatter so every record is machine-parseable. A contextvar carries the
request/correlation scope (request_id, trace_id/span_id, session_id, ...) and a
logging.Filter splices those fields into every record emitted while the scope
is active — giving request-scoped correlation without threading a logger
object through every call site.

`setup_logging()` is idempotent and safe to call at the composition root
(`whirlwind serve`) and in tests. Format may be `json` or `text`.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import traceback
from datetime import datetime, timezone
from typing import Any

# Correlative scope shared by the gateway middleware (writer) and the logging
# filter (reader). Keys are fixed; trace fields (P2.3) live here too so a span
# and the log line that opens it share one contextvar.
request_scope: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "whirlwind_request_scope", default={}
)

# Stable field order so the JSON object is boring and greppable.
_BASE_FIELDS = ("ts", "level", "logger", "message", "exception")
_CONTEXT_FIELDS = (
    "request_id",
    "trace_id",
    "span_id",
    "parent_span_id",
    "session_id",
    "agent_id",
    "sandbox_id",
    "cron_id",
    "version_id",
)
_STATIC_FIELDS = ("service", "environment")


class JSONFormatter(logging.Formatter):
    """One JSON object per line over the stdlib logging interface."""

    def __init__(self, service: str = "whirlwind", environment: str = "unknown") -> None:
        super().__init__()
        self._service = service
        self._environment = environment

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = "".join(traceback.format_exception(*record.exc_info))
        for key in _CONTEXT_FIELDS:
            value = request_scope.get().get(key)
            if value is not None:
                payload[key] = value
        payload["service"] = self._service
        payload["environment"] = self._environment
        # any ad-hoc extras callers attached via logging extra={...}
        for key in sorted(set(record.__dict__) - set(logging.LogRecord(record.name, 1, "", 0, "", {}, None).__dict__)):
            if key.startswith("_"):
                continue
            if key in (_BASE_FIELDS, _CONTEXT_FIELDS, _STATIC_FIELDS):
                continue
            payload[key] = record.__dict__[key]
        return json.dumps(payload, default=_json_default, separators=(",", ":"))


class TextFormatter(logging.Formatter):
    """Plain log line (level logger: message) for interactive / dev shells."""

    def __init__(self, service: str = "whirlwind") -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        req = request_scope.get().get("request_id")
        prefix = f"{record.levelname:<7} {record.name}: "
        ctx = f"[req={req}] " if req else ""
        body = record.getMessage()
        if record.exc_info:
            body += "\n" + "".join(traceback.format_exception(*record.exc_info))
        return f"{prefix}{ctx}{body}"


def setup_logging(
    *,
    level: str = "INFO",
    format: str = "text",
    stream=None,
    service: str = "whirlwind",
    environment: str = "unknown",
    propagate_to: logging.Logger | None = None,
) -> None:
    """Configure the root logger with a single handler in the chosen format.

    Idempotent: re-invocation replaces the handler and level. Tests may pass
    their own `stream` (e.g. io.StringIO) to capture output.
    """
    root = logging.getLogger()
    root.setLevel(level.upper())
    root.handlers.clear()
    handler = logging.StreamHandler(stream or sys.stdout)
    if format == "json":
        handler.setFormatter(JSONFormatter(service=service, environment=environment))
    else:
        handler.setFormatter(TextFormatter(service=service))
    root.addHandler(handler)
    if propagate_to is not None:
        propagate_to.handlers = root.handlers


def set_request_scope(**fields: Any) -> object:
    """Set (merge) the correlation scope for the current context; returns a
    token to restore with `reset_request_scope` (for middleware try/finally)."""
    current = request_scope.get()
    return request_scope.set({**current, **{k: v for k, v in fields.items() if v is not None}})


def reset_request_scope(token: object) -> None:
    request_scope.reset(token)


def current_request_id() -> str | None:
    return request_scope.get().get("request_id")


def _json_default(value: Any) -> str:
    return repr(value)