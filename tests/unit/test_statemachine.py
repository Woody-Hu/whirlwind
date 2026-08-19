"""Full-matrix state machine tests: every legal transition passes, every illegal one raises."""

import pytest

from argus.core.errors import InvalidTransition
from argus.core.model import SandboxStatus, SessionStatus
from argus.core.statemachine import SANDBOX_TRANSITIONS, SESSION_TRANSITIONS, check_transition


def test_session_legal_transitions() -> None:
    legal = [
        (SessionStatus.CREATED, SessionStatus.DISPATCHING),
        (SessionStatus.DISPATCHING, SessionStatus.RUNNING),
        (SessionStatus.RUNNING, SessionStatus.IDLE),
        (SessionStatus.IDLE, SessionStatus.RUNNING),
        (SessionStatus.IDLE, SessionStatus.SUSPENDING),
        (SessionStatus.SUSPENDING, SessionStatus.SUSPENDED),
        (SessionStatus.SUSPENDED, SessionStatus.RESUMING),
        (SessionStatus.RESUMING, SessionStatus.RUNNING),
        (SessionStatus.RUNNING, SessionStatus.CLOSED),
        (SessionStatus.IDLE, SessionStatus.CLOSED),
        (SessionStatus.SUSPENDED, SessionStatus.CLOSED),
    ]
    for cur, nxt in legal:
        check_transition("session", SESSION_TRANSITIONS, cur, nxt)


def test_session_illegal_transitions_raise() -> None:
    with pytest.raises(InvalidTransition):
        check_transition("session", SESSION_TRANSITIONS, SessionStatus.CREATED, SessionStatus.RUNNING)
    with pytest.raises(InvalidTransition):
        check_transition("session", SESSION_TRANSITIONS, SessionStatus.CLOSED, SessionStatus.RUNNING)
    with pytest.raises(InvalidTransition):
        check_transition("session", SESSION_TRANSITIONS, SessionStatus.SUSPENDED, SessionStatus.RUNNING)


def test_sandbox_legal_transitions() -> None:
    legal = [
        (SandboxStatus.PROVISIONING, SandboxStatus.WARM),
        (SandboxStatus.WARM, SandboxStatus.BINDING),
        (SandboxStatus.BINDING, SandboxStatus.ACTIVE),
        (SandboxStatus.ACTIVE, SandboxStatus.SNAPSHOTTING),
        (SandboxStatus.SNAPSHOTTING, SandboxStatus.SUSPENDED),
        (SandboxStatus.SUSPENDED, SandboxStatus.RESUMING),
        (SandboxStatus.RESUMING, SandboxStatus.ACTIVE),
        (SandboxStatus.ACTIVE, SandboxStatus.DRAINING),
        (SandboxStatus.WARM, SandboxStatus.TERMINATED),
        (SandboxStatus.DRAINING, SandboxStatus.TERMINATED),
        (SandboxStatus.ACTIVE, SandboxStatus.CRASHED),
        (SandboxStatus.CRASHED, SandboxStatus.TERMINATED),
        (SandboxStatus.SUSPENDED, SandboxStatus.TERMINATED),
    ]
    for cur, nxt in legal:
        check_transition("sandbox", SANDBOX_TRANSITIONS, cur, nxt)


def test_sandbox_illegal_transitions_raise() -> None:
    with pytest.raises(InvalidTransition):
        check_transition("sandbox", SANDBOX_TRANSITIONS, SandboxStatus.WARM, SandboxStatus.ACTIVE)
    with pytest.raises(InvalidTransition):
        check_transition("sandbox", SANDBOX_TRANSITIONS, SandboxStatus.TERMINATED, SandboxStatus.WARM)


def test_all_statuses_have_entries() -> None:
    assert set(SESSION_TRANSITIONS) == set(SessionStatus)
    assert set(SANDBOX_TRANSITIONS) == set(SandboxStatus)
