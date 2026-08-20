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


class QuotaExceeded(WhirlwindError):
    code = "whirlwind/quota-exceeded"


class SeamError(WhirlwindError):
    code = "whirlwind/seam"


class BadRequest(WhirlwindError):
    code = "whirlwind/bad-request"


class Unprocessable(WhirlwindError):
    """Syntactically valid payload whose declared references disagree (422).

    Distinct from BadRequest (malformed payload) so clients can tell a shape
    error from a binding conflict (ADR-0011 D4: an explicit value that
    disagrees with a harness bundle is a 422, not a silent override).
    """

    code = "whirlwind/unprocessable"


class HarnessError(WhirlwindError):
    code = "whirlwind/harness"
