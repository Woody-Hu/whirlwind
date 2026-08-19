"""Host platform facts and per-platform behaviour plugins (ADR-0007).

One source of truth for "what system am I running on". Code never branches on
`sys.platform` directly (AGENTS.md §3.3); it asks `current_facts()` (a frozen
`PlatformFacts`) or resolves a behaviour plugin with `resolve_impl()`, so:

- dev/test can simulate another OS family through `WHIRLWIND_PLATFORM`
  (identity + OS-family semantics only);
- per-platform behaviour registers as plugins via `@platform_impl` and is
  dispatched against the active platform key.

Honesty boundary (project non-negotiable, AGENTS.md §4.2): the override changes
identity and OS-family semantics — it NEVER fabricates binaries, devices, or
kernel enforcement. Real-execution paths keep gating on real probes
(`shutil.which`, `/dev/vsock`), so a simulated platform cannot turn an absent
substrate green.
"""

from __future__ import annotations

import os
import platform as _stdlib
import socket
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import WhirlwindError

SYSTEM_MACOS = "macos"
SYSTEM_LINUX = "linux"
SYSTEM_WINDOWS = "windows"
SYSTEM_UNKNOWN = "unknown"
_KNOWN_SYSTEMS = (SYSTEM_MACOS, SYSTEM_LINUX, SYSTEM_WINDOWS)

#: Env var configuring the active platform (ADR-0007 D2):
#: `auto` (default/unset) | `macos` | `linux` | `windows`, optionally `/machine`
#: (e.g. `linux/x86_64`). Identity simulation for logic paths only.
ENV_PLATFORM = "WHIRLWIND_PLATFORM"
ENV_AUTO = "auto"

# Linux capability bit for CAP_SYS_ADMIN (linux/capability.h) — the privilege
# runsc needs to run non-rootless with per-sandbox netstack networking.
CAP_SYS_ADMIN = 21

_PlatformImpl = Callable[..., Any]


class PlatformImplError(WhirlwindError):
    code = "whirlwind/platform/impl-not-found"


@dataclass(frozen=True, slots=True)
class PlatformFacts:
    """Snapshot of host platform facts (interface-layer value object, §3.2).

    `vsock` / `restricted` are real probes and are never affected by the
    `WHIRLWIND_PLATFORM` override (ADR-0007 D2 honesty boundary).
    """

    system: str       # "macos" | "linux" | "windows" | "unknown"
    machine: str      # "arm64" | "x86_64" | raw platform.machine()
    vsock: bool       # AF_VSOCK + /dev/vsock actually present
    restricted: bool  # Linux container without full privileges (non-root / no CAP_SYS_ADMIN)

    @property
    def rlimit_as_supported(self) -> bool:
        """RLIMIT_AS as soft=hard is only enforceable on Linux: macOS raises
        "current limit exceeds maximum limit" (ADR-0005 D1 honesty note)."""
        return self.system == SYSTEM_LINUX

    @property
    def uds_path_max(self) -> int:
        """AF_UNIX sun_path cap: 104 bytes on macOS, 108 elsewhere."""
        return 104 if self.system == SYSTEM_MACOS else 108

    @property
    def overridden(self) -> bool:
        """True when WHIRLWIND_PLATFORM explicitly simulates a platform."""
        raw = os.environ.get(ENV_PLATFORM, "").strip()
        return bool(raw) and raw != ENV_AUTO


# ------------------------------------------------------------------ detection

def _detect_system() -> str:
    if sys.platform == "darwin":
        return SYSTEM_MACOS
    if sys.platform.startswith("linux"):
        return SYSTEM_LINUX
    if sys.platform == "win32":
        return SYSTEM_WINDOWS
    return SYSTEM_UNKNOWN


def _normalize_machine(raw: str) -> str:
    machine = raw.lower()
    if machine in ("arm64", "aarch64"):
        return "arm64"
    if machine in ("x86_64", "amd64"):
        return "x86_64"
    return machine


def _vsock_present() -> bool:
    """Real device probe — the honest gate for virtio-vsock I/O."""
    return hasattr(socket, "AF_VSOCK") and Path("/dev/vsock").exists()


def _have_cap_sys_admin() -> bool:
    """CapEff from /proc/self/status (Linux only); False when unreadable."""
    try:
        status = Path("/proc/self/status").read_text()
        for line in status.splitlines():
            if line.startswith("CapEff:"):
                return bool(int(line.split()[1], 16) & (1 << CAP_SYS_ADMIN))
    except (OSError, ValueError, IndexError):
        return False
    return False


def _restricted_container() -> bool:
    """True when this Linux host cannot run privileged runsc: non-root, or a
    rootless container that dropped CAP_SYS_ADMIN. Meaningful on Linux only —
    on other systems runsc gating is the binary probe, which is correct."""
    return os.geteuid() != 0 or not _have_cap_sys_admin()


def detect_facts() -> PlatformFacts:
    """Real detection — no env override applied."""
    system = _detect_system()
    return PlatformFacts(
        system=system,
        machine=_normalize_machine(_stdlib.machine()),
        vsock=_vsock_present(),
        restricted=(system == SYSTEM_LINUX) and _restricted_container(),
    )


def current_facts() -> PlatformFacts:
    """Active platform facts: the WHIRLWIND_PLATFORM identity override applied
    on top of real detection. Probes (`vsock`, `restricted`) always reflect
    reality. Intentionally not cached — probes are stat-level cheap and tests
    change the env between calls (ADR-0007 D1).
    """
    facts = detect_facts()
    raw = os.environ.get(ENV_PLATFORM, "").strip()
    if not raw or raw == ENV_AUTO:
        return facts
    system, _, machine = raw.partition("/")
    if system not in _KNOWN_SYSTEMS:
        raise ValueError(
            f"{ENV_PLATFORM}={raw!r} is not one of "
            f"{'/'.join(_KNOWN_SYSTEMS)} (optionally suffixed /machine)"
        )
    return PlatformFacts(
        system=system,
        machine=_normalize_machine(machine) if machine else facts.machine,
        vsock=facts.vsock,            # device probe: reality, never simulated
        restricted=facts.restricted,  # privilege probe: reality
    )


# ------------------------------------------------------- behaviour plugins

_REGISTRY: dict[str, dict[str, _PlatformImpl]] = {}


def platform_impl(feature: str, platform: str = "*") -> Callable[[_PlatformImpl], _PlatformImpl]:
    """Decorator: register `fn` as the implementation of `feature` for
    `platform` (a system key like ``"macos"``/``"linux"``, or ``"*"`` for any
    system without a specific impl).

    Registration happens at import time of the defining module — imports are
    the wiring; there is no dynamic discovery (ADR-0007 D3).
    """

    def register(fn: _PlatformImpl) -> _PlatformImpl:
        _REGISTRY.setdefault(feature, {})[platform] = fn
        return fn

    return register


def resolve_impl(feature: str) -> _PlatformImpl:
    """Dispatch `feature` against the active platform key (WHIRLWIND_PLATFORM
    honored). Falls back to the ``"*"`` impl; raises PlatformImplError when
    nothing is registered for the feature or the active system.
    """
    impls = _REGISTRY.get(feature, {})
    if not impls:
        raise PlatformImplError(f"no implementation registered for feature {feature!r}")
    active = current_facts().system
    impl = impls.get(active) or impls.get("*")
    if impl is None:
        raise PlatformImplError(
            f"no implementation of {feature!r} for platform {active!r} "
            f"(registered: {sorted(impls)})"
        )
    return impl


def registered_platforms(feature: str) -> list[str]:
    """Platform keys registered for `feature` (introspection/tests)."""
    return sorted(_REGISTRY.get(feature, {}))
