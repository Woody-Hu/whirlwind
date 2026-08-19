"""Shared integration fixtures: a real local LLM upstream for relay tests."""

from __future__ import annotations

import asyncio

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

API_KEY_SENTINEL = "sk-test-secret-do-not-leak"


@pytest.fixture
async def llm_upstream(monkeypatch: pytest.MonkeyPatch) -> str:
    """A real local LLM-ish HTTP service: 401 without the Bearer key, 200 with.

    Downstream tests point the Hostlet SecretRelay at this base URL; the
    fixture asserts (on teardown) that the key was actually exercised.
    """
    monkeypatch.setenv("ARGUS_TEST_KEY", API_KEY_SENTINEL)
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
