"""Error hierarchy. Every failure surfaces as an ArgusError subclass with a stable code."""

from __future__ import annotations


class ArgusError(Exception):
    """Base class. `code` is stable across releases for programmatic handling."""

    code = "argus/error"

    def __init__(self, message: str = "", *, detail: dict | None = None) -> None:
        super().__init__(message or self.code)
        self.detail = detail or {}


class InvalidTransition(ArgusError):
    code = "argus/invalid-transition"

    def __init__(self, kind: str, current: str, wanted: str) -> None:
        super().__init__(f"{kind}: {current} -> {wanted} is not a legal transition")
        self.detail = {"kind": kind, "current": current, "wanted": wanted}


class NotFound(ArgusError):
    code = "argus/not-found"


class Conflict(ArgusError):
    code = "argus/conflict"


class SeamError(ArgusError):
    code = "argus/seam"


class HarnessError(ArgusError):
    code = "argus/harness"
