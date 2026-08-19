"""Domain model: the shared vocabulary of every layer.

Upper layers (gateway) and lower layers (hostlet/drivers) communicate exclusively
through the structures defined here (AgentSession and friends), per the
architecture doc's "AgentSession is the only scheduling unit" principle.
"""

from .errors import WhirlwindError, InvalidTransition, NotFound, Conflict, QuotaExceeded, SeamError, HarnessError
from .ids import new_agent_id, new_version_id, new_session_id, new_sandbox_id, new_snapshot_id, new_cron_id, new_team_id
from .events import SessionEvent, Surface, SurfaceOp, EventKind
from .model import (
    AgentDefinition,
    AgentVersion,
    SeamBindingDecl,
    SeamConsumerDecl,
    SkillRef,
    AgentSession,
    SessionStatus,
    Sandbox,
    SandboxStatus,
    Snapshot,
    SnapshotKind,
    CronJob,
    SessionPolicy,
)
from .statemachine import SESSION_TRANSITIONS, SANDBOX_TRANSITIONS, check_transition

__all__ = [
    "WhirlwindError", "InvalidTransition", "NotFound", "Conflict", "QuotaExceeded", "SeamError", "HarnessError",
    "new_agent_id", "new_version_id", "new_session_id", "new_sandbox_id", "new_snapshot_id",
    "new_cron_id", "new_team_id",
    "SessionEvent", "Surface", "SurfaceOp", "EventKind",
    "AgentDefinition", "AgentVersion", "SeamBindingDecl", "SeamConsumerDecl", "SkillRef",
    "AgentSession", "SessionStatus",
    "Sandbox", "SandboxStatus",
    "Snapshot", "SnapshotKind",
    "CronJob", "SessionPolicy",
    "SESSION_TRANSITIONS", "SANDBOX_TRANSITIONS", "check_transition",
]
