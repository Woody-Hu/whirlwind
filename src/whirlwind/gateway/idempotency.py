"""Idempotency-key middleware for mutating routes (ADR-0005 D3).

A POST/DELETE carrying an `Idempotency-Key` header becomes exactly-once
from the client's point of view: the first request executes and its
response is stored in the KVStore (with TTL); retries with the same key
replay that response instead of re-executing. Keys are scoped to
(method, path, key) so a key reused on another route is never replayed,
and the claim records a sha256 of the request body — the same key with a
different body is rejected (422), matching Stripe semantics.

Lives behind the existing KVStore seam (ADR-0004): memory backend for
single-process, Redis for multi-process. Pure ASGI — no Starlette
middleware base class, no streaming surprises (all mutating responses
here are JSON).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Awaitable, Callable

from whirlwind.storage.providers import KVStore

Pending = dict[str, Any]
Send = Callable[[dict[str, Any]], Awaitable[None]]
Receive = Callable[[], Awaitable[dict[str, Any]]]

ERR_IN_FLIGHT = {"error": {
    "code": "whirlwind/idempotency-in-flight",
    "message": "an earlier request with this idempotency key is still executing",
    "detail": {},
}}
ERR_KEY_REUSE = {"error": {
    "code": "whirlwind/idempotency-key-reuse",
    "message": "idempotency key was already used with a different request body",
    "detail": {},
}}


class IdempotencyMiddleware:
    """ASGI middleware; construct with the app it wraps and a started KVStore."""

    def __init__(self, app: Any, kv: KVStore, *, ttl_s: float = 86400.0) -> None:
        self.app = app
        self.kv = kv
        self.ttl_s = ttl_s

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in ("POST", "DELETE"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        idem_key = headers.get("idempotency-key")
        if not idem_key:
            await self.app(scope, receive, send)
            return

        body = await _drain_body(receive)
        body_sha = hashlib.sha256(body).hexdigest()
        kv_key = f"idem:{scope['method']}:{scope['path']}:{idem_key}"

        stored = await self.kv.get(kv_key)
        if stored is None:
            claim = json.dumps({"pending": True, "sha": body_sha})
            if await self.kv.cas(kv_key, None, claim):
                await self._execute_and_store(scope, body, kv_key, body_sha, send)
                return
            stored = await self.kv.get(kv_key)  # lost the race — fall through to replay

        if stored is not None:
            record = json.loads(stored)
            if record.get("pending"):
                await _send_json(send, 409, ERR_IN_FLIGHT)
            elif record.get("sha") != body_sha:
                await _send_json(send, 422, ERR_KEY_REUSE)
            else:
                # replay verbatim: the stored body is already the exact wire payload
                await _send_raw(send, record["status"], record["body"], record.get("content_type"))
            return

        # CAS failed but the winner's record vanished (TTL race) — execute.
        await self.kv.put(kv_key, json.dumps({"pending": True, "sha": body_sha}), ttl_s=self.ttl_s)
        await self._execute_and_store(scope, body, kv_key, body_sha, send)

    # ------------------------------------------------------------ internals

    async def _execute_and_store(
        self,
        scope: dict[str, Any],
        body: bytes,
        kv_key: str,
        body_sha: str,
        send: Send,
    ) -> None:
        state: dict[str, Any] = {"start": None, "chunks": []}

        async def buffered_receive() -> dict[str, Any]:
            if not state.get("body_sent"):
                state["body_sent"] = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        async def capture_send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                state["start"] = message
            elif message["type"] == "http.response.body":
                state["chunks"].append(message.get("body", b""))
                if not message.get("more_body"):
                    await self._finish(send, state, kv_key, body_sha)
                    state["chunks"] = []

        try:
            await self.app(scope, buffered_receive, capture_send)
        except Exception:
            # a crashed handler must not poison the key until TTL
            await self.kv.delete(kv_key)
            raise

    async def _finish(
        self,
        send: Send,
        state: dict[str, Any],
        kv_key: str,
        body_sha: str,
    ) -> None:
        start: dict[str, Any] = state["start"]
        status = start["status"]
        payload = b"".join(state["chunks"])
        if status < 500:
            # store for replay (2xx/4xx are deterministic outcomes; only
            # 5xx are considered retryable and release the key)
            record = json.dumps({
                "status": status,
                "body": payload.decode(errors="replace"),
                "sha": body_sha,
                "content_type": _content_type(start),
            })
            await self.kv.put(kv_key, record, ttl_s=self.ttl_s)
        else:
            await self.kv.delete(kv_key)
        await send(start)
        await send({"type": "http.response.body", "body": payload})


async def _drain_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body"):
            break
    return b"".join(chunks)


def _content_type(start: dict[str, Any]) -> str:
    for k, v in start.get("headers", []):
        if k.decode().lower() == "content-type":
            return v.decode()
    return "application/json"


async def _send_json(
    send: Send, status: int, payload: dict[str, Any], content_type: str = "application/json"
) -> None:
    await _send_raw(send, status, json.dumps(payload), content_type)


async def _send_raw(send: Send, status: int, body: str, content_type: str) -> None:
    encoded = body.encode()
    headers = [(b"content-type", content_type.encode()), (b"content-length", str(len(encoded)).encode())]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": encoded})
