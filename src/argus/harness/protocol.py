"""Argus harness wire protocol (M1: the dsh JSON-RPC stdio subset).

A harness mounts natively when it implements this protocol over stdio
(line-delimited JSON-RPC 2.0):

  request      initialize      {cwd, provider, model, maxTokens?} -> {serverInfo}
  request      session/prompt  {sessionId, contentBlocks}         -> {messageId}
  request      shutdown        {}                                 -> {}
  notification session.event   {sessionId, event{type, seq?, time?, data, surface?}}
  notification session.status  {sessionId, status: "idle"|"busy"}
  notification subagent.started/finished {parentSessionId, childSessionId}

Turn lifecycle: after session/prompt is accepted, the harness emits
session.event notifications for the session; the turn ends when the harness
reports session.status idle for that session.

`HarnessRpc` is the client side used by the SandboxAgent; `echo_server` is the
reference conformance harness used to drive real-subprocess integration tests.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable


@dataclass(slots=True)
class HarnessNotification:
    method: str
    payload: dict[str, Any]


class HarnessProtocolError(Exception):
    pass


def content_blocks(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": text}]


class HarnessRpc:
    """Async JSON-RPC client over a harness subprocess's stdio."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._proc = process
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._notify_handlers: list[Callable[[HarnessNotification], None]] = []
        self._reader_task: asyncio.Task | None = None
        self._closed = False
        self.stderr_tail: list[str] = []

    @property
    def closed(self) -> bool:
        return self._closed

    def on_notification(self, handler: Callable[[HarnessNotification], None]) -> None:
        self._notify_handlers.append(handler)

    def start(self) -> None:
        if self._reader_task is None:
            self._reader_task = asyncio.get_running_loop().create_task(self._read_loop())

    async def _read_loop(self) -> None:
        proc = self._proc
        assert proc.stdout is not None
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except json.JSONDecodeError:
                    continue
                self._dispatch(message)
        finally:
            self._closed = True
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(HarnessProtocolError("harness stdout closed"))
            self._pending.clear()

    def _dispatch(self, message: dict[str, Any]) -> None:
        msg_id = message.get("id")
        method = message.get("method")
        if isinstance(msg_id, (str, int)):
            fut = self._pending.pop(str(msg_id), None)
            if fut is None or fut.done():
                return
            if isinstance(message.get("error"), dict):
                fut.set_exception(
                    HarnessProtocolError(str(message["error"].get("message", "rpc error")))
                )
            else:
                fut.set_result(message.get("result") or {})
            return
        if isinstance(method, str):
            params = message.get("params")
            notification = HarnessNotification(method, params if isinstance(params, dict) else {})
            for handler in list(self._notify_handlers):
                handler(notification)

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._closed:
            raise HarnessProtocolError("harness transport closed")
        request_id = uuid.uuid4().hex
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = fut
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        assert self._proc.stdin is not None
        self._proc.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
        await self._proc.stdin.drain()
        return await fut

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        assert self._proc.stdin is not None
        self._proc.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
        await self._proc.stdin.drain()

    async def drain_stderr(self) -> None:
        proc = self._proc
        assert proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            self.stderr_tail.append(line.decode("utf-8", errors="replace").rstrip())
            if len(self.stderr_tail) > 100:
                del self.stderr_tail[:50]

    async def close(self) -> None:
        try:
            if not self._closed:
                await asyncio.wait_for(self.request("shutdown", {}), timeout=5)
        except (HarnessProtocolError, TimeoutError, asyncio.TimeoutError):
            pass
        if self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass


@dataclass
class SessionEventWire:
    """A dsh-shaped session event off the wire."""

    session_id: str
    type: str
    seq: int | None
    time: int | None
    data: dict[str, Any] = field(default_factory=dict)
    surface: dict[str, Any] | None = None


def parse_session_event(notification: HarnessNotification) -> SessionEventWire | None:
    """Extract a session event from a `session.event` notification, else None."""
    if notification.method != "session.event":
        return None
    payload = notification.payload
    event = payload.get("event")
    if not isinstance(event, dict):
        return None
    return SessionEventWire(
        session_id=str(payload.get("sessionId", "")),
        type=str(event.get("type", "")),
        seq=event.get("seq") if isinstance(event.get("seq"), int) else None,
        time=event.get("time") if isinstance(event.get("time"), int) else None,
        data=event.get("data") if isinstance(event.get("data"), dict) else {},
        surface=event.get("surface") if isinstance(event.get("surface"), dict) else None,
    )


def parse_session_status(notification: HarnessNotification) -> tuple[str, str] | None:
    """Extract (session_id, status) from a `session.status` notification, else None."""
    if notification.method != "session.status":
        return None
    payload = notification.payload
    session_id = payload.get("sessionId")
    status = payload.get("status")
    if isinstance(session_id, str) and isinstance(status, str):
        return session_id, status
    return None
