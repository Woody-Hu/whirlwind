"""Agent-defined env secrets end-to-end (ADR-0010).

Real runtime, real REST (ASGI), real files, real sandbox subprocesses — no
mocks. The chain under test: POST /agents with `env` -> validation -> seal ->
envelope on disk (0600, ciphertext only) -> first turn provisions a sandbox
whose runtime.json harness env carries the decrypted values. Plus the D4
rejections and the D5 precedence proof.
"""

from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path

import httpx
import pytest

from whirlwind.runtime import RuntimeConfig, WhirlwindRuntime

REPO_ROOT = Path(__file__).resolve().parents[2]
SECRET_VALUE = "gh-live-token-do-not-persist"


@pytest.fixture
async def asgi(tmp_path: Path):
    """(runtime, client): a started all-in-one runtime driven over ASGI.

    Provisioning happens on the first turn, so no LLM upstream is needed —
    the echo harness without ECHO_LLM_URL never calls the relay.
    """
    runtime = WhirlwindRuntime(
        RuntimeConfig(
            data_dir=tmp_path / "runtime",
            repo_root=REPO_ROOT,
            api_key_env="WHIRLWIND_TEST_KEY",
            llm_upstream="http://127.0.0.1:1",  # unused: no LLM path in these tests
        )
    )
    await runtime.start()
    transport = httpx.ASGITransport(app=runtime.app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=60.0) as client:
            yield runtime, client
    finally:
        await runtime.stop()


async def _build_echo_image(client: httpx.AsyncClient) -> None:
    response = await client.post("/images/echo/build")
    assert response.status_code == 200, response.text


async def _turn_to_end(client: httpx.AsyncClient, session_id: str) -> None:
    """Send a turn and poll the durable log until turn/end arrives."""
    response = await client.post(f"/sessions/{session_id}/turns", json={"text": "hi"})
    assert response.status_code == 200, response.text
    deadline = asyncio.get_event_loop().time() + 20.0
    while asyncio.get_event_loop().time() < deadline:
        events = (await client.get(f"/sessions/{session_id}/events")).json()
        if any(e["type"] == "turn/end" for e in events):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("turn never completed")


def _runtime_plans(runtime: WhirlwindRuntime) -> list[Path]:
    return list((runtime.config.data_dir / "sandboxes").glob("*/ws/.whirlwind/runtime.json"))


# ---------------------------------------------------------------------- tests


@pytest.mark.asyncio
async def test_env_secrets_full_chain(asgi) -> None:
    """(a) names only in every response; (b) ciphertext + 0600 on disk;
    (d) decrypted values land in the sandbox runtime.json harness env."""
    runtime, client = asgi
    await _build_echo_image(client)

    created = await client.post(
        "/agents",
        json={
            "name": "secret-agent",
            "version": {
                "harness": "echo",
                "image_ref": "echo",
                "env": {"GITHUB_TOKEN": SECRET_VALUE, "OTHER_KEY": "other-value"},
            },
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["version"]["env_secrets"] == ["GITHUB_TOKEN", "OTHER_KEY"]
    assert "env" not in body["version"]  # write-only surface: values never echoed
    assert SECRET_VALUE not in created.text

    # every GET surface carries names only
    got = await client.get("/agents/secret-agent")
    assert got.status_code == 200
    assert SECRET_VALUE not in got.text
    assert got.json()["versions"][0]["env_secrets"] == ["GITHUB_TOKEN", "OTHER_KEY"]

    # on-disk artifact: envelope ciphertext, 0600, no plaintext anywhere
    version_id = body["version"]["id"]
    envelope_file = runtime.config.data_dir / "secrets" / f"{version_id}.json"
    assert envelope_file.is_file()
    assert stat.S_IMODE(envelope_file.stat().st_mode) == 0o600
    raw = envelope_file.read_text()
    assert SECRET_VALUE not in raw and "other-value" not in raw
    envelopes = json.loads(raw)
    assert set(envelopes) == {"GITHUB_TOKEN", "OTHER_KEY"}
    assert all(v.startswith("v1:") for v in envelopes.values())

    # (d) the first turn provisions a real sandbox; secrets ride its harness env
    session = await client.post("/sessions", json={"agent_name": "secret-agent"})
    assert session.status_code == 200, session.text
    await _turn_to_end(client, session.json()["id"])
    plans = _runtime_plans(runtime)
    assert plans, "no sandbox runtime.json was provisioned"
    plan = json.loads(plans[0].read_text())
    assert plan["env"]["GITHUB_TOKEN"] == SECRET_VALUE
    assert plan["env"]["OTHER_KEY"] == "other-value"
    # the injection manifest never carries values (it is persisted + echoed)
    manifests = list((runtime.config.data_dir / "sandboxes").glob("*/ws/.whirlwind/manifest.json"))
    assert manifests and SECRET_VALUE not in manifests[0].read_text()


@pytest.mark.asyncio
async def test_reserved_names_rejected_and_nothing_persisted(asgi) -> None:
    runtime, client = asgi
    await _build_echo_image(client)
    for reserved in ("WHIRLWIND_EVIL", "DEEPSEEK_API_KEY"):
        response = await client.post(
            "/agents",
            json={
                "name": f"evil-{reserved.lower()}",
                "version": {"harness": "echo", "image_ref": "echo", "env": {reserved: "x"}},
            },
        )
        assert response.status_code == 400, response.text
        assert "reserved" in response.json()["error"]["message"]
    # no agent, no version, no envelopes were created
    assert (await client.get("/agents")).json() == []
    assert not any((runtime.config.data_dir / "secrets").glob("*.json"))


@pytest.mark.asyncio
async def test_precedence_prepared_env_beats_user_secret(asgi) -> None:
    """D5: adapter wiring (ECHO_LLM_MODEL from model_config_decl) wins over a
    user secret of the same name — the platform boundary survives a hostile
    definition that slipped past name validation."""
    runtime, client = asgi
    await _build_echo_image(client)
    created = await client.post(
        "/agents",
        json={
            "name": "hostile-agent",
            "version": {
                "harness": "echo",
                "image_ref": "echo",
                "model_config_decl": {"model": "deepseek-chat"},
                "env": {"ECHO_LLM_MODEL": "user-wants-to-hijack"},
            },
        },
    )
    assert created.status_code == 200, created.text
    session = await client.post("/sessions", json={"agent_name": "hostile-agent"})
    assert session.status_code == 200, session.text
    await _turn_to_end(client, session.json()["id"])
    plans = _runtime_plans(runtime)
    plan = json.loads(plans[0].read_text())
    assert plan["env"]["ECHO_LLM_MODEL"] == "deepseek-chat"  # prepared.env won


@pytest.mark.asyncio
async def test_fail_closed_when_envelopes_missing(asgi) -> None:
    """A version declaring secrets whose envelopes vanished fails provisioning
    instead of booting a harness without its promised credentials."""
    runtime, client = asgi
    await _build_echo_image(client)
    created = await client.post(
        "/agents",
        json={
            "name": "lost-env-agent",
            "version": {"harness": "echo", "image_ref": "echo", "env": {"GITHUB_TOKEN": "v"}},
        },
    )
    assert created.status_code == 200, created.text
    version_id = created.json()["version"]["id"]
    # simulate the envelope store losing the record
    (runtime.config.data_dir / "secrets" / f"{version_id}.json").unlink()
    session = await client.post("/sessions", json={"agent_name": "lost-env-agent"})
    assert session.status_code == 200, session.text
    # provisioning runs on the first turn — it must fail closed
    response = await client.post(f"/sessions/{session.json()['id']}/turns", json={"text": "hi"})
    assert response.status_code >= 400
    message = response.json()["error"]["message"].lower()
    assert "env secrets" in message or "envelopes" in message
    assert not _runtime_plans(runtime), "no sandbox may boot without its declared secrets"
