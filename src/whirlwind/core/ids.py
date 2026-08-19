"""Prefixed identifier factories. Prefixes make ids self-describing in logs and events."""

from __future__ import annotations

import secrets


def _new(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(12)}"


def new_agent_id() -> str:
    return _new("agt")


def new_version_id() -> str:
    return _new("ver")


def new_session_id() -> str:
    return _new("ses")


def new_sandbox_id() -> str:
    return _new("sbx")


def new_snapshot_id() -> str:
    return _new("snap")


def new_cron_id() -> str:
    return _new("cron")


def new_team_id() -> str:
    return _new("team")
