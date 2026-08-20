"""Structured JSON logging + correlation scope (ADR-0013 P2.2)."""

from __future__ import annotations

import io
import json
import logging

import pytest

from whirlwind.observability.logconfig import (
    JSONFormatter,
    TextFormatter,
    reset_request_scope,
    set_request_scope,
    setup_logging,
)


def _root_records():
    rec = logging.LogRecord("whirlwind.gateway", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    return rec


def test_json_formatter_emits_valid_json_line() -> None:
    fmt = JSONFormatter()
    line = fmt.format(_root_records())
    payload = json.loads(line)
    assert payload["level"] == "INFO"
    assert payload["logger"] == "whirlwind.gateway"
    assert payload["message"] == "hello world"
    assert "ts" in payload
    assert payload["service"] == "whirlwind"


def test_json_formatter_includes_correlation_from_scope() -> None:
    token = set_request_scope(request_id="req-123", session_id="ses-9")
    try:
        payload = json.loads(JSONFormatter().format(_root_records()))
    finally:
        reset_request_scope(token)
    assert payload["request_id"] == "req-123"
    assert payload["session_id"] == "ses-9"


def test_scope_reset_restores_empty() -> None:
    token = set_request_scope(request_id="req-abc")
    reset_request_scope(token)
    payload = json.loads(JSONFormatter().format(_root_records()))
    assert "request_id" not in payload


def test_json_formatter_includes_ad_hoc_extra() -> None:
    rec = _root_records()
    rec.custom_field = 42
    payload = json.loads(JSONFormatter().format(rec))
    assert payload["custom_field"] == 42


def test_text_formatter_emits_level_and_request_id() -> None:
    token = set_request_scope(request_id="req-xyz")
    try:
        line = TextFormatter().format(_root_records())
    finally:
        reset_request_scope(token)
    assert "INFO" in line
    assert "req-xyz" in line
    assert "hello world" in line


def test_setup_logging_replaces_root_handler_and_level() -> None:
    root = logging.getLogger()
    previous = root.handlers[:]
    try:
        stream = io.StringIO()
        setup_logging(level="DEBUG", format="json", stream=stream, service="test-svc")
        assert root.level == logging.DEBUG
        assert len(root.handlers) == 1
        logging.getLogger("whirlwind").info("booted")
        payload = json.loads(stream.getvalue().strip())
        assert payload["service"] == "test-svc"
        assert payload["level"] == "INFO"
    finally:
        root.handlers[:] = previous


def test_setup_logging_idempotent() -> None:
    root = logging.getLogger()
    previous = root.handlers[:]
    try:
        s1, s2 = io.StringIO(), io.StringIO()
        setup_logging(format="json", stream=s1)
        setup_logging(format="json", stream=s2)
        assert len(root.handlers) == 1
    finally:
        root.handlers[:] = previous