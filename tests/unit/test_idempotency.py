"""Idempotency middleware semantics (ADR-0005 D3) against a scratch app.

Deterministic at the ASGI level via httpx's ASGITransport — the same code
path a real uvicorn server drives. Every claim in the ADR has a test:
replay identity, in-flight 409, body-mismatch 422, 5xx key release, TTL
expiry, route scoping, passthrough without the header.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI

from whirlwind.gateway.idempotency import IdempotencyMiddleware
from whirlwind.storage.memory import MemoryKVStore


def _scratch_app(
    kv: MemoryKVStore,
    *,
    ttl_s: float = 86400.0,
    fail_first: int = 0,
    gate: asyncio.Event | None = None,
) -> FastAPI:
    app = FastAPI()
    app.add_middleware(IdempotencyMiddleware, kv=kv, ttl_s=ttl_s)
    state = {"created": 0, "failures": 0}

    @app.post("/widgets")
    async def create_widget(payload: dict) -> dict:
        if gate is not None:
            await gate.wait()  # hold the winner in-flight so a rival can arrive
        state["created"] += 1
        if state["created"] <= fail_first:
            state["failures"] += 1
            raise RuntimeError("boom")  # becomes a 500 via the test client
        return {"id": state["created"], "name": payload.get("name")}

    @app.delete("/widgets/{wid}")
    async def delete_widget(wid: str) -> dict:
        state["created"] -= 1
        return {"deleted": wid}

    @app.get("/widgets")
    async def list_widgets() -> dict:
        return state

    app.state.counter = state
    return app


@pytest.fixture
async def kv() -> MemoryKVStore:
    store = MemoryKVStore()
    await store.start()
    try:
        yield store
    finally:
        await store.aclose()


def _client(app: FastAPI) -> httpx.AsyncClient:
    # raise_app_exceptions=False mirrors a real server: handler crashes
    # surface as 500 responses, not client-side exceptions
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


@pytest.mark.asyncio
async def test_same_key_replays_identical_response(kv: MemoryKVStore) -> None:
    app = _scratch_app(kv)
    async with _client(app) as client:
        first = await client.post("/widgets", json={"name": "a"}, headers={"Idempotency-Key": "k1"})
        second = await client.post("/widgets", json={"name": "a"}, headers={"Idempotency-Key": "k1"})
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {"id": 1, "name": "a"}
    assert app.state.counter["created"] == 1  # executed exactly once


@pytest.mark.asyncio
async def test_no_header_means_pure_passthrough(kv: MemoryKVStore) -> None:
    app = _scratch_app(kv)
    async with _client(app) as client:
        await client.post("/widgets", json={"name": "a"})
        await client.post("/widgets", json={"name": "a"})
    assert app.state.counter["created"] == 2


@pytest.mark.asyncio
async def test_concurrent_same_key_one_winner_one_409(kv: MemoryKVStore) -> None:
    """Two truly concurrent requests: the claim winner executes (held
    in-flight by a gate), the loser sees the pending marker and gets 409 —
    never a double execution."""
    gate = asyncio.Event()
    app = _scratch_app(kv, gate=gate)
    client = _client(app)

    async def create() -> httpx.Response:
        return await client.post("/widgets", json={"name": "a"}, headers={"Idempotency-Key": "race"})

    t1 = asyncio.create_task(create())
    await asyncio.sleep(0.2)  # t1 claimed the key and is parked in the handler
    t2 = asyncio.create_task(create())
    await asyncio.sleep(0.2)  # t2 sees the pending marker
    gate.set()
    r1, r2 = await asyncio.gather(t1, t2)
    await client.aclose()
    assert {r1.status_code, r2.status_code} == {200, 409}
    assert app.state.counter["created"] == 1


@pytest.mark.asyncio
async def test_same_key_different_body_is_422(kv: MemoryKVStore) -> None:
    app = _scratch_app(kv)
    async with _client(app) as client:
        first = await client.post("/widgets", json={"name": "a"}, headers={"Idempotency-Key": "k"})
        mismatch = await client.post("/widgets", json={"name": "b"}, headers={"Idempotency-Key": "k"})
    assert first.status_code == 200
    assert mismatch.status_code == 422
    assert mismatch.json()["error"]["code"] == "whirlwind/idempotency-key-reuse"


@pytest.mark.asyncio
async def test_keys_are_scoped_per_route(kv: MemoryKVStore) -> None:
    app = _scratch_app(kv)
    async with _client(app) as client:
        created = await client.post("/widgets", json={"name": "a"}, headers={"Idempotency-Key": "shared"})
        deleted = await client.delete("/widgets/1", headers={"Idempotency-Key": "shared"})
    assert created.status_code == 200
    assert deleted.status_code == 200  # not a replay of the create response
    assert deleted.json() == {"deleted": "1"}


@pytest.mark.asyncio
async def test_5xx_releases_the_key_for_retry(kv: MemoryKVStore) -> None:
    """First attempt 500s, key is released, the retry re-executes and
    succeeds — the exact timeout-retry story this middleware exists for."""
    app = _scratch_app(kv, fail_first=1)
    async with _client(app) as client:
        first = await client.post("/widgets", json={"name": "a"}, headers={"Idempotency-Key": "retry"})
        second = await client.post("/widgets", json={"name": "a"}, headers={"Idempotency-Key": "retry"})
    assert first.status_code == 500
    assert second.status_code == 200  # re-executed, not replayed
    assert second.json() == {"id": 2, "name": "a"}
    assert app.state.counter["created"] == 2  # ran twice, key never poisoned


@pytest.mark.asyncio
async def test_replay_expires_with_ttl(kv: MemoryKVStore) -> None:
    app = _scratch_app(kv, ttl_s=0.05)
    async with _client(app) as client:
        first = await client.post("/widgets", json={"name": "a"}, headers={"Idempotency-Key": "ttl"})
        await asyncio.sleep(0.08)
        second = await client.post("/widgets", json={"name": "a"}, headers={"Idempotency-Key": "ttl"})
    assert first.json() == {"id": 1, "name": "a"}
    assert second.json() == {"id": 2, "name": "a"}  # fresh execution post-expiry
    assert app.state.counter["created"] == 2
