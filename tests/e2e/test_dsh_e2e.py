"""E2E acceptance: native DeepSeek Harness image on the real DeepSeek API.

Gated behind ARGUS_E2E=1 + DEEPSEEK_API_KEY (ADR-0001: e2e tests touch the
paid upstream; the default suite must not). Everything here is real:

- the dsh image is a real venv pip-installed from the deepseek-harness
  checkout (refs/deepseek-harness with its built single-file exe),
- the sandbox runs the real dsh-jsonrpc-agent exe as a child of the real
  SandboxAgent sidecar,
- turns flow gateway -> SessionManager -> Hostlet -> sidecar -> dsh, and LLM
  calls leave through the keyless in-sandbox relay -> Hostlet SecretRelay
  (which alone holds the credential) -> api.deepseek.com.

macOS (m-series) note: this file is platform-neutral; build the runtime exe
once in the checkout (`pnpm exec tsx scripts/build-exe-for-python-sdk.ts`)
and the same tests run unchanged.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from argus.imaging import LocalRegistry, dsh_image_build
from argus.runtime import ArgusRuntime, RuntimeConfig

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    os.environ.get("ARGUS_E2E") != "1" or not os.environ.get("DEEPSEEK_API_KEY"),
    reason="set ARGUS_E2E=1 and DEEPSEEK_API_KEY to run paid-upstream e2e tests",
)

MODEL_DECL = {"provider": "deepseek-official", "model": "deepseek-v4-flash"}
TURN_TIMEOUT_S = 240.0  # real LLM + tool round-trips; no hard CI line, regression-compare only


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the dsh image once for the module (a real multi-minute pip build)."""
    directory = tmp_path_factory.mktemp("dsh-e2e")
    registry = LocalRegistry(directory / "images")
    ref = asyncio.run(registry.register(dsh_image_build(REPO_ROOT)))
    assert ref.startswith("dsh@")
    return directory


class _Stack:
    """One runtime + real uvicorn server per test; image reused from data_dir."""

    def __init__(self, runtime: ArgusRuntime, client: httpx.AsyncClient) -> None:
        self.runtime = runtime
        self.client = client

    async def workspace(self, session_id: str) -> Path:
        session = await self.runtime.store.get_session(session_id)
        assert session is not None and session.bound_sandbox_id
        sandbox = await self.runtime.store.get_sandbox(session.bound_sandbox_id)
        assert sandbox is not None and sandbox.workspace
        return Path(sandbox.workspace)

    async def wait_idle(self, session_id: str, timeout_s: float = TURN_TIMEOUT_S) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            session = await self.runtime.store.get_session(session_id)
            if session is not None and session.status.value == "idle":
                return
            await asyncio.sleep(0.2)
        pytest.fail(f"session {session_id} never returned to idle")

    async def turn(self, session_id: str, text: str) -> list[dict]:
        prior = (await self.client.get(f"/sessions/{session_id}/events")).json()
        start_seq = prior[-1]["seq"] if prior else 0
        response = await self.client.post(f"/sessions/{session_id}/turns", json={"text": text})
        assert response.status_code == 200, response.text
        deadline = time.monotonic() + TURN_TIMEOUT_S
        while time.monotonic() < deadline:
            events = (await self.client.get(f"/sessions/{session_id}/events?from_seq={start_seq + 1}")).json()
            if any(e["type"] == "turn/end" for e in events):
                return events
            await asyncio.sleep(0.3)
        pytest.fail(f"turn never completed within {TURN_TIMEOUT_S}s")

    @staticmethod
    def assistant_text(events: list[dict]) -> str:
        chunks: list[str] = []
        for event in events:
            if event["type"] != "assistant/message":
                continue
            message = event["data"].get("message", {})
            for block in message.get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    chunks.append(str(block.get("text", "")))
        return "\n".join(chunks)


@pytest.fixture
async def stack(data_dir: Path) -> _Stack:
    # same data_dir across tests: the module-scoped dsh image is resolved, not rebuilt
    runtime = ArgusRuntime(RuntimeConfig(data_dir=data_dir, repo_root=REPO_ROOT))
    server = uvicorn.Server(uvicorn.Config(runtime.app, host="127.0.0.1", port=0, log_level="warning"))
    task = asyncio.get_running_loop().create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started
    try:
        base = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"  # type: ignore[index]
        async with httpx.AsyncClient(base_url=base, timeout=300.0) as client:
            yield _Stack(runtime, client)
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=15)


async def _create_dsh_agent(
    stack: _Stack,
    name: str,
    *,
    seams: list[dict] | None = None,
    skills: list[dict] | None = None,
) -> str:
    version: dict = {"harness": "dsh", "image_ref": "dsh", "model_config_decl": MODEL_DECL}
    if seams:
        version["seam_bindings"] = seams
    if skills:
        version["skill_refs"] = skills
    response = await stack.client.post("/agents", json={"name": name, "version": version})
    assert response.status_code == 200, response.text
    return response.json()["agent"]["id"]


@pytest.mark.asyncio
@pytest.mark.timeout(900)
async def test_dsh_turn_with_native_shell_and_fs_seams(stack: _Stack) -> None:
    """The acceptance path: dsh image mounted, seams configured, real task done.

    The model must actually use the injected bash tool (the proof file only
    exists if a real shell ran inside the sandbox) and reply through the
    relayed DeepSeek API.
    """
    await _create_dsh_agent(
        stack,
        "dsh-e2e-tools",
        seams=[
            {"seam": "shell.v1", "provider": "sandbox-bash"},
            {"seam": "fs.v1", "provider": "sandbox-fs"},
        ],
    )
    session = (await stack.client.post("/sessions", json={"agent_name": "dsh-e2e-tools"})).json()

    events = await stack.turn(
        session["id"],
        "Use the bash tool to run exactly this command: echo e2e-ok > proof.txt "
        "Then reply with the single word DONE.",
    )
    assert "DONE" in _Stack.assistant_text(events)
    workspace = await stack.workspace(session["id"])
    assert (workspace / "proof.txt").read_text().strip() == "e2e-ok"
    await stack.wait_idle(session["id"])


@pytest.mark.asyncio
@pytest.mark.timeout(900)
async def test_dsh_skill_configured_into_agent(stack: _Stack) -> None:
    """Skills upload -> staging -> dsh skill-filesystem -> model actually uses it."""
    skill_md = (
        "---\n"
        "name: codeword\n"
        "description: Reveals the secret codeword when the user asks for it\n"
        "---\n\n"
        "The secret codeword is pine-tree. When the user asks for the codeword, "
        "reply with exactly the word pine-tree and nothing else."
    )
    response = await stack.client.post(
        "/skills/codeword/1.0.0", content=skill_md, headers={"content-type": "text/markdown"}
    )
    assert response.status_code == 200, response.text

    await _create_dsh_agent(
        stack,
        "dsh-e2e-skill",
        seams=[{"seam": "fs.v1", "provider": "sandbox-fs"}],
        skills=[{"name": "codeword", "version": "1.0.0"}],
    )
    session = (await stack.client.post("/sessions", json={"agent_name": "dsh-e2e-skill"})).json()

    events = await stack.turn(session["id"], "What is the secret codeword?")
    assert "pine-tree" in _Stack.assistant_text(events).lower()

    # injection evidence: staged skill + rendered native config in the sandbox workspace
    workspace = await stack.workspace(session["id"])
    staged = workspace / ".argus" / "skills" / "codeword" / "SKILL.md"
    assert staged.is_file() and "pine-tree" in staged.read_text()
    cordis = (workspace / ".argus" / "cordis.yml").read_text()
    assert "@deepseek-ai/dsh-skill-filesystem" in cordis
    assert ".argus/skills" in cordis
    await stack.wait_idle(session["id"])


@pytest.mark.asyncio
@pytest.mark.timeout(900)
async def test_dsh_snapshot_resume_continuity(stack: _Stack) -> None:
    """M2-e acceptance: suspend (data snapshot + teardown) -> resume (fresh
    sandbox seeded from the snapshot) and the dsh conversation continues.

    Continuity proof: a secret planted in turn 1 must still be known after the
    round-trip — the model can only answer from dsh's persisted session state
    (.argus/sessions JSONL restored into the new sandbox), never from argus
    memory (the process sandbox is destroyed between the two turns).
    """
    await _create_dsh_agent(stack, "dsh-e2e-suspend", seams=[{"seam": "fs.v1", "provider": "sandbox-fs"}])
    session = (await stack.client.post("/sessions", json={"agent_name": "dsh-e2e-suspend"})).json()

    secret = f"mango-{int(time.time())}"
    events = await stack.turn(
        session["id"],
        f"Remember this secret word for the rest of our conversation: {secret}. "
        "Just reply ACK.",
    )
    assert "ACK" in _Stack.assistant_text(events)
    await stack.wait_idle(session["id"])

    first_sandbox = (await stack.client.get(f"/sessions/{session['id']}")).json()["bound_sandbox_id"]
    before = await stack.workspace(session["id"])  # gone after suspend; hold the path only
    suspend_start = time.perf_counter()
    suspended = (await stack.client.post(f"/sessions/{session['id']}/suspend")).json()
    suspend_s = time.perf_counter() - suspend_start
    assert suspended["status"] == "suspended"
    assert suspended["bound_sandbox_id"] is None
    assert not before.exists(), "suspend must tear the sandbox workspace down (data lives in the snapshot)"

    resume_start = time.perf_counter()
    resumed = (await stack.client.post(f"/sessions/{session['id']}/resume")).json()
    resume_s = time.perf_counter() - resume_start
    assert resumed["status"] == "running"
    assert resumed["bound_sandbox_id"] != first_sandbox, "resume must boot a fresh sandbox"

    events = await stack.turn(
        session["id"],
        "What was the secret word I told you? Reply with the word only.",
    )
    if secret not in _Stack.assistant_text(events):
        for e in events:
            print(f"EVENT seq={e['seq']} {e['type']}: {e['data']}")
    assert secret in _Stack.assistant_text(events)
    print(f"\nsuspend={suspend_s:.2f}s resume={resume_s:.2f}s (real dsh + DeepSeek)")
    await stack.wait_idle(session["id"])
