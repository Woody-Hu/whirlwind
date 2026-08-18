"""Hostlet: host-side owner of one sandbox's lifecycle (architecture 4.4/4.5).

Responsibilities:
- ensure: image -> bundle, workspace provisioning, seam injection manifest,
  adapter-prepared files (cordis.yml &c), sandbox launch via the driver,
  agent health gate
- turn/stop: control the harness through the SandboxAgent HTTP face
- control face (localhost HTTP): event ingest + SecretRelay
- destroy: teardown via the driver

Secret boundary: the DeepSeek API key is read from the host environment here
and never crosses into the sandbox — the harness calls the agent's
/relay/llm/*, the agent forwards to this control face, and only here is the
Authorization header attached before egress to the upstream provider.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from argus.core import (
    AgentSession,
    AgentVersion,
    Sandbox,
    SandboxStatus,
    SessionStatus,
    SkillRef,
    Snapshot,
    SnapshotKind,
    new_sandbox_id,
)
from argus.core.errors import ArgusError, Conflict, NotFound
from argus.drivers import SandboxDriver, SandboxSpec, SnapshotArtifact
from argus.harness.adapter import AdapterRegistry
from argus.imaging import ImageRegistry
from argus.seam.model import SeamRenderer
from argus.storage.providers import EventBus, EventLog, MetadataStore


class HostletError(ArgusError):
    code = "argus/hostlet"


@dataclass
class HostletConfig:
    data_dir: Path
    api_key_env: str = "DEEPSEEK_API_KEY"
    llm_upstream: str = "https://api.deepseek.com"
    agent_boot_timeout_s: float = 30.0


@dataclass
class _ManagedSandbox:
    sandbox_id: str
    agent_port: int
    session_id: str
    harness: str
    workspace: Path
    session_root: str = ""
    http: httpx.AsyncClient = field(default_factory=lambda: httpx.AsyncClient(timeout=120.0))


def _alloc_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _as_artifact(snapshot: Snapshot) -> SnapshotArtifact:
    """Rebuild the driver-level artifact handle from the stored domain record."""
    return SnapshotArtifact(
        snapshot_id=snapshot.manifest.get("sandbox_id", snapshot.subject),
        subject=snapshot.subject,
        kind=snapshot.kind,
        path=Path(snapshot.location),
        manifest=snapshot.manifest,
        size=snapshot.size,
        merkle=snapshot.merkle,
    )


class Hostlet:
    """Owns sandboxes for one host process. Single tenant in M1."""

    def __init__(
        self,
        driver: SandboxDriver,
        images: ImageRegistry,
        adapters: AdapterRegistry,
        renderer: SeamRenderer,
        store: MetadataStore,
        event_log: EventLog,
        bus: EventBus,
        config: HostletConfig,
    ) -> None:
        self.driver = driver
        self.images = images
        self.adapters = adapters
        self.renderer = renderer
        self.store = store
        self.event_log = event_log
        self.bus = bus
        self.config = config
        self._sandboxes: dict[str, _ManagedSandbox] = {}
        self._api_key = os.environ.get(config.api_key_env, "")
        self._server: uvicorn.Server | None = None
        self._server_task: asyncio.Task | None = None
        self._upstream = httpx.AsyncClient(timeout=120.0)
        self._app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        self._wire_control_face()

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        config = uvicorn.Config(self._app, host="127.0.0.1", port=0, log_level="warning")
        self._server = uvicorn.Server(config)
        self._server_task = asyncio.get_running_loop().create_task(self._server.serve())
        for _ in range(200):
            if self._server.started:
                break
            await asyncio.sleep(0.05)
        if not self._server.started:
            raise HostletError("hostlet control face failed to start")

    async def aclose(self) -> None:
        for sandbox_id in list(self._sandboxes):
            await self.destroy(sandbox_id)
        await self._upstream.aclose()
        if self._server is not None:
            self._server.should_exit = True
            if self._server_task is not None:
                await self._server_task

    @property
    def base_url(self) -> str:
        assert self._server is not None and self._server.started
        return f"http://127.0.0.1:{self._port}"

    @property
    def _port(self) -> int:
        assert self._server is not None
        sockets = self._server.servers[0].sockets  # type: ignore[index]
        return int(sockets[0].getsockname()[1])

    # ---------------------------------------------------------------- api

    async def ensure(
        self,
        session: AgentSession | None,
        version: AgentVersion,
        *,
        from_snapshot: Snapshot | None = None,
    ) -> Sandbox:
        """Provision + boot a sandbox. `session=None` provisions a WARM
        (unbound, pre-booted) sandbox for the pool; otherwise binds directly."""
        sandbox_id = new_sandbox_id()
        bundle = await self.images.resolve(version.image_ref)
        adapter = self.adapters.adapter_for(version.harness)
        workspace = self.config.data_dir / "sandboxes" / sandbox_id / "ws"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / ".argus").mkdir(parents=True, exist_ok=True)

        if from_snapshot is not None:
            # Seed harness/user state from the snapshot BEFORE the fresh plan
            # files are written: the plan (ports, sandbox ids, workspace paths)
            # is per-sandbox and must win over the snapshot's stale copies.
            artifact = _as_artifact(from_snapshot)
            if not artifact.path.is_dir():
                raise HostletError(f"snapshot artifact missing: {artifact.path}")
            shutil.copytree(artifact.path, workspace, symlinks=True, dirs_exist_ok=True)

        # stage skills from the resource registry into the workspace
        skills: list[tuple[SkillRef, str]] = []
        skills_dir = workspace / ".argus" / "skills"
        for ref in version.skill_refs:
            staged = await self._stage_skill(ref, skills_dir)
            skills.append((ref, str(staged)))

        agent_port = _alloc_port()
        llm_relay_url = f"http://127.0.0.1:{agent_port}/relay/llm"
        manifest = self.renderer.render_manifest(
            version,
            session_id=session.id if session is not None else "",
            workspace_root=str(workspace),
            skills=skills,
            llm_relay_url=llm_relay_url,
            events_post_url="",
        )
        prepared = adapter.prepare(manifest)
        for rel, content in prepared.files.items():
            target = workspace / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        (workspace / ".argus" / "manifest.json").write_text(manifest.model_dump_json())

        harness_env = {**bundle.env, **prepared.env}
        harness_env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
        harness_env.setdefault("HOME", str(workspace))
        harness_env.setdefault("PYTHONUNBUFFERED", "1")
        runtime_plan = {"launcher": list(bundle.launcher), "env": harness_env}
        (workspace / ".argus" / "runtime.json").write_text(json.dumps(runtime_plan))
        provisioned = workspace / ".argus" / "provisioned-dirs"
        if provisioned.is_file():
            for line in provisioned.read_text().splitlines():
                if line.strip():
                    Path(line.strip()).mkdir(parents=True, exist_ok=True)

        # the SandboxAgent is the sandbox's first process; the harness is its child
        agent_interpreter = bundle.launcher[0]
        sandbox_env = {
            "ARGUS_SANDBOX_ID": sandbox_id,
            "ARGUS_MANIFEST": str(workspace / ".argus" / "manifest.json"),
            "ARGUS_RUNTIME": str(workspace / ".argus" / "runtime.json"),
            "ARGUS_AGENT_PORT": str(agent_port),
            "ARGUS_HOSTLET_URL": self.base_url,
            "PATH": harness_env["PATH"],
            "PYTHONUNBUFFERED": "1",
        }
        spec = SandboxSpec(
            sandbox_id=sandbox_id,
            argv=[agent_interpreter, "-m", "argus.agent.server"],
            bundle_root=bundle.root,
            workspace=workspace,
            env=sandbox_env,
        )
        record = Sandbox(
            id=sandbox_id,
            pool_id="default",
            agent_version_id=version.id,
            status=SandboxStatus.PROVISIONING,
        )
        await self.store.upsert_sandbox(record)
        await self.driver.create(spec)
        record.workspace = str(workspace)
        if session is not None:
            record.status = SandboxStatus.BINDING
            record.bound_session_id = session.id
            await self.store.upsert_sandbox(record)
        managed = _ManagedSandbox(
            sandbox_id=sandbox_id,
            agent_port=agent_port,
            session_id=session.id if session is not None else "",
            harness=version.harness,
            workspace=workspace,
            session_root=prepared.session_root,
        )
        self._sandboxes[sandbox_id] = managed
        try:
            await self._wait_healthy(managed)
        except Exception:
            await self.destroy(sandbox_id)
            raise
        record.status = SandboxStatus.ACTIVE if session is not None else SandboxStatus.WARM
        await self.store.upsert_sandbox(record)
        if session is not None:
            session.bound_sandbox_id = sandbox_id
            await self.store.update_session(session)
        return record

    async def bind(self, sandbox_id: str, session: AgentSession) -> Sandbox:
        """Bind a WARM sandbox to a session (the pool claim's second half)."""
        managed = self._get(sandbox_id)
        record = await self.store.get_sandbox(sandbox_id)
        if record is None or record.status != SandboxStatus.WARM:
            raise Conflict(f"sandbox {sandbox_id} is not warm; cannot bind")
        record.status = SandboxStatus.BINDING
        record.bound_session_id = session.id
        await self.store.upsert_sandbox(record)
        record.status = SandboxStatus.ACTIVE
        await self.store.upsert_sandbox(record)
        managed.session_id = session.id
        session.bound_sandbox_id = sandbox_id
        await self.store.update_session(session)
        return record

    async def turn(self, sandbox_id: str, text: str, content_blocks: list | None = None) -> str:
        managed = self._get(sandbox_id)
        body: dict[str, Any] = {"sessionId": managed.session_id}
        if content_blocks is not None:
            body["contentBlocks"] = content_blocks
        else:
            body["text"] = text
        response = await managed.http.post(f"http://127.0.0.1:{managed.agent_port}/turn", json=body)
        if response.status_code != 200:
            raise HostletError(f"agent /turn failed: {response.status_code} {response.text}")
        return str(response.json().get("messageId", ""))

    async def stop(self, sandbox_id: str) -> None:
        managed = self._get(sandbox_id)
        await managed.http.post(f"http://127.0.0.1:{managed.agent_port}/stop")

    async def destroy(self, sandbox_id: str) -> None:
        managed = self._sandboxes.pop(sandbox_id, None)
        record = await self.store.get_sandbox(sandbox_id)
        if record is not None and record.status == SandboxStatus.ACTIVE:
            record.status = SandboxStatus.DRAINING
            await self.store.upsert_sandbox(record)
        if managed is not None:
            await managed.http.aclose()
        await self.driver.destroy(sandbox_id)
        if record is not None:
            record.status = SandboxStatus.TERMINATED
            await self.store.upsert_sandbox(record)

    # ------------------------------------------------- suspend / restore (M2)

    async def suspend(self, sandbox_id: str) -> Snapshot:
        """Suspend = data checkpoint + process teardown (ADR conflict #6).

        The process driver cannot freeze memory, so a suspended sandbox is an
        dead process plus a content-addressed workspace snapshot. Harness
        state survives inside the snapshot to the extent its persistence
        files live in the workspace (dsh: session JSONL under .argus/sessions).
        """
        managed = self._get(sandbox_id)
        record = await self.store.get_sandbox(sandbox_id)
        if record is None or record.status != SandboxStatus.ACTIVE:
            raise Conflict(f"sandbox {sandbox_id} is not active; cannot suspend")
        record.status = SandboxStatus.SNAPSHOTTING
        await self.store.upsert_sandbox(record)

        artifact = await self.driver.checkpoint(sandbox_id, SnapshotKind.DATA)
        snapshot = Snapshot(
            kind=SnapshotKind.DATA,
            subject=sandbox_id,
            manifest={
                "session_id": managed.session_id,
                "sandbox_id": sandbox_id,
                "agent_version_id": record.agent_version_id,
                "harness": managed.harness,
                "files": artifact.manifest.get("files"),
            },
            location=str(artifact.path),
            size=artifact.size,
            merkle=artifact.merkle,
        )
        await self.store.save_snapshot(snapshot)

        self._sandboxes.pop(sandbox_id, None)
        await managed.http.aclose()
        await self.driver.destroy(sandbox_id)
        if record.workspace is not None:
            shutil.rmtree(record.workspace, ignore_errors=True)  # data lives in the snapshot now
        record.status = SandboxStatus.SUSPENDED
        record.bound_session_id = None
        record.last_snapshot_id = artifact.snapshot_id
        record.workspace = None
        await self.store.upsert_sandbox(record)

        session = await self.store.get_session(managed.session_id)
        if session is not None and session.bound_sandbox_id == sandbox_id:
            session.bound_sandbox_id = None
            await self.store.update_session(session)
        return snapshot

    async def restore(self, session: AgentSession, version: AgentVersion) -> Sandbox:
        """Resume: a fresh sandbox booted from the session's latest snapshot."""
        snapshot = await self.store.latest_session_snapshot(session.id)
        if snapshot is None:
            raise NotFound(f"session {session.id} has no snapshot to restore from")
        previous = await self.store.get_sandbox(snapshot.manifest.get("sandbox_id", ""))
        sandbox = await self.ensure(session, version, from_snapshot=snapshot)
        if previous is not None and previous.status == SandboxStatus.SUSPENDED:
            previous.status = SandboxStatus.TERMINATED
            await self.store.upsert_sandbox(previous)
        return sandbox

    def _get(self, sandbox_id: str) -> _ManagedSandbox:
        try:
            return self._sandboxes[sandbox_id]
        except KeyError:
            raise NotFound(f"hostlet does not manage sandbox {sandbox_id}") from None

    async def _stage_skill(self, ref: SkillRef, skills_dir: Path) -> Path:
        source = await self.store.skill_path(ref)
        if source is None:
            raise NotFound(f"skill {ref.name}@{ref.version} not found in registry")
        dest = skills_dir / ref.name
        if source.is_dir():
            shutil.copytree(source, dest, dirs_exist_ok=True)
        else:
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest / source.name)
        return dest

    async def _wait_healthy(self, managed: _ManagedSandbox) -> None:
        deadline = asyncio.get_running_loop().time() + self.config.agent_boot_timeout_s
        url = f"http://127.0.0.1:{managed.agent_port}/health"
        while asyncio.get_running_loop().time() < deadline:
            try:
                response = await managed.http.get(url, timeout=2.0)
                if response.status_code == 200 and response.json().get("ok"):
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.02)  # tight poll: boot latency shows up directly in dispatch p50
        raise HostletError(f"sandbox agent did not become healthy within {self.config.agent_boot_timeout_s}s")

    # -------------------------------------------------------- control face

    def _wire_control_face(self) -> None:
        app = self._app

        @app.get("/healthz")
        async def _healthz() -> dict[str, Any]:
            return {"ok": True, "sandboxes": len(self._sandboxes)}

        @app.post("/ingest/events")
        async def _ingest(request: Request) -> JSONResponse:
            payload = await request.json()
            if payload.get("kind") == "event":
                event = payload.get("event", {})
                stored = await self.event_log.append(
                    payload["session_id"], event.get("type", ""), event.get("data") or {}
                )
                self.bus.publish(f"sessions.{stored.session_id}.stream", stored.model_dump())
            elif payload.get("kind") == "status":
                self.bus.publish(
                    f"sessions.{payload['session_id']}.status",
                    {"session_id": payload["session_id"], "status": payload.get("status")},
                )
            return JSONResponse({"ok": True})

        @app.api_route("/secret/llm/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
        async def _secret_llm(path: str, request: Request):
            """SecretRelay: attach the credential and stream bytes through.

            LLM traffic is SSE (`text/event-stream`) as often as JSON; the
            response is passed through unbuffered with its content-type intact.
            """
            if not self._api_key:
                return JSONResponse({"error": "hostlet has no api key"}, status_code=503)
            headers = {
                k: v
                for k, v in request.headers.items()
                if k.lower() not in ("host", "content-length", "authorization", "transfer-encoding")
            }
            headers["Authorization"] = f"Bearer {self._api_key}"
            upstream_req = self._upstream.build_request(
                request.method,
                f"{self.config.llm_upstream}/{path}",
                headers=headers,
                content=await request.body(),
            )
            upstream = await self._upstream.send(upstream_req, stream=True)

            async def _passthrough():
                try:
                    async for chunk in upstream.aiter_raw():
                        yield chunk
                finally:
                    await upstream.aclose()

            return StreamingResponse(
                _passthrough(),
                status_code=upstream.status_code,
                headers={
                    "content-type": upstream.headers.get("content-type", "application/octet-stream")
                },
            )
