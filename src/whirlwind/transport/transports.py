"""Transport connect/serve primitives: TCP / UDS / virtio-vsock under one face.

Everything here is asyncio streams (or a raw AF_VSOCK socket adapted to them),
so the SandboxAgent's stdlib HTTP stack can speak over any substrate without
changing its request logic:

- `connect(endpoint)`   -> (StreamReader, StreamWriter)   client side
- `serve(listen, handler)` -> asyncio.Server               server side

`listen` strings are the same endpoint grammar as `parse_endpoint`, with one
extension for the server side: `vsock://PORT` binds the host listener on
`VMADDR_CID_ANY` (the host side of the link), and `unix:///path` unlinks a
stale socket file before binding.

vsock reality check: `socket.AF_VSOCK` exists on Linux kernels, but actual
I/O needs a `/dev/vsock` device (i.e. a VM platform underneath). `vsock_available()`
is the honest gate: parsing works always, connect/bind raise DriverError-grade
OSError when the device is absent, and tests skip.
"""

from __future__ import annotations

import asyncio
import os
import socket
from pathlib import Path
from typing import Any, Awaitable, Callable

from whirlwind.core.platform import current_facts

from .endpoints import Endpoint, parse_endpoint

_HANDLER = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[Any]]

# virtio-vsock well-known constants (linux/vm_sockets.h)
VMADDR_CID_ANY = 0xFFFFFFFF  # -1, u32: bind on any local CID (host side)
VMADDR_CID_HOST = 2          # the host as seen from inside a guest


def vsock_available() -> bool:
    """True when this kernel can actually move bytes over virtio-vsock.

    Delegates to the platform facts probe (ADR-0007): a real device check
    (AF_VSOCK + /dev/vsock), never affected by WHIRLWIND_PLATFORM.
    """
    return current_facts().vsock


async def connect(endpoint: Endpoint, timeout_s: float | None = None) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open a stream to `endpoint`; dispatch on its scheme."""
    if endpoint.scheme == "unix":
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(endpoint.address)), timeout=timeout_s
        )
        return reader, writer
    if endpoint.scheme == "tcp":
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(endpoint.address, endpoint.port), timeout=timeout_s
        )
        return reader, writer
    if endpoint.scheme == "vsock":
        return await _connect_vsock(endpoint, timeout_s)
    raise ValueError(f"unsupported transport scheme: {endpoint.scheme!r}")


async def _connect_vsock(endpoint: Endpoint, timeout_s: float | None) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    if not vsock_available():
        raise OSError("virtio-vsock is not available on this host (/dev/vsock missing)")
    sock = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    sock.setblocking(False)
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(loop.sock_connect(sock, (int(endpoint.address), endpoint.port)), timeout=timeout_s)
    except BaseException:
        sock.close()
        raise
    return await asyncio.open_connection(sock=sock)


async def serve(listen: str, handler: _HANDLER) -> asyncio.Server:
    """Bind a listener from a `tcp://…` / `unix://…` / `vsock://…` string."""
    endpoint = parse_endpoint(listen)
    if endpoint.scheme == "unix":
        path = str(endpoint.address)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        return await asyncio.start_unix_server(handler, path=path)
    if endpoint.scheme == "tcp":
        return await asyncio.start_server(handler, endpoint.address, endpoint.port)
    if endpoint.scheme == "vsock":
        return await _serve_vsock(endpoint, handler)
    raise ValueError(f"unsupported transport scheme: {endpoint.scheme!r}")


async def _serve_vsock(endpoint: Endpoint, handler: _HANDLER) -> asyncio.Server:
    if not vsock_available():
        raise OSError("virtio-vsock is not available on this host (/dev/vsock missing)")
    sock = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((VMADDR_CID_ANY, endpoint.port))
    sock.listen(128)
    sock.setblocking(False)
    return await asyncio.start_server(handler, sock=sock)
