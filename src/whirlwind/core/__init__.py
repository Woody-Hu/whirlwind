"""Domain model: the shared vocabulary of every layer.

Upper layers (gateway) and lower layers (hostlet/drivers) communicate exclusively
through the structures defined here (AgentSession and friends), per the
architecture doc's "AgentSession is the only scheduling unit" principle.

Import-cost contract (cold-start path): the SandboxAgent boots as
`python -m whirlwind.agent.server`, whose import chain reaches this package via
`whirlwind.transport` -> `whirlwind.core.platform`. The pydantic-backed
modules (events / model / statemachine) are therefore re-exported lazily
(PEP 562): `from whirlwind.core import AgentSession` still works everywhere,
but subprocess boot paths that only need platform facts / ids / errors do not
pay the ~50-150ms pydantic import on every sandbox start.
"""

from importlib import import_module

from .errors import WhirlwindError, InvalidTransition, NotFound, Conflict, QuotaExceeded, SeamError, HarnessError
from .ids import new_agent_id, new_version_id, new_session_id, new_sandbox_id, new_snapshot_id, new_cron_id, new_team_id
from .platform import (
    ENV_PLATFORM,
    SYSTEM_LINUX,
    SYSTEM_MACOS,
    SYSTEM_WINDOWS,
    PlatformFacts,
    current_facts,
    detect_facts,
    platform_impl,
    resolve_impl,
)

# name -> submodule (lazy: imported on first attribute access)
_LAZY_EXPORTS: dict[str, str] = {
    **{name: ".events" for name in ("SessionEvent", "Surface", "SurfaceOp", "EventKind")},
    **{
        name: ".model"
        for name in (
            "AgentDefinition",
            "AgentVersion",
            "SeamBindingDecl",
            "SeamConsumerDecl",
            "SkillRef",
            "SeamParamSpec",
            "SeamTemplate",
            "SeamInstance",
            "HarnessBundle",
            "AgentSession",
            "SessionStatus",
            "Sandbox",
            "SandboxStatus",
            "Snapshot",
            "SnapshotKind",
            "CronJob",
            "SessionPolicy",
        )
    },
    **{
        name: ".statemachine"
        for name in ("SESSION_TRANSITIONS", "SANDBOX_TRANSITIONS", "check_transition")
    },
}

__all__ = [
    "WhirlwindError", "InvalidTransition", "NotFound", "Conflict", "QuotaExceeded", "SeamError", "HarnessError",
    "new_agent_id", "new_version_id", "new_session_id", "new_sandbox_id", "new_snapshot_id",
    "new_cron_id", "new_team_id",
    "SessionEvent", "Surface", "SurfaceOp", "EventKind",
    "ENV_PLATFORM", "SYSTEM_LINUX", "SYSTEM_MACOS", "SYSTEM_WINDOWS",
    "PlatformFacts", "current_facts", "detect_facts", "platform_impl", "resolve_impl",
    "AgentDefinition", "AgentVersion", "SeamBindingDecl", "SeamConsumerDecl", "SkillRef",
    "SeamParamSpec", "SeamTemplate", "SeamInstance", "HarnessBundle",
    "AgentSession", "SessionStatus",
    "Sandbox", "SandboxStatus",
    "Snapshot", "SnapshotKind",
    "CronJob", "SessionPolicy",
    "SESSION_TRANSITIONS", "SANDBOX_TRANSITIONS", "check_transition",
]


def __getattr__(name: str):
    try:
        submodule = _LAZY_EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(import_module(submodule, __name__), name)
    globals()[name] = value  # cache: subsequent accesses are a plain dict hit
    return value


def __dir__() -> list[str]:
    return sorted(set(__all__) | {*globals()})
