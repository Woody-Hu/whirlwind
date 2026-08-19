"""Sandbox transport integration: real streams over TCP and Unix domain sockets.

These tests prove the transport layer (endpoint model + connect/serve) moves
real HTTP bytes end-to-end, and that the SandboxAgent's stdlib HTTP client and
server both work over a non-TCP substrate (UDS) — the same code path a guest
would use over virtio-vsock. vsock itself is exercised only when the host has
a /dev/vsock device (i.e. an actual VM platform).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

import whirlwind.agent.server as agent
from whirlwind.transport import connect, parse_url, serve, vsock_available

VSOCK_PRESENT = vsock_available()


def _uds_path(name: str) -> Path:
    """Return a Unix-socket path short enough for AF_UNIX on macOS.

    macOS caps AF_UNIX paths at 104 bytes; pytest's real tmp dir lives under
    /var/folders/... and can already be ~60 bytes, so appending a socket name
    blows past the cap. Put sockets in a short dedicated dir under $TMPDIR.
    """
    base = Path(os.environ.get("TMPDIR", "/tmp"))
    short = base / "whirlwind-uds"
    short.mkdir(parents=True, exist_ok=True)
    return short / name


async def _read_head(
    reader: asyncio.StreamReader,
) -> tuple[str, str, dict[str, str], bytes]:
    raw = await reader.readuntil(b"\r\n\r\n")
    lines = raw.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    headers = agent._parse_header_lines(lines[1:])
    body = await reader.readexactly(int(headers.get("content-length", "0")))
    return parts[0], parts[1], headers, body


def _ok_response() -> bytes:
    return b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"


async def _roundtrip(listen: str, url: str) -> None:
    received: dict = {}

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        method, path, headers, body = await _read_head(reader)
        received["method"], received["path"] = method, path
        received["body"] = body
        writer.write(_ok_response())
        await writer.drain()
        writer.close()

    server = await serve(listen, handler)
    try:
        endpoint, base = parse_url(url)
        reader, writer = await connect(endpoint)
        writer.write(b"POST /ingest HTTP/1.1\r\nHost: t\r\nContent-Length: 5\r\n\r\nhello")
        await writer.drain()
        head = await reader.readuntil(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 200")
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
    assert received["path"] == "/ingest"
    assert received["body"] == b"hello"


@pytest.mark.asyncio
async def test_tcp_roundtrip_via_transport() -> None:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    await _roundtrip(f"tcp://127.0.0.1:{port}", f"http://127.0.0.1:{port}")


@pytest.mark.asyncio
async def test_unix_roundtrip_via_transport() -> None:
    sock = _uds_path("hostlet.sock")
    await _roundtrip(f"unix://{sock}", f"http+unix://{sock}")


async def test_agent_client_posts_events_over_uds() -> None:
    """The SandboxAgent's real ingest client (stdlib HTTP) over a UDS hostlet."""
    sock = _uds_path("hostlet.sock")
    got: dict = {}

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        _, path, _, body = await _read_head(reader)
        got["path"] = path
        got["body"] = json.loads(body)
        writer.write(_ok_response())
        await writer.drain()
        writer.close()

    server = await serve(f"unix://{sock}", handler)
    try:
        agent.STATE.hostlet = f"http+unix://{sock}"
        await agent._post_events(
            {"kind": "event", "session_id": "ses_x", "event": {"type": "turn/end"}}
        )
    finally:
        server.close()
        await server.wait_closed()
    assert got["path"] == "/ingest/events"
    assert got["body"]["session_id"] == "ses_x"


@pytest.mark.asyncio
async def test_agent_server_serves_over_uds() -> None:
    """The SandboxAgent's real stdlib HTTP server on a UDS listener."""
    sock = _uds_path("agent.sock")
    server = await serve(f"unix://{sock}", agent._handle_connection)
    try:
        reader, writer = await connect(parse_url(f"http+unix://{sock}")[0])
        writer.write(b"GET /health HTTP/1.1\r\nHost: unix\r\nConnection: close\r\n\r\n")
        await writer.drain()
        head = await reader.readuntil(b"\r\n\r\n")
        body = await reader.read(-1)
        writer.close()
        assert head.startswith(b"HTTP/1.1 200")
        assert b'"ok": false' in body  # no harness bound in-process
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.skipif(not VSOCK_PRESENT, reason="virtio-vsock device (/dev/vsock) not present")
@pytest.mark.asyncio
async def test_vsock_transport_roundtrip() -> None:
    import socket

    from whirlwind.transport.transports import VMADDR_CID_HOST

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        assert await reader.read(64) == b"ping"
        writer.write(b"pong")
        await writer.drain()
        writer.close()

    sock = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((0xFFFFFFFF, 0))  # any local CID, ephemeral port
    port = sock.getsockname()[1]
    sock.listen(8)
    sock.setblocking(False)
    server = await asyncio.start_server(handler, sock=sock)
    try:
        reader, writer = await connect(parse_url(f"http+vsock://{VMADDR_CID_HOST}:{port}")[0])
        writer.write(b"ping")
        await writer.drain()
        assert await reader.read(64) == b"pong"
        writer.close()
    finally:
        server.close()
        await server.wait_closed()
