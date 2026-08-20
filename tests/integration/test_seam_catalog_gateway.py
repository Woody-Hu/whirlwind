"""Seam catalog + harness bundle integration (ADR-0011 D4-D7).

Real runtime, real uvicorn, real echo-image subprocesses — no stubs. Covers:
template/instance registration over REST, version admission (bundle supply,
eager instance validation, 422 on disagreement), provision-time resolution
(manifest provenance + substituted policy + bundle env overlay), and the
ConfigMap liveness of instance references.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from whirlwind.runtime import WhirlwindRuntime, RuntimeConfig

REPO_ROOT = Path(__file__).resolve().parents[2]

TEMPLATE_BODY = {
    "name": "workspace-fs",
    "seam": "fs.v1",
    "provider": "sandbox-fs",
    "policy": {"mode": "${mode}"},
    "params": [
        {"name": "mode", "type": "string", "required": True},
    ],
    "description": "parameterized workspace filesystem",
}


@pytest.fixture
async def env(tmp_path: Path, llm_upstream: str):
    """A running runtime whose data_dir is exposed for workspace inspection."""
    data_dir = tmp_path / "runtime"
    runtime = WhirlwindRuntime(
        RuntimeConfig(
            data_dir=data_dir,
            repo_root=REPO_ROOT,
            api_key_env="WHIRLWIND_TEST_KEY",
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
    base_url = f"http://127.0.0.1:{int(server.servers[0].sockets[0].getsockname()[1])}"  # type: ignore[index]
    async with httpx.AsyncClient(base_url=base_url, timeout=60.0) as client:
        yield client, data_dir
    server.should_exit = True
    await asyncio.wait_for(task, timeout=10)


async def _wait_turn_end(client: httpx.AsyncClient, session_id: str, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        events = (await client.get(f"/sessions/{session_id}/events")).json()
        if any(e["type"] == "turn/end" for e in events):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("turn never completed")


def _read_workspace_doc(data_dir: Path, session_id: str, filename: str) -> dict:
    """A provisioned doc (manifest.json / runtime.json) of the sandbox that
    served `session_id` — the persisted execution truth (ADR-0011 D2)."""
    for manifest_path in sorted((data_dir / "sandboxes").glob("*/ws/.whirlwind/manifest.json")):
        if json.loads(manifest_path.read_text()).get("session_id") == session_id:
            return json.loads((manifest_path.parent / filename).read_text())
    raise AssertionError(f"no sandbox workspace provisioned for session {session_id}")


async def _run_echo_turn(client: httpx.AsyncClient, agent_name: str, text: str) -> str:
    session = (await client.post("/sessions", json={"agent_name": agent_name})).json()
    response = await client.post(f"/sessions/{session['id']}/turns", json={"text": text})
    assert response.status_code == 200, response.text
    await _wait_turn_end(client, session["id"])
    return session["id"]


# ---------------------------------------------------------------------- tests


async def test_bundle_and_instances_flow_to_manifest(env) -> None:
    client, data_dir = env
    # catalog setup over REST
    template = (await client.put("/seam-templates/workspace-fs", json=TEMPLATE_BODY)).json()
    assert template["name"] == "workspace-fs"
    instance = (
        await client.put(
            "/seam-instances/prod-fs",
            json={"name": "prod-fs", "template": "workspace-fs", "params": {"mode": "read-only"}},
        )
    ).json()
    assert instance["params"] == {"mode": "read-only"}

    assert (await client.post("/images/echo/build")).status_code == 200
    created = (
        await client.post(
            "/agents",
            json={
                "name": "bundled-agent",
                "version": {
                    "version": "1.0.0",
                    "harness_bundle": "echo",
                    "seam_instances": ["prod-fs"],
                },
            },
        )
    )
    assert created.status_code == 200, created.text
    version = created.json()["version"]
    # denormalized snapshot of the bundle at admission (ADR-0011 D4)
    assert version["harness"] == "echo" and version["image_ref"] == "echo"
    assert version["harness_bundle"] == "echo" and version["seam_instances"] == ["prod-fs"]

    session_id = await _run_echo_turn(client, "bundled-agent", "hello bundle")
    manifest = _read_workspace_doc(data_dir, session_id, "manifest.json")
    assert manifest["harness"] == "echo"
    (binding,) = manifest["seams"]
    assert binding["seam"] == "fs.v1"
    assert binding["policy"]["mode"] == "read-only"  # substituted, not ${mode}
    assert binding["instance"] == "prod-fs"  # provenance


async def test_admission_rejections(env) -> None:
    client, _ = env
    await client.put("/seam-templates/workspace-fs", json=TEMPLATE_BODY)
    await client.put(
        "/seam-instances/prod-fs",
        json={"name": "prod-fs", "template": "workspace-fs", "params": {"mode": "read-only"}},
    )
    await client.post("/images/echo/build")

    def version_payload(**overrides):
        body = {"harness_bundle": "echo", "seam_instances": ["prod-fs"]}
        body.update(overrides)
        return {"name": "reject-agent", "version": body}

    # unknown bundle: 404
    unknown = await client.post("/agents", json=version_payload(harness_bundle="nope"))
    assert unknown.status_code == 404
    # explicit value disagreeing with the bundle: 422, never a silent override
    disagree = await client.post("/agents", json=version_payload(harness="dsh"))
    assert disagree.status_code == 422
    assert disagree.json()["error"]["code"] == "whirlwind/unprocessable"
    # unknown seam instance: SeamError at admission (400)
    bad_instance = await client.post("/agents", json=version_payload(seam_instances=["missing-i"]))
    assert bad_instance.status_code == 400
    # inline + instance collision on the same seam: 400
    collide = await client.post(
        "/agents",
        json=version_payload(seam_bindings=[{"seam": "fs.v1", "provider": "sandbox-fs"}]),
    )
    assert collide.status_code == 400
    # legacy shape unchanged: harness + image_ref required without a bundle
    legacy_missing = await client.post(
        "/agents", json={"name": "reject-agent", "version": {"harness": "echo"}}
    )
    assert legacy_missing.status_code == 400


async def test_instance_update_is_live_for_new_sandboxes(env) -> None:
    """ConfigMap semantics (ADR-0011 D2): updating an instance changes what
    NEWLY provisioned sandboxes get; nothing is frozen at version creation."""
    client, data_dir = env
    await client.put("/seam-templates/workspace-fs", json=TEMPLATE_BODY)
    await client.put(
        "/seam-instances/prod-fs",
        json={"name": "prod-fs", "template": "workspace-fs", "params": {"mode": "read-only"}},
    )
    await client.post("/images/echo/build")
    await client.post(
        "/agents",
        json={"name": "live-agent", "version": {"harness_bundle": "echo", "seam_instances": ["prod-fs"]}},
    )

    first = await _run_echo_turn(client, "live-agent", "one")
    assert _read_workspace_doc(data_dir, first, "manifest.json")["seams"][0]["policy"]["mode"] == "read-only"

    # flip the instance — same name, new params
    await client.put(
        "/seam-instances/prod-fs",
        json={"name": "prod-fs", "template": "workspace-fs", "params": {"mode": "workspace-write"}},
    )
    second = await _run_echo_turn(client, "live-agent", "two")
    assert _read_workspace_doc(data_dir, second, "manifest.json")["seams"][0]["policy"]["mode"] == "workspace-write"
    # the first sandbox's persisted manifest keeps its resolved snapshot
    assert _read_workspace_doc(data_dir, first, "manifest.json")["seams"][0]["policy"]["mode"] == "read-only"


async def test_bundle_shadowing_and_env_overlay(env) -> None:
    client, data_dir = env
    listed = {b["name"] for b in (await client.get("/harness-bundles")).json()}
    assert {"echo", "dsh"} <= listed  # builtin fallbacks, zero seeding ceremony

    # a stored doc shadows the builtin: same adapter, custom env overlay
    await client.put(
        "/harness-bundles/echo",
        json={
            "name": "echo",
            "harness": "echo",
            "image_ref": "echo",
            "env": {"WHIRLWIND_TEST_OVERLAY": "from-bundle"},
        },
    )
    shadowed = (await client.get("/harness-bundles/echo")).json()
    assert shadowed["env"]["WHIRLWIND_TEST_OVERLAY"] == "from-bundle"

    await client.post("/images/echo/build")
    await client.post(
        "/agents", json={"name": "overlay-agent", "version": {"harness_bundle": "echo"}}
    )
    session_id = await _run_echo_turn(client, "overlay-agent", "overlay")
    runtime_plan = _read_workspace_doc(data_dir, session_id, "runtime.json")
    assert runtime_plan["env"]["WHIRLWIND_TEST_OVERLAY"] == "from-bundle"

    # DELETE restores the builtin (a shadow doc is removed, not the builtin)
    assert (await client.delete("/harness-bundles/echo")).status_code == 200
    restored = (await client.get("/harness-bundles/echo")).json()
    assert restored["env"] == {} and restored["harness"] == "echo"


async def test_legacy_inline_bindings_still_work(env) -> None:
    client, data_dir = env
    await client.post("/images/echo/build")
    await client.post(
        "/agents",
        json={
            "name": "inline-agent",
            "version": {
                "harness": "echo",
                "image_ref": "echo",
                "seam_bindings": [{"seam": "fs.v1", "provider": "sandbox-fs", "policy": {"mode": "read-only"}}],
            },
        },
    )
    session_id = await _run_echo_turn(client, "inline-agent", "inline")
    manifest = _read_workspace_doc(data_dir, session_id, "manifest.json")
    (binding,) = manifest["seams"]
    assert binding["policy"]["mode"] == "read-only"
    assert binding["instance"] == ""  # inline decls carry no provenance
