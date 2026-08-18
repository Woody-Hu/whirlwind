"""Gateway integration: REST + SSE + MCP + cron over the real all-in-one runtime.

The runtime's app runs under a real uvicorn server on a real port (lifespan
wires hostlet, wheel, SessionManager, CronScheduler). Sandboxes are real
echo-image subprocesses; LLM relay egress goes to the shared local upstream.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from argus.runtime import ArgusRuntime, RuntimeConfig

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
async def base_url(tmp_path: Path, llm_upstream: str) -> str:
    runtime = ArgusRuntime(
        RuntimeConfig(
            data_dir=tmp_path / "runtime",
            repo_root=REPO_ROOT,
            api_key_env="ARGUS_TEST_KEY",
            llm_upstream=llm_upstream,
        )
    )
    server = uvicorn.Server(uvicorn.Config(runtime.app, host="127.0.0.1", port=0, log_level="warning"))
    task = asyncio.get_running_loop().create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started
    port = int(server.servers[0].sockets[0].getsockname()[1])  # type: ignore[index]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    await asyncio.wait_for(task, timeout=10)


@pytest.fixture
async def gateway(base_url: str) -> httpx.AsyncClient:
    async with httpx.AsyncClient(base_url=base_url, timeout=60.0) as client:
        yield client


async def _wait_until(predicate, timeout_s: float = 20.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(interval)
    return False


async def _build_echo_image(client: httpx.AsyncClient) -> None:
    response = await client.post("/images/echo/build")
    assert response.status_code == 200, response.text


async def _create_echo_agent(client: httpx.AsyncClient, name: str, extra: dict | None = None) -> dict:
    version = {
        "harness": "echo",
        "image_ref": "echo",
        "model_config_decl": {"provider": "deepseek-official", "model": "deepseek-chat"},
    }
    if extra:
        version.update(extra)
    response = await client.post("/agents", json={"name": name, "version": version})
    assert response.status_code == 200, response.text
    return response.json()


async def _run_turn_to_end(client: httpx.AsyncClient, session_id: str, text: str) -> list[dict]:
    """Send a turn and poll the durable log until turn/end arrives."""
    response = await client.post(f"/sessions/{session_id}/turns", json={"text": text})
    assert response.status_code == 200, response.text

    async def done() -> bool:
        events = await client.get(f"/sessions/{session_id}/events")
        return any(e["type"] == "turn/end" for e in events.json())

    assert await _wait_until(done), "turn never completed"
    response = await client.get(f"/sessions/{session_id}/events")
    return response.json()


# ---------------------------------------------------------------------- tests


@pytest.mark.asyncio
async def test_rest_agent_session_lifecycle(gateway: httpx.AsyncClient) -> None:
    await _build_echo_image(gateway)
    created = await _create_echo_agent(gateway, "rest-agent")
    assert created["agent"]["name"] == "rest-agent"
    assert created["agent"]["default_version_id"] == created["version"]["id"]

    # duplicate name is a conflict
    dup = await gateway.post("/agents", json={"name": "rest-agent", "version": {"harness": "echo", "image_ref": "echo"}})
    assert dup.status_code == 409

    # by-name and by-id lookup
    by_name = await gateway.get("/agents/rest-agent")
    assert by_name.status_code == 200
    by_id = await gateway.get(f"/agents/{created['agent']['id']}")
    assert by_id.status_code == 200 and len(by_id.json()["versions"]) == 1

    # session + turn over REST
    session = (await gateway.post("/sessions", json={"agent_name": "rest-agent"})).json()
    events = await _run_turn_to_end(gateway, session["id"], "hello gateway")
    types = [e["type"] for e in events]
    assert types[0] == "turn/start" and types[-1] == "turn/end"
    assert "assistant/message" in types

    # statuses on the wire match the session record
    live = (await gateway.get(f"/sessions/{session['id']}")).json()
    assert live["status"] in ("idle", "running")

    # 404 + error envelope shape
    missing = await gateway.get("/sessions/ses_missing")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "argus/not-found"


@pytest.mark.asyncio
async def test_sse_stream_and_replay(base_url: str, gateway: httpx.AsyncClient) -> None:
    await _build_echo_image(gateway)
    await _create_echo_agent(gateway, "sse-agent")
    session = (await gateway.post("/sessions", json={"agent_name": "sse-agent"})).json()

    # live stream: subscribe first, then fire the turn
    collected: list[dict] = []

    async def consume() -> None:
        async with httpx.AsyncClient(base_url=base_url, timeout=None) as client:
            async with client.stream("GET", f"/sessions/{session['id']}/stream") as response:
                assert response.status_code == 200
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    event = json.loads(line[6:])
                    collected.append(event)
                    if event["type"] == "turn/end":
                        return

    consumer = asyncio.get_running_loop().create_task(consume())
    await _run_turn_to_end(gateway, session["id"], "stream me")
    await asyncio.wait_for(consumer, timeout=30)

    seqs = [e["seq"] for e in collected]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)  # ordered, no dups
    assert collected[0]["type"] == "turn/start"
    assert collected[-1]["type"] == "turn/end"

    # replay: Last-Event-ID resumes from the durable log (no live turn needed)
    replayed: list[dict] = []
    deadline = time.monotonic() + 20
    async with httpx.AsyncClient(base_url=base_url, timeout=None) as client:
        async with client.stream(
            "GET",
            f"/sessions/{session['id']}/stream",
            headers={"Last-Event-ID": str(seqs[1])},
        ) as response:
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    replayed.append(json.loads(line[6:]))
                    if len(replayed) >= len(seqs) - 2:
                        break
                if time.monotonic() > deadline:
                    pytest.fail(f"replay stalled after {len(replayed)} events")
    assert [e["seq"] for e in replayed] == seqs[2:]


@pytest.mark.asyncio
async def test_mcp_gateway_tools_and_calls(gateway: httpx.AsyncClient) -> None:
    await _build_echo_image(gateway)
    created = await _create_echo_agent(
        gateway,
        "mcp-agent",
        extra={
            "seam_bindings": [
                {"seam": "fs.v1", "provider": "sandbox-fs", "consumers": [{"harness": "*"}]},
                {"seam": "shell.v1", "provider": "sandbox-bash", "consumers": [{"harness": "echo"}]},
            ]
        },
    )
    version_id = created["version"]["id"]

    def rpc(method: str, params: dict | None = None, msg_id: int = 1):
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": method,
            **({"params": params} if params is not None else {}),
        }

    # initialize
    response = await gateway.post(f"/mcp/{version_id}", json=rpc("initialize"))
    assert response.status_code == 200
    body = response.json()
    assert body["result"]["serverInfo"]["name"] == "argus-mcp"

    # tools/list: only seams with a "*" consumer are exposed
    response = await gateway.post(f"/mcp/{version_id}", json=rpc("tools/list", {}))
    tools = response.json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == {"fs_read", "fs_write"}  # shell.v1 has no "*" consumer

    # tools/call does real work: write then read back via the version scratch ws
    response = await gateway.post(
        f"/mcp/{version_id}",
        json=rpc("tools/call", {"name": "fs_write", "arguments": {"path": "notes/hello.txt", "content": "mcp was here"}}),
    )
    result = response.json()["result"]
    assert result["isError"] is False and "wrote" in result["content"][0]["text"]

    response = await gateway.post(
        f"/mcp/{version_id}",
        json=rpc("tools/call", {"name": "fs_read", "arguments": {"path": "notes/hello.txt"}}),
    )
    result = response.json()["result"]
    assert result["content"][0]["text"] == "mcp was here"

    # path escapes are rejected by the executor (confinement), as MCP errors
    response = await gateway.post(
        f"/mcp/{version_id}",
        json=rpc("tools/call", {"name": "fs_read", "arguments": {"path": "../secrets"}}),
    )
    assert response.json()["result"]["isError"] is True

    # unexposed tool is an MCP error, and unknown version a JSON-RPC error
    response = await gateway.post(
        f"/mcp/{version_id}",
        json=rpc("tools/call", {"name": "shell_exec", "arguments": {"command": "echo nope"}}),
    )
    assert response.json()["result"]["isError"] is True
    response = await gateway.post("/mcp/ver_missing", json=rpc("tools/list", {}))
    assert response.json()["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_mcp_shell_exec_scoped_to_session_workspace(gateway: httpx.AsyncClient) -> None:
    await _build_echo_image(gateway)
    created = await _create_echo_agent(
        gateway,
        "mcp-shell",
        extra={"seam_bindings": [{"seam": "shell.v1", "provider": "sandbox-bash", "consumers": [{"harness": "*"}]}]},
    )
    version_id = created["version"]["id"]

    # a live session pins tool execution to that session's sandbox workspace
    session = (await gateway.post("/sessions", json={"agent_name": "mcp-shell"})).json()
    await _run_turn_to_end(gateway, session["id"], "warm up")

    response = await gateway.post(
        f"/mcp/{version_id}",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "shell_exec",
                "arguments": {"command": "pwd"},
                "_session": session["id"],
            },
        },
    )
    result = response.json()["result"]
    assert result["isError"] is False
    assert "sandboxes" in result["content"][0]["text"]  # cwd is the sandbox workspace

    # a file written via MCP shell is visible inside the same workspace
    response = await gateway.post(
        f"/mcp/{version_id}",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "shell_exec",
                "arguments": {"command": "echo via-mcp > from-mcp.txt"},
                "_session": session["id"],
            },
        },
    )
    assert response.json()["result"]["isError"] is False

    # _session pointing at a session without a sandbox is an MCP error
    bare = (await gateway.post("/sessions", json={"agent_name": "mcp-shell"})).json()
    response = await gateway.post(
        f"/mcp/{version_id}",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "shell_exec", "arguments": {"command": "pwd"}, "_session": bare["id"]},
        },
    )
    assert response.json()["result"]["isError"] is True


@pytest.mark.asyncio
async def test_cron_add_trigger_and_scheduled_fire(gateway: httpx.AsyncClient) -> None:
    await _build_echo_image(gateway)
    created = await _create_echo_agent(gateway, "cron-agent")

    # boundary validation: malformed cron and unknown agent are rejected
    bad = await gateway.post(
        "/crons",
        json={"agent_id": created["agent"]["id"], "schedule": "not-a-cron", "input_template": "hi"},
    )
    assert bad.status_code == 400 and "cron" in bad.json()["error"]["message"]
    missing = await gateway.post(
        "/crons",
        json={"agent_id": "agt_missing", "schedule": "* * * * *", "input_template": "hi"},
    )
    assert missing.status_code == 404

    job = (
        await gateway.post(
            "/crons",
            json={
                "agent_id": created["agent"]["id"],
                "schedule": "*/5 * * * *",
                "input_template": "cron ping",
            },
        )
    ).json()
    assert job["id"]

    # manual trigger runs the exact scheduled path: fresh session + real turn
    result = (await gateway.post(f"/crons/{job['id']}/trigger")).json()
    assert result["session_id"] and result["message_id"]
    events = await _wait_for_turn(gateway, result["session_id"])
    assert events

    # listing + delete
    jobs = (await gateway.get("/crons")).json()
    assert any(j["id"] == job["id"] for j in jobs)
    assert (await gateway.delete(f"/crons/{job['id']}")).status_code == 200
    assert not any(j["id"] == job["id"] for j in (await gateway.get("/crons")).json())


async def _wait_for_turn(client: httpx.AsyncClient, session_id: str) -> list[dict]:
    async def done() -> list[dict] | None:
        response = await client.get(f"/sessions/{session_id}/events")
        events = response.json()
        return events if any(e["type"] == "turn/end" for e in events) else None

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        events = await done()
        if events is not None:
            return events
        await asyncio.sleep(0.1)
    return []


@pytest.mark.asyncio
async def test_session_suspend_resume_over_rest(gateway: httpx.AsyncClient) -> None:
    """REST-level suspend/resume: snapshot taken, session suspended, resumed
    sandbox answers a new turn on the same session."""
    await _build_echo_image(gateway)
    await _create_echo_agent(gateway, "suspend-agent")
    session = (await gateway.post("/sessions", json={"agent_name": "suspend-agent"})).json()
    events = await _run_turn_to_end(gateway, session["id"], "before suspend")
    assert events[-1]["type"] == "turn/end"

    # wait for the idle status to land before suspending
    async def idle() -> bool:
        return (await gateway.get(f"/sessions/{session['id']}")).json()["status"] == "idle"

    assert await _wait_until(idle), "session never went idle"

    suspended = (await gateway.post(f"/sessions/{session['id']}/suspend")).json()
    assert suspended["status"] == "suspended"
    assert suspended["bound_sandbox_id"] is None

    # a suspended session refuses turns until resumed
    rejected = await gateway.post(f"/sessions/{session['id']}/turns", json={"text": "no"})
    assert rejected.status_code == 409

    resumed = (await gateway.post(f"/sessions/{session['id']}/resume")).json()
    assert resumed["status"] == "running"
    assert resumed["bound_sandbox_id"]

    # the restored session continues: seq keeps growing on the same durable log
    before = len((await gateway.get(f"/sessions/{session['id']}/events")).json())
    response = await gateway.post(f"/sessions/{session['id']}/turns", json={"text": "after resume"})
    assert response.status_code == 200, response.text

    async def grew() -> bool:
        events = (await gateway.get(f"/sessions/{session['id']}/events")).json()
        return len(events) > before and events[-1]["type"] == "turn/end"

    assert await _wait_until(grew), "post-resume turn never completed"
    closed = (await gateway.post(f"/sessions/{session['id']}/close")).json()
    assert closed["status"] == "closed"
