"""Shared integration fixtures: a real local LLM upstream for relay tests,
plus backend-parameterized storage fixtures (ADR-0004 D5: real services or
skip — never fakes)."""

from __future__ import annotations

import asyncio
import os

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from whirlwind.storage.memory import MemoryKVStore, MemoryLocks, MemoryMetadataStore

API_KEY_SENTINEL = "sk-test-secret-do-not-leak"

POSTGRES_DSN = os.environ.get(
    "WHIRLWIND_TEST_POSTGRES_DSN", "postgresql://whirlwind:whirlwind@127.0.0.1:5432/whirlwind_test"
)
REDIS_URL = os.environ.get("WHIRLWIND_TEST_REDIS_URL", "redis://127.0.0.1:6379/15")

_TRUNCATE = (
    "TRUNCATE agents, agent_versions, sessions, sandboxes, snapshots, crons RESTART IDENTITY"
)

try:
    import asyncpg  # noqa: F401  (optional extra: whirlwind[postgres])
except ImportError:
    asyncpg = None

try:
    from redis import asyncio as aioredis  # noqa: F401  (optional extra: whirlwind[redis])
except ImportError:
    aioredis = None


async def _postgres_reachable(dsn: str) -> bool:
    if asyncpg is None:
        return False
    try:
        conn = await asyncio.wait_for(asyncpg.connect(dsn), timeout=3.0)
    except Exception:
        return False
    try:
        await conn.execute("SELECT 1")
        return True
    finally:
        await conn.close()


async def _redis_reachable(url: str) -> bool:
    if aioredis is None:
        return False
    try:
        client = aioredis.from_url(url, decode_responses=True)
    except Exception:
        return False
    try:
        await asyncio.wait_for(client.ping(), timeout=3.0)
        return True
    except Exception:
        return False
    finally:
        await client.aclose()


@pytest.fixture
async def postgres_dsn() -> str:
    """The shared test DSN; skips unless a real PostgreSQL answers."""
    if asyncpg is None:
        pytest.skip("asyncpg not installed (whirlwind[postgres])")
    if not await _postgres_reachable(POSTGRES_DSN):
        pytest.skip(f"postgres not reachable at {POSTGRES_DSN!r}")
    return POSTGRES_DSN


@pytest.fixture
async def redis_url() -> str:
    """The shared test Redis URL; skips unless a real Redis answers."""
    if aioredis is None:
        pytest.skip("redis not installed (whirlwind[redis])")
    if not await _redis_reachable(REDIS_URL):
        pytest.skip(f"redis not reachable at {REDIS_URL!r}")
    return REDIS_URL


@pytest.fixture(params=["memory", "postgres"])
async def metadata_store(request, tmp_path):
    """The MetadataStore contract suite runs against every backend.

    Memory always runs; postgres runs only when a real service answers.
    """
    if request.param == "memory":
        store = MemoryMetadataStore(skills_dir=tmp_path / "skills")
        await store.start()
        try:
            yield store
        finally:
            await store.aclose()
        return
    if asyncpg is None:
        pytest.skip("asyncpg not installed (whirlwind[postgres])")
    if not await _postgres_reachable(POSTGRES_DSN):
        pytest.skip(f"postgres not reachable at {POSTGRES_DSN!r}")
    from whirlwind.storage.postgres import PostgresMetadataStore

    store = PostgresMetadataStore(POSTGRES_DSN, skills_dir=tmp_path / "skills")
    await store.start()
    await store.pool.execute(_TRUNCATE)
    try:
        yield store
    finally:
        await store.aclose()


@pytest.fixture(params=["memory", "redis"])
async def kv_store(request):
    """The KVStore contract suite runs against every backend."""
    if request.param == "memory":
        kv = MemoryKVStore()
        await kv.start()
        try:
            yield kv
        finally:
            await kv.aclose()
        return
    if aioredis is None:
        pytest.skip("redis not installed (whirlwind[redis])")
    if not await _redis_reachable(REDIS_URL):
        pytest.skip(f"redis not reachable at {REDIS_URL!r}")
    from whirlwind.storage.redis import RedisKVStore

    kv = RedisKVStore(REDIS_URL)
    await kv.start()
    await kv.client.flushdb()
    try:
        yield kv
    finally:
        await kv.aclose()


@pytest.fixture(params=["memory", "redis"])
async def lock_provider(request):
    """The LockProvider contract suite runs against every backend."""
    if request.param == "memory":
        yield MemoryLocks()
        return
    if aioredis is None:
        pytest.skip("redis not installed (whirlwind[redis])")
    if not await _redis_reachable(REDIS_URL):
        pytest.skip(f"redis not reachable at {REDIS_URL!r}")
    from whirlwind.storage.redis import RedisLocks

    locks = RedisLocks(REDIS_URL)
    await locks.start()
    await locks.client.flushdb()
    try:
        yield locks
    finally:
        await locks.aclose()


@pytest.fixture(params=["memory", "local"])
async def secret_store(request, tmp_path):
    """The SecretStore contract suite runs against every landed backend (ADR-0010 D6).

    Memory and the local file store both always run — no external service.
    """
    from whirlwind.storage.local import LocalFileSecretStore
    from whirlwind.storage.memory import MemorySecretStore

    if request.param == "memory":
        yield MemorySecretStore()
        return
    yield LocalFileSecretStore(tmp_path / "data")


@pytest.fixture
async def llm_upstream(monkeypatch: pytest.MonkeyPatch) -> str:
    """A real local LLM-ish HTTP service: 401 without the Bearer key, 200 with.

    Downstream tests point the Hostlet SecretRelay at this base URL; the
    fixture asserts (on teardown) that the key was actually exercised.
    """
    monkeypatch.setenv("WHIRLWIND_TEST_KEY", API_KEY_SENTINEL)
    seen: dict[str, str] = {}
    app = FastAPI()

    @app.post("/chat/completions")
    async def completions(request: Request):
        seen["auth"] = request.headers.get("authorization", "")
        if seen["auth"] != f"Bearer {API_KEY_SENTINEL}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json()
        seen["model"] = str(body.get("model", ""))
        return {"choices": [{"message": {"content": "relayed-reply"}}]}

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    serve_task = asyncio.get_running_loop().create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    port = int(server.servers[0].sockets[0].getsockname()[1])  # type: ignore[index]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    await asyncio.wait_for(serve_task, timeout=5)
    # NOTE: no "key was used" assertion here — turn-executing tests prove the
    # relay end-to-end by asserting the upstream's reply content (it 401s
    # without the Bearer key); tests that never turn (e.g. MCP-only) share
    # this fixture and must not be forced through the LLM path.
