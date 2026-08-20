"""Observability closed-loop integration: /metrics over the real runtime.

Runs the all-in-one runtime under a real uvicorn server and exercises the
metrics face end to end — the HTTP timing middleware increments request
counters, the source-of-truth samplers pull session state from the real
MetadataStore, and /metrics renders the Prometheus text exposition. No fakes:
agent/session records are created through the real REST API, sessions advance
through the real state machine.

The LLM upstream is not touched — no turn is executed here, so the tests stay
offline and deterministic.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
import uvicorn

from whirlwind.runtime import WhirlwindRuntime, RuntimeConfig

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
async def base_url(tmp_path: Path) -> str:
    runtime = WhirlwindRuntime(
        RuntimeConfig(
            data_dir=tmp_path / "runtime",
            repo_root=REPO_ROOT,
            api_key_env="WHIRLWIND_TEST_KEY",
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
async def client(base_url: str) -> httpx.AsyncClient:
    async with httpx.AsyncClient(base_url=base_url, timeout=60.0) as c:
        yield c


async def _metrics(client: httpx.AsyncClient) -> dict[str, str]:
    """Fetch /metrics and return a {sample_line: ...} multiset."""
    response = await client.get("/metrics")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    return {line.strip(): "" for line in response.text.splitlines() if "%" not in line}


def _sample_value(metrics: dict[str, str], series: str) -> str:
    """Extract a bare float sample value by exact series prefix+label match."""
    for line in metrics:
        if line.startswith(series + " ") or line.startswith(series + "{"):
            return line.split()[-1]
    pytest.fail(f"sample {series!r} not found in:\n" + "\n".join(metrics))


async def test_healthz_and_metrics_roundtrip(client: httpx.AsyncClient) -> None:
    # /healthz twice: the first warms the family headers, the count is 2
    assert (await client.get("/healthz")).status_code == 200
    assert (await client.get("/healthz")).status_code == 200

    metrics = await _metrics(client)
    assert "# TYPE whirlwind_http_requests_total counter" in metrics
    assert "# TYPE whirlwind_http_request_seconds histogram" in metrics
    assert "# TYPE whirlwind_sessions_by_status gauge" in metrics
    assert "# TYPE whirlwind_turns_total counter" in metrics
    assert "# TYPE whirlwind_wal_appends_total counter" in metrics

    # /healthz called twice -> per-route counter == 2
    assert _sample_value(metrics, 'whirlwind_http_requests_total{method="GET",route="/healthz",status="200"}') == "2"


async def test_request_counter_is_per_route(client: httpx.AsyncClient) -> None:
    # multiple distinct routes each get their own bounded bucket (no cardinality
    # blow-up from concrete ids in the path).
    assert (await client.get("/sessions")).status_code == 200
    assert (await client.get("/missing-route")).status_code == 404
    metrics = await _metrics(client)
    assert _sample_value(metrics, 'whirlwind_http_requests_total{method="GET",route="/sessions",status="200"}') == "1"
    assert _sample_value(
        metrics, 'whirlwind_http_requests_total{method="GET",route="/missing-route",status="404"}'
    ) == "1"


async def test_sessions_gauge_tracks_live_state(client: httpx.AsyncClient) -> None:
    # baseline: zero sessions in every status
    metrics = await _metrics(client)
    assert _sample_value(metrics, 'whirlwind_sessions_by_status{status="created"}') == "0"

    # create one session through the real REST + state-machine path
    response = await client.post(
        "/agents", json={"name": "obs-agent", "version": {"harness": "echo", "image_ref": "echo"}}
    )
    assert response.status_code == 200, response.text
    created = await client.post("/sessions", json={"agent_name": "obs-agent"})
    assert created.status_code == 200, created.text
    session_id = created.json()["id"]

    metrics = await _metrics(client)
    assert _sample_value(metrics, 'whirlwind_sessions_by_status{status="created"}') == "1"

    # closing advances the machine; the gauge heals from the store on next scrape
    closed = await client.post(f"/sessions/{session_id}/close")
    assert closed.status_code == 200, closed.text
    metrics = await _metrics(client)
    assert _sample_value(metrics, 'whirlwind_sessions_by_status{status="closed"}') == "1"
    assert _sample_value(metrics, 'whirlwind_sessions_by_status{status="created"}') == "0"