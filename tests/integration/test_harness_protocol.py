"""Harness wire protocol integration tests: real echo-harness subprocess over stdio.

No mocks: every test spawns `python -m argus.harness.echo_server` as a real
child process and speaks the dsh-shaped JSON-RPC subset through HarnessRpc.
"""

from __future__ import annotations

import asyncio
import sys
import time

import pytest

from argus.harness.protocol import (
    HarnessNotification,
    HarnessProtocolError,
    HarnessRpc,
    content_blocks,
    parse_session_event,
    parse_session_status,
)


class NotificationRecorder:
    def __init__(self) -> None:
        self.events: list[HarnessNotification] = []
        self.idle = asyncio.Event()

    def __call__(self, notification: HarnessNotification) -> None:
        self.events.append(notification)
        status = parse_session_status(notification)
        if status is not None and status[1] == "idle":
            self.idle.set()


async def _spawn_echo(env: dict[str, str] | None = None) -> HarnessRpc:
    import os

    proc_env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        "PYTHONUNBUFFERED": "1",
        **(env or {}),
    }
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "argus.harness.echo_server",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=proc_env,
    )
    rpc = HarnessRpc(proc)
    rpc.start()
    asyncio.get_running_loop().create_task(rpc.drain_stderr())
    return rpc


async def _drain_turn(recorder: NotificationRecorder, session_id: str, timeout: float = 10.0) -> list[str]:
    await asyncio.wait_for(recorder.idle.wait(), timeout=timeout)
    types: list[str] = []
    for notification in recorder.events:
        event = parse_session_event(notification)
        if event is not None and event.session_id == session_id:
            types.append(event.type)
    return types


@pytest.mark.asyncio
async def test_initialize_handshake() -> None:
    rpc = await _spawn_echo()
    try:
        result = await rpc.request("initialize", {"cwd": "/tmp", "provider": "deepseek-official"})
        assert result["serverInfo"]["name"] == "echo-harness"
    finally:
        await rpc.close()


@pytest.mark.asyncio
async def test_full_turn_event_sequence() -> None:
    rpc = await _spawn_echo()
    try:
        await rpc.request("initialize", {"cwd": "/tmp"})
        recorder = NotificationRecorder()
        rpc.on_notification(recorder)
        result = await rpc.request(
            "session/prompt",
            {"sessionId": "s1", "contentBlocks": content_blocks("hello world")},
        )
        assert result["messageId"]
        types = await _drain_turn(recorder, "s1")
        assert types[0] == "turn/start"
        assert "assistant/message" in types
        assert types[-1] == "turn/end"
        # status transition seen: busy then idle
        statuses = [
            parse_session_status(n)[1] for n in recorder.events if parse_session_status(n) is not None
        ]
        assert statuses == ["busy", "idle"]
        # the final assistant message echoes the input
        final = [n for n in recorder.events if parse_session_event(n) is not None]
        message_events = [
            parse_session_event(n) for n in final
        ]
        assistant = [e for e in message_events if e is not None and e.type == "assistant/message"]
        assert assistant[-1].data["message"]["content"][0]["text"] == "echo: hello world"
    finally:
        await rpc.close()


@pytest.mark.asyncio
async def test_seq_monotonic_per_session() -> None:
    rpc = await _spawn_echo()
    try:
        await rpc.request("initialize", {"cwd": "/tmp"})
        recorder = NotificationRecorder()
        rpc.on_notification(recorder)
        await rpc.request(
            "session/prompt", {"sessionId": "s-seq", "contentBlocks": content_blocks("one")}
        )
        await _drain_turn(recorder, "s-seq")
        seqs = [
            e.seq for e in (parse_session_event(n) for n in recorder.events)
            if e is not None and e.session_id == "s-seq" and e.seq is not None
        ]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)
    finally:
        await rpc.close()


@pytest.mark.asyncio
async def test_shell_tool_roundtrip_runs_real_command() -> None:
    rpc = await _spawn_echo({"ECHO_CWD": "/tmp"})
    try:
        await rpc.request("initialize", {"cwd": "/tmp"})
        recorder = NotificationRecorder()
        rpc.on_notification(recorder)
        await rpc.request(
            "session/prompt",
            {"sessionId": "s-tool", "contentBlocks": content_blocks("/run echo argus-$((20+3))")},
        )
        await _drain_turn(recorder, "s-tool")
        events = [
            e for e in (parse_session_event(n) for n in recorder.events)
            if e is not None and e.session_id == "s-tool"
        ]
        tool_calls = [e for e in events if e.type == "tool/call"]
        tool_results = [e for e in events if e.type == "tool/result"]
        assert len(tool_calls) == 1
        assert tool_calls[0].data["seam"] == "shell.v1"
        assert len(tool_results) == 1
        assert tool_results[0].data["exit_code"] == 0
        assert "argus-23" in tool_results[0].data["output"]
    finally:
        await rpc.close()


@pytest.mark.asyncio
async def test_unknown_method_returns_rpc_error() -> None:
    rpc = await _spawn_echo()
    try:
        with pytest.raises(HarnessProtocolError):
            await rpc.request("no/such/method", {})
    finally:
        await rpc.close()


@pytest.mark.asyncio
async def test_shutdown_terminates_process_cleanly() -> None:
    rpc = await _spawn_echo()
    await rpc.request("initialize", {"cwd": "/tmp"})
    await rpc.close()
    assert rpc._proc.returncode is not None


@pytest.mark.asyncio
async def test_prompt_response_precedes_events() -> None:
    """The messageId response must be written before any turn event (ordering contract)."""
    rpc = await _spawn_echo({"ECHO_CHUNK_MODE": "chars"})
    try:
        await rpc.request("initialize", {"cwd": "/tmp"})
        recorder = NotificationRecorder()
        first_event = asyncio.Event()
        seen_first = {"value": False}

        def handler(notification: HarnessNotification) -> None:
            recorder(notification)
            if not seen_first["value"]:
                seen_first["value"] = True
                first_event.set()

        rpc.on_notification(handler)
        start = time.monotonic()
        result = await rpc.request(
            "session/prompt", {"sessionId": "s-order", "contentBlocks": content_blocks("x" * 512)}
        )
        prompt_latency = time.monotonic() - start
        await asyncio.wait_for(first_event.wait(), timeout=5)
        # session/prompt must return immediately (the turn runs asynchronously)
        assert prompt_latency < 2.0
        assert result["messageId"]
    finally:
        await rpc.close()
