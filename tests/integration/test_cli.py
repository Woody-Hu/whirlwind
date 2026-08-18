"""CLI integration: the real `argus` command against a real `argus serve` process.

Everything is a subprocess: the server boots via the console entrypoint
(`python -m argus.cli serve`), the client commands hit it over HTTP, and the
echo sandbox + LLM relay run for real (local upstream fixture).
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import time
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _run_cli(*args: str, env_extra: dict[str, str] | None = None, timeout_s: float = 120.0) -> str:
    env = {**os.environ, **(env_extra or {})}
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "argus.cli",
        *args,
        cwd=str(REPO_ROOT),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    assert proc.returncode == 0, f"argus {' '.join(args)} failed:\n{err.decode()}"
    return out.decode()


@pytest.fixture
async def server(tmp_path: Path, llm_upstream: str) -> str:
    port = _free_port()
    env = dict(os.environ)  # inherits ARGUS_TEST_KEY sentinel set by the llm_upstream fixture
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "argus.cli",
        "serve",
        "--port",
        str(port),
        "--data-dir",
        str(tmp_path / "serve-data"),
        "--repo-root",
        str(REPO_ROOT),
        "--api-key-env",
        "ARGUS_TEST_KEY",
        "--llm-upstream",
        llm_upstream,
        cwd=str(REPO_ROOT),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 60
    async with httpx.AsyncClient(base_url=base, timeout=2.0) as client:
        while time.monotonic() < deadline:
            try:
                if (await client.get("/healthz")).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.2)
        else:
            proc.terminate()
            out, _ = await proc.communicate()
            raise AssertionError(f"serve never became healthy:\n{out.decode()[-3000:]}")
    yield base
    proc.terminate()
    try:
        await asyncio.wait_for(proc.communicate(), timeout=10)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()


@pytest.mark.asyncio
@pytest.mark.timeout(600)  # the echo image build is a real pip venv install
async def test_cli_full_flow(server: str) -> None:
    env = {"ARGUS_URL": server}

    # image -> agent -> session -> streamed turn -> durable events
    assert "ref" in await _run_cli("image", "build", "echo", env_extra=env, timeout_s=300)

    created = await _run_cli(
        "agent",
        "create",
        "cli-agent",
        "--harness",
        "echo",
        "--image",
        "echo",
        "--model",
        "provider=deepseek-official",
        "--model",
        "model=deepseek-chat",
        "--seam",
        "shell.v1=sandbox-bash",
        env_extra=env,
    )
    assert "cli-agent" in created

    listed = await _run_cli("agent", "list", env_extra=env)
    assert "cli-agent" in listed

    session_id = (await _run_cli("session", "create", "cli-agent", env_extra=env)).strip()
    assert session_id.startswith("ses_")

    streamed = await _run_cli("session", "send", session_id, "hello cli", "--stream", env_extra=env)
    assert "relayed-reply" in streamed  # real reply through agent relay -> hostlet -> upstream

    events = await _run_cli("session", "events", session_id, env_extra=env)
    assert "turn/start" in events and "turn/end" in events

    # cron: add, trigger (fresh session + real turn), list, delete
    cron_out = await _run_cli(
        "cron",
        "add",
        "agt_cli-agent",
        "--schedule",
        "*/5 * * * *",
        "--input",
        "cron says hi",
        env_extra=env,
    )
    assert "*/5 * * * *" in cron_out
    cron_id = _field(cron_out, "id")

    triggered = await _run_cli("cron", "trigger", cron_id, env_extra=env)
    fired_session = _field(triggered, "session_id")
    assert fired_session.startswith("ses_") and fired_session != session_id  # FRESH policy

    jobs = await _run_cli("cron", "list", env_extra=env)
    assert cron_id in jobs

    # error surface: unknown session exits non-zero with the server's message
    with pytest.raises(AssertionError):
        await _run_cli("session", "send", "ses_missing", "nope", env_extra=env)


def _field(json_out: str, key: str) -> str:
    import json

    payload = json.loads(json_out)
    return str(payload[key])
