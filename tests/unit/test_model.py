"""Domain model unit tests: invariants, defaults, event surface semantics."""

import time

from argus.core import (
    AgentSession,
    AgentVersion,
    SessionEvent,
    SessionStatus,
    Surface,
    SurfaceOp,
    new_agent_id,
    new_session_id,
    new_sandbox_id,
    new_version_id,
)


def test_id_prefixes() -> None:
    assert new_agent_id().startswith("agt_")
    assert new_version_id().startswith("ver_")
    assert new_session_id().startswith("ses_")
    assert new_sandbox_id().startswith("sbx_")


def test_session_defaults() -> None:
    s = AgentSession(id=new_session_id(), agent_id="a", agent_version_id="v")
    assert s.status == SessionStatus.CREATED
    assert s.route_epoch == 0
    assert s.bound_sandbox_id is None


def test_agent_version_is_plain_data() -> None:
    v = AgentVersion(
        id=new_version_id(),
        agent_id=new_agent_id(),
        version="1",
        harness="dsh",
        image_ref="dsh@0.3",
    )
    dumped = v.model_dump()
    restored = AgentVersion.model_validate(dumped)
    assert restored == v


def test_event_surface_semantics() -> None:
    e = SessionEvent(session_id="s", seq=1, type="assistant/chunk", data={"delta": "hi"})
    assert not e.is_replace()
    r = SessionEvent(
        session_id="s",
        seq=2,
        type="assistant/chunk",
        data={"delta": "[summary]"},
        surface=Surface(op=SurfaceOp.REPLACE, start=0, end=10),
    )
    assert r.is_replace()
    assert r.surface.start == 0 and r.surface.end == 10


def test_event_ts_monotonic_default() -> None:
    a = SessionEvent(session_id="s", seq=1, type="turn/start")
    b = SessionEvent(session_id="s", seq=2, type="turn/end")
    assert b.ts >= a.ts >= 0


def test_model_roundtrip_through_json() -> None:
    s = AgentSession(id=new_session_id(), agent_id="a", agent_version_id="v")
    assert AgentSession.model_validate_json(s.model_dump_json()) == s
