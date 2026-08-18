"""Reproduce the CLI stream failure and surface the harness error event."""

import asyncio
import json
import shutil
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, "/workspace/src")
sys.path.insert(0, "/workspace/tests")

from argus.control import LifecycleManager, SessionManager
from argus.core.ids import new_agent_id, new_version_id
from argus.core.model import AgentDefinition, AgentVersion
from argus.drivers.process import ProcessDriver
from argus.harness.adapters import default_registry
from argus.hostlet.hostlet import Hostlet, HostletConfig
from argus.imaging.local import LocalRegistry
from argus.seam.model import SeamRenderer
from argus.storage.local import JSONLEventLog
from argus.storage.memory import MemoryMetadataStore
from argus.bus.inproc import InProcessEventBus
from argus.timer.wheel import TimingWheel

API_KEY = "test-key-123"
seen: dict[str, Any] = {}


async def _upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        headers = {}
        for line in lines[1:]:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        body = await reader.readexactly(int(headers.get("content-length", "0")))
        seen["auth"] = headers.get("authorization", "")
        seen["body"] = json.loads(body)
        if seen["auth"] != f"Bearer {API_KEY}":
            resp = json.dumps({"error": "unauthorized"}).encode()
        else:
            resp = json.dumps({"choices": [{"message": {"content": "relayed-reply"}}]}).encode()
        out = (
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(resp)}\r\nConnection: close\r\n\r\n"
        ).encode() + resp
        writer.write(out)
        await writer.drain()
    finally:
        writer.close()


async def main() -> None:
    tmp = Path("/workspace/.repro-data")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)

    server = await asyncio.start_server(_upstream, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    upstream = f"http://127.0.0.1:{port}"
    print(f"upstream={upstream}", flush=True)

    import os
    os.environ["ARGUS_TEST_KEY"] = API_KEY

    store = MemoryMetadataStore()
    bus = InProcessEventBus()
    hostlet = Hostlet(
        driver=ProcessDriver(),
        images=LocalRegistry(tmp / "images", repo_root=Path("/workspace")),
        adapters=default_registry(),
        renderer=SeamRenderer(),
        store=store,
        event_log=JSONLEventLog(tmp / "events"),
        bus=bus,
        config=HostletConfig(
            data_dir=tmp,
            api_key_env="ARGUS_TEST_KEY",
            llm_upstream=upstream,
        ),
    )
    await hostlet.start()

    agent = AgentDefinition(id=new_agent_id(), name="repro")
    await store.create_agent(agent)
    version = AgentVersion(
        id=new_version_id(), agent_id=agent.id, version="1.0.0",
        harness="echo", image_ref="echo",
        model_config_decl={"provider": "deepseek-official", "model": "deepseek-chat"},
    )
    await store.create_version(version)

    wheel = TimingWheel(tick_ms=20)
    wheel.start()
    lifecycle = LifecycleManager(wheel)
    manager = SessionManager(store, hostlet, bus, lifecycle)
    await manager.start()

    session_id = await manager.create_session(agent.id)
    print(f"session={session_id}", flush=True)
    try:
        await manager.send_turn(session_id, "hello repro")
        print("turn ok", flush=True)
    except Exception as exc:
        print(f"turn failed: {type(exc).__name__}: {exc}", flush=True)

    events = await JSONLEventLog(tmp / "events").read(session_id)
    for e in events:
        print(f"{e.type}: {json.dumps(e.data)[:300]}", flush=True)
    print(f"upstream seen: {seen}", flush=True)

    await manager.stop()
    await wheel.stop()
    await hostlet.aclose()
    server.close()


asyncio.run(main())
