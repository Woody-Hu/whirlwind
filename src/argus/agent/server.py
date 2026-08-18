"""SandboxAgent: the first process inside a sandbox (ADR D4).

Responsibilities (the architecture's sidecar set, M1 subset):
- ResourceInjector: reads the injection manifest + runtime plan the Hostlet
  wrote into the workspace and launches the harness with exactly that env
- ControlAgent: HTTP face on 127.0.0.1:{ARGUS_AGENT_PORT} — /health, /turn, /stop
- EventTap: forwards harness notifications to the Hostlet ingest endpoint
- LLM Relay jump box: /relay/llm/* forwards to the Hostlet SecretRelay;
  no credential ever exists inside the sandbox

The HTTP face is stdlib asyncio (no fastapi/uvicorn/httpx): agent import cost
dominates sandbox boot, and the heavy frameworks measured ~630ms of imports
vs ~120ms stdlib — every image boots that much faster. Bodies are
Content-Length framed; responses are Connection: close with close-delimited
bodies, which the relay streams through unbuffered (LLM traffic is SSE as
often as JSON).

Env contract (set by the Hostlet through the driver's env whitelist):
  ARGUS_SANDBOX_ID    this sandbox's id (ingest attribution)
  ARGUS_MANIFEST      path to .argus/manifest.json (injection manifest)
  ARGUS_RUNTIME       path to .argus/runtime.json  {launcher[], env{}}
  ARGUS_AGENT_PORT    pre-allocated localhost port for the control face
  ARGUS_HOSTLET_URL   base URL of the Hostlet control face (ingest + secrets)

Run as: python -m argus.agent.server
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from argus.harness.protocol import (
    HarnessRpc,
    content_blocks,
    parse_session_event,
    parse_session_status,
)

_MAX_HEADER_BYTES = 64 * 1024

# hop-by-hop headers never forwarded by the relay
_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


class AgentState:
    def __init__(self) -> None:
        self.rpc: HarnessRpc | None = None
        self.harness_info: dict[str, Any] = {}
        self.session_status: dict[str, str] = {}
        self.hostlet = os.environ.get("ARGUS_HOSTLET_URL", "").rstrip("/")
        self.sandbox_id = os.environ.get("ARGUS_SANDBOX_ID", "")


STATE = AgentState()


# ------------------------------------------------------------- harness boot

async def boot() -> None:
    """ResourceInjector face: launch the harness exactly as the Hostlet planned."""
    manifest = json.loads(Path(os.environ["ARGUS_MANIFEST"]).read_text())
    runtime = json.loads(Path(os.environ["ARGUS_RUNTIME"]).read_text())
    workspace = Path(manifest["workspace_root"])
    workspace.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        *runtime["launcher"],
        cwd=str(workspace),
        env=dict(runtime["env"]),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    rpc = HarnessRpc(proc)
    rpc.start()
    asyncio.get_running_loop().create_task(rpc.drain_stderr())
    rpc.on_notification(_on_notification)
    model = manifest.get("model_config_decl", {})
    info = await rpc.request(
        "initialize",
        {
            "cwd": str(workspace),
            "provider": model.get("provider", "deepseek-official"),
            "model": model.get("model", "deepseek-chat"),
        },
    )
    STATE.rpc = rpc
    STATE.harness_info = info.get("serverInfo", {})


def _on_notification(notification: Any) -> None:
    event = parse_session_event(notification)
    if event is not None and event.session_id:
        payload = {
            "sandbox_id": STATE.sandbox_id,
            "kind": "event",
            "session_id": event.session_id,
            "event": {"type": event.type, "seq": event.seq, "time": event.time, "data": event.data},
        }
    else:
        status = parse_session_status(notification)
        if status is None:
            return
        session_id, value = status
        STATE.session_status[session_id] = value
        payload = {
            "sandbox_id": STATE.sandbox_id,
            "kind": "status",
            "session_id": session_id,
            "status": value,
        }
    asyncio.get_running_loop().create_task(_post_events(payload))


# ------------------------------------------------ stdlib asyncio HTTP client

async def _post_events(payload: dict[str, Any]) -> None:
    """Fire-and-forget ingest: the durable session log stays the source of truth."""
    if not STATE.hostlet:
        return
    try:
        host, port, base = _split_url(STATE.hostlet)
        await _request(
            host, port, "POST", f"{base}/ingest/events",
            {"content-type": "application/json"},
            json.dumps(payload).encode(),
        )
    except (OSError, ValueError):
        pass


def _split_url(url: str) -> tuple[str, int, str]:
    rest = url.split("://", 1)[-1]
    authority, _, base = rest.partition("/")
    host, _, port = authority.partition(":")
    return host or "127.0.0.1", int(port or 80), "/" + base if base else ""


async def _request(
    host: str,
    port: int,
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes,
    timeout_s: float = 30.0,
) -> tuple[int, dict[str, str], bytes]:
    """One-shot HTTP/1.1 request over a fresh connection (Connection: close)."""
    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout_s
        )
        head = f"{method} {path} HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\n"
        for key, value in headers.items():
            head += f"{key}: {value}\r\n"
        head += f"Content-Length: {len(body)}\r\n\r\n"
        writer.write(head.encode() + body)
        await writer.drain()

        status, response_headers = await _read_response_head(reader, timeout_s)
        payload = await _read_body(reader, response_headers, timeout_s)
        return status, response_headers, payload
    finally:
        if writer is not None:
            writer.close()


async def _read_response_head(
    reader: asyncio.StreamReader, timeout_s: float
) -> tuple[int, dict[str, str]]:
    raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout_s)
    if len(raw) > _MAX_HEADER_BYTES:
        raise ValueError("response head too large")
    lines = raw.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        raise ValueError(f"bad status line: {lines[0]!r}")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        key, _, value = line.partition(":")
        headers[key.strip().lower()] = value.strip()
    return int(parts[1]), headers


async def _read_body(
    reader: asyncio.StreamReader, headers: dict[str, str], timeout_s: float
) -> bytes:
    if headers.get("transfer-encoding", "").lower() == "chunked":
        chunks: list[bytes] = []
        while True:
            size_line = await asyncio.wait_for(reader.readline(), timeout_s)
            size = int(size_line.strip().split(b";")[0], 16)
            if size == 0:
                await reader.readuntil(b"\r\n")  # trailing CRLF (and any trailers)
                return b"".join(chunks)
            chunks.append(await asyncio.wait_for(reader.readexactly(size), timeout_s))
            await reader.readexactly(2)  # chunk CRLF
    length = headers.get("content-length")
    if length is not None:
        return await asyncio.wait_for(reader.readexactly(int(length)), timeout_s)
    return await asyncio.wait_for(reader.read(-1), timeout_s)  # close-delimited


# ------------------------------------------------ stdlib asyncio HTTP server

async def _handle_connection(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    try:
        while True:
            request = await _read_request(reader)
            if request is None:
                return
            method, path, headers, body = request
            keep_alive = await _route(reader, writer, method, path, headers, body)
            if not keep_alive:
                return
    except (asyncio.IncompleteReadError, ConnectionError, asyncio.LimitOverrunError, ValueError):
        return  # client went away or spoke garbage; nothing to salvage
    finally:
        writer.close()


async def _read_request(
    reader: asyncio.StreamReader,
) -> tuple[str, str, dict[str, str], bytes] | None:
    try:
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=300.0)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError):
        return None
    if len(raw) > _MAX_HEADER_BYTES:
        raise ValueError("request head too large")
    lines = raw.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) < 3:
        raise ValueError(f"bad request line: {lines[0]!r}")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        key, _, value = line.partition(":")
        headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    body = await reader.readexactly(length) if length else b""
    return parts[0], parts[1], headers, body


def _write_response(
    writer: asyncio.StreamWriter,
    status: int,
    reason: str,
    body: bytes,
    content_type: str = "application/json",
) -> None:
    head = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    )
    writer.write(head.encode() + body)


async def _route(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes,
) -> bool:
    """Dispatch one request; returns False when the connection must close."""
    if path == "/health" and method == "GET":
        payload = {
            "ok": STATE.rpc is not None and not STATE.rpc.closed,
            "harness": STATE.harness_info,
            "sessions": STATE.session_status,
        }
        _write_response(writer, 200, "OK", json.dumps(payload).encode())
        await writer.drain()
        return False

    if path == "/turn" and method == "POST":
        if STATE.rpc is None or STATE.rpc.closed:
            _write_response(writer, 503, "Service Unavailable", b'{"error": "harness not running"}')
            await writer.drain()
            return False
        request_body = json.loads(body)
        blocks = request_body.get("contentBlocks")
        if blocks is None:
            blocks = content_blocks(str(request_body.get("text", "")))
        result = await STATE.rpc.request(
            "session/prompt",
            {"sessionId": str(request_body.get("sessionId", "")), "contentBlocks": blocks},
        )
        _write_response(writer, 200, "OK", json.dumps({"messageId": result.get("messageId", "")}).encode())
        await writer.drain()
        return False

    if path == "/stop" and method == "POST":
        if STATE.rpc is not None:
            await STATE.rpc.close()
            STATE.rpc = None
        _write_response(writer, 200, "OK", b'{"ok": true}')
        await writer.drain()
        return False

    if path.startswith("/relay/llm/"):
        await _relay(writer, method, path, headers, body)
        return False  # the (possibly SSE) body was close-delimited

    _write_response(writer, 404, "Not Found", b'{"error": "not found"}')
    await writer.drain()
    return False


async def _relay(
    writer: asyncio.StreamWriter,
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes,
) -> None:
    """Keyless jump box: the Hostlet SecretRelay owns the credential.

    Forwards to {hostlet}/secret/llm/<rest> and streams the upstream body
    through unbuffered, close-delimited (SSE-safe).
    """
    if not STATE.hostlet:
        _write_response(writer, 503, "Service Unavailable", b'{"error": "no hostlet"}')
        await writer.drain()
        return
    host, port, base = _split_url(STATE.hostlet)
    upstream_path = f"{base}/secret/llm/{path[len('/relay/llm/'):]}"
    upstream_writer = None
    try:
        upstream_reader, upstream_writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=30.0
        )
        head = f"{method} {upstream_path} HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\n"
        for key, value in headers.items():
            if key.lower() not in _HOP_HEADERS:
                head += f"{key}: {value}\r\n"
        head += f"Content-Length: {len(body)}\r\n\r\n"
        upstream_writer.write(head.encode() + body)
        await upstream_writer.drain()

        status, upstream_headers = await _read_response_head(upstream_reader, 30.0)
        content_type = upstream_headers.get("content-type", "application/octet-stream")
        reason = {200: "OK", 401: "Unauthorized", 400: "Bad Request"}.get(status, "Status")
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            "Connection: close\r\n\r\n"  # close-delimited: the body streams until EOF
        )
        writer.write(head.encode())
        await writer.drain()

        if upstream_headers.get("transfer-encoding", "").lower() == "chunked":
            while True:
                size_line = await upstream_reader.readline()
                size = int(size_line.strip().split(b";")[0], 16)
                if size == 0:
                    break
                chunk = await upstream_reader.readexactly(size)
                writer.write(chunk)
                await writer.drain()
                await upstream_reader.readexactly(2)
        else:
            length = upstream_headers.get("content-length")
            remaining = int(length) if length is not None else None
            while remaining is None or remaining > 0:
                chunk = await upstream_reader.read(65536)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
                if remaining is not None:
                    remaining -= len(chunk)
    except (OSError, ValueError, asyncio.IncompleteReadError):
        if not writer.is_closing():
            _write_response(writer, 502, "Bad Gateway", b'{"error": "relay upstream failed"}')
            await writer.drain()
    finally:
        if upstream_writer is not None:
            upstream_writer.close()


async def main() -> None:
    await boot()
    server = await asyncio.start_server(
        _handle_connection, "127.0.0.1", int(os.environ.get("ARGUS_AGENT_PORT", "8000"))
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
