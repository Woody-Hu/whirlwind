"""Error hierarchy. Every failure surfaces as an WhirlwindError subclass with a stable code."""

from __future__ import annotations


class WhirlwindError(Exception):
    """Base class. `code` is stable across releases for programmatic handling."""

    code = "whirlwind/error"

    def __init__(self, message: str = "", *, detail: dict | None = None) -> None:
        super().__init__(message or self.code)
        self.detail = detail or {}


class InvalidTransition(WhirlwindError):
    code = "whirlwind/invalid-transition"

    def __init__(self, kind: str, current: str, wanted: str) -> None:
        super().__init__(f"{kind}: {current} -> {wanted} is not a legal transition")
        self.detail = {"kind": kind, "current": current, "wanted": wanted}


class NotFound(WhirlwindError):
    code = "whirlwind/not-found"


class Conflict(WhirlwindError):
    code = "whirlwind/conflict"


class SeamError(WhirlwindError):
    code = "whirlwind/seam"


class BadRequest(WhirlwindError):
    code = "whirlwind/bad-request"


class HarnessError(WhirlwindError):
    code = "whirlwind/harness"
