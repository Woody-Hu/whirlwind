"""SessionEvent: the append-only log record, aligned with the unified event model.

Schema (architecture 4.5): seq is monotonic per session; `surface` carries
append/replace semantics so compaction rewrites stay lossless.
"""

from __future__ import annotations

import time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class SurfaceOp(StrEnum):
    APPEND = "append"
    REPLACE = "replace"


class Surface(BaseModel):
    """Rewrite semantics: append grows the surface; replace swaps [start, end)."""

    op: SurfaceOp = SurfaceOp.APPEND
    start: int | None = None
    end: int | None = None


class EventKind(StrEnum):
    """Platform-normalized event vocabulary (minimum set for M1)."""

    TURN_START = "turn/start"
    ASSISTANT_CHUNK = "assistant/chunk"
    TOOL_CALL = "tool/call"
    TOOL_RESULT = "tool/result"
    TURN_END = "turn/end"
    SESSION_STARTED = "session/started"
    SESSION_SUSPENDED = "session/suspended"
    SESSION_RESUMED = "session/resumed"
    ERROR = "error"


class SessionEvent(BaseModel):
    session_id: str
    seq: int
    type: str
    ts: int = Field(default_factory=lambda: int(time.time() * 1000))
    data: dict[str, Any] = Field(default_factory=dict)
    surface: Surface = Field(default_factory=Surface)

    def is_replace(self) -> bool:
        return self.surface.op == SurfaceOp.REPLACE
