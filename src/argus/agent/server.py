"""SandboxAgent: the first process inside a sandbox (ADR D4).

Responsibilities (the architecture's sidecar set, M1 subset):
- ResourceInjector: reads the injection manifest + runtime plan the Hostlet
  wrote into the workspace and launches the harness with exactly that env
- ControlAgent: HTTP face on 127.0.0.1:{ARGUS_AGENT_PORT} — /health, /turn, /stop
- EventTap: forwards harness notifications to the Hostlet ingest endpoint
- LLM Relay jump box: /relay/llm/* forwards to the Hostlet SecretRelay;
  no credential ever exists inside the sandbox

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
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from argus.harness.protocol import (
    HarnessRpc,
    content_blocks,
    parse_session_event,
    parse_session_status,
)


class AgentState:
    def __init__(self) -> None:
        self.rpc: HarnessRpc | None = None
        self.harness_info: dict[str, Any] = {}
        self.session_status: dict[str, str] = {}
        self.hostlet = os.environ.get("ARGUS_HOSTLET_URL", "").rstrip("/")
        self.sandbox_id = os.environ.get("ARGUS_SANDBOX_ID", "")
        self.client = httpx.AsyncClient(timeout=120.0)

    @property
    def llm_upstream(self) -> str:
        return f"{self.hostlet}/secret/llm"


STATE = AgentState()


async def _post_events(payload: dict[str, Any]) -> None:
    if not STATE.hostlet:
        return
    try:
        await STATE.client.post(f"{STATE.hostlet}/ingest/events", json=payload)
    except httpx.HTTPError:
        pass  # the durable session log remains the source of truth; drops tolerated


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


@asynccontextmanager
async def _lifespan(_: FastAPI):
    await boot()
    yield
    if STATE.rpc is not None:
        await STATE.rpc.close()
    await STATE.client.aclose()


AGENT = FastAPI(lifespan=_lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@AGENT.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": STATE.rpc is not None and not STATE.rpc.closed,
        "harness": STATE.harness_info,
        "sessions": STATE.session_status,
    }


@AGENT.post("/turn")
async def turn(request: Request) -> JSONResponse:
    body = await request.json()
    if STATE.rpc is None or STATE.rpc.closed:
        return JSONResponse({"error": "harness not running"}, status_code=503)
    blocks = body.get("contentBlocks")
    if blocks is None:
        blocks = content_blocks(str(body.get("text", "")))
    result = await STATE.rpc.request(
        "session/prompt", {"sessionId": str(body.get("sessionId", "")), "contentBlocks": blocks}
    )
    return JSONResponse({"messageId": result.get("messageId", "")})


@AGENT.post("/stop")
async def stop() -> dict[str, Any]:
    if STATE.rpc is not None:
        await STATE.rpc.close()
        STATE.rpc = None
    return {"ok": True}


@AGENT.api_route("/relay/llm/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def relay_llm(path: str, request: Request) -> JSONResponse:
    """Keyless jump box: the Hostlet SecretRelay owns the credential."""
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "authorization")
    }
    response = await STATE.client.request(
        request.method, f"{STATE.llm_upstream}/{path}", headers=headers, content=await request.body()
    )
    if response.headers.get("content-type", "").startswith("application/json"):
        return JSONResponse(response.json(), status_code=response.status_code)
    return JSONResponse({"raw": response.text}, status_code=response.status_code)


def main() -> None:
    uvicorn.run(
        AGENT,
        host="127.0.0.1",
        port=int(os.environ.get("ARGUS_AGENT_PORT", "8000")),
        log_level="warning",
    )


if __name__ == "__main__":
    main()
