"""Transition tables for the two state machines (architecture fig. 5 / fig. 6).

The tables encode exactly the diagrams; anything else raises InvalidTransition.
"""

from __future__ import annotations

from .errors import InvalidTransition
from .model import SandboxStatus, SessionStatus

SESSION_TRANSITIONS: dict[SessionStatus, frozenset[SessionStatus]] = {
    SessionStatus.CREATED: frozenset({SessionStatus.DISPATCHING, SessionStatus.CLOSED}),
    SessionStatus.DISPATCHING: frozenset({SessionStatus.RUNNING, SessionStatus.CLOSED}),
    SessionStatus.RUNNING: frozenset({SessionStatus.IDLE, SessionStatus.CLOSED}),
    SessionStatus.IDLE: frozenset({SessionStatus.RUNNING, SessionStatus.SUSPENDING, SessionStatus.CLOSED}),
    SessionStatus.SUSPENDING: frozenset({SessionStatus.SUSPENDED, SessionStatus.CLOSED}),
    SessionStatus.SUSPENDED: frozenset({SessionStatus.RESUMING, SessionStatus.CLOSED}),
    SessionStatus.RESUMING: frozenset({SessionStatus.RUNNING, SessionStatus.CLOSED}),
    SessionStatus.CLOSED: frozenset(),
}

SANDBOX_TRANSITIONS: dict[SandboxStatus, frozenset[SandboxStatus]] = {
    SandboxStatus.PROVISIONING: frozenset({SandboxStatus.WARM, SandboxStatus.TERMINATED, SandboxStatus.BINDING}),
    SandboxStatus.WARM: frozenset({SandboxStatus.BINDING, SandboxStatus.TERMINATED}),
    SandboxStatus.BINDING: frozenset({SandboxStatus.ACTIVE, SandboxStatus.TERMINATED}),
    SandboxStatus.ACTIVE: frozenset({SandboxStatus.SNAPSHOTTING, SandboxStatus.DRAINING, SandboxStatus.CRASHED}),
    SandboxStatus.SNAPSHOTTING: frozenset({SandboxStatus.SUSPENDED, SandboxStatus.ACTIVE, SandboxStatus.CRASHED}),
    SandboxStatus.SUSPENDED: frozenset({SandboxStatus.RESUMING, SandboxStatus.TERMINATED}),
    SandboxStatus.RESUMING: frozenset({SandboxStatus.ACTIVE, SandboxStatus.TERMINATED}),
    SandboxStatus.DRAINING: frozenset({SandboxStatus.TERMINATED}),
    SandboxStatus.CRASHED: frozenset({SandboxStatus.TERMINATED}),
    SandboxStatus.TERMINATED: frozenset(),
}


def check_transition(kind: str, table: dict, current, wanted) -> None:
    if wanted not in table[current]:
        raise InvalidTransition(kind, str(current), str(wanted))
