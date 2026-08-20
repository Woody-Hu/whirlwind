"""Gateway: the north-facing REST + SSE + MCP face (architecture 10.1 / ADR D7-D8).

REST verbs map 1:1 onto control-plane calls; session events stream over SSE
with durable replay (`Last-Event-ID` or `?from_seq=` — both are the per-session
seq from the EventLog). The MCP gateway rides under `/mcp/{version_id}`.

The app is a pure assembly boundary: it receives already-constructed modules
(GatewayDeps) and never imports the runtime, keeping the layering one-way.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from whirlwind.control.manager import SessionManager
from whirlwind.core import (
    AgentDefinition,
    AgentVersion,
    CronJob,
    HarnessBundle,
    SeamBindingDecl,
    SeamInstance,
    SeamTemplate,
)
from whirlwind.core.errors import (
    WhirlwindError,
    BadRequest,
    Conflict,
    InvalidTransition,
    NotFound,
    QuotaExceeded,
    SeamError,
    Unprocessable,
)
from whirlwind.core.model import SessionPolicy
from whirlwind.gateway.cron import CronScheduler
from whirlwind.gateway.idempotency import IdempotencyMiddleware
from whirlwind.gateway.mcp import McpGateway
from whirlwind.harness.bundles import HarnessBundles
from whirlwind.imaging import ImageRegistry, dsh_image_build, echo_image_build
from whirlwind.seam.catalog import SeamCatalog
from whirlwind.seam.model import SeamRenderer
from whirlwind.secrets import SecretBox, SecretBoxError, SecretNameError, validate_env_names
from whirlwind.storage.providers import EventLog, EventBus, KVStore, MetadataStore, SecretStore
from whirlwind.timer.cron import CronExpr, CronParseError

logger = logging.getLogger(__name__)


@dataclass
class GatewayDeps:
    manager: SessionManager
    store: MetadataStore
    event_log: EventLog
    bus: EventBus
    images: ImageRegistry
    renderer: SeamRenderer
    cron: CronScheduler
    mcp: McpGateway
    repo_root: Path
    version: str = "0.1.0"
    kv: KVStore | None = None  # enables Idempotency-Key on mutating routes (ADR-0005 D3)
    # agent-env secrets (ADR-0010): seal on version write; optional so
    # secret-less deployments (and minimal test assemblies) stay valid —
    # supplying `env` values without a box is rejected at request time.
    secret_box: SecretBox | None = None
    secret_store: SecretStore | None = None
    # catalog faces (ADR-0011 D7): REST CRUD + eager admission validation.
    # Optional with the same fail-closed contract — payloads referencing
    # bundles/instances without wiring are rejected at request time.
    seam_catalog: SeamCatalog | None = None
    bundles: HarnessBundles | None = None


# ------------------------------------------------------------- request models


class VersionIn(BaseModel):
    version: str = "1.0.0"
    # ADR-0011 D4: with `harness_bundle` set, harness/image_ref come from the
    # bundle and may be omitted; explicitly given values must agree (422).
    # Without a bundle they are required as before (legacy shape unchanged).
    harness: str | None = None
    image_ref: str | None = None
    harness_bundle: str = ""
    seam_instances: list[str] = Field(default_factory=list)
    entrypoint: list[str] = Field(default_factory=list)
    seam_bindings: list[dict[str, Any]] = Field(default_factory=list)
    skill_refs: list[dict[str, str]] = Field(default_factory=list)
    model_config_decl: dict[str, Any] = Field(default_factory=dict)
    # write-only surface (ADR-0010 D6): accepted at creation, sealed into the
    # SecretStore, never echoed back — responses carry `env_secrets` names.
    env: dict[str, str] | None = None


class AgentIn(BaseModel):
    name: str
    display_name: str = ""
    version: VersionIn


class SessionIn(BaseModel):
    agent_id: str | None = None
    agent_name: str | None = None
    version_id: str | None = None
    idle_timeout_s: float | None = None
    max_duration_s: float | None = None


class TurnIn(BaseModel):
    text: str | None = None
    content_blocks: list[dict[str, Any]] | None = None


class CronIn(BaseModel):
    agent_id: str
    schedule: str
    input_template: str
    session_policy: SessionPolicy = SessionPolicy.FRESH
    session_id: str | None = None
    enabled: bool = True


_STATUS_BY_ERROR = {
    NotFound: 404,
    Conflict: 409,
    InvalidTransition: 409,
    QuotaExceeded: 429,
    SeamError: 400,
    BadRequest: 400,
    Unprocessable: 422,  # well-formed payload, declared references disagree (ADR-0011 D4)
    SecretNameError: 400,
    SecretBoxError: 500,
}


def _error_response(exc: WhirlwindError) -> JSONResponse:
    status = 500
    for cls, code in _STATUS_BY_ERROR.items():
        if isinstance(exc, cls):
            status = code
            break
    return JSONResponse(
        {"error": {"code": exc.code, "message": str(exc), "detail": exc.detail}},
        status_code=status,
    )


def create_app(
    deps: GatewayDeps,
    on_startup: Callable[[], Awaitable[None]] | None = None,
    on_shutdown: Callable[[], Awaitable[None]] | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def _lifespan(_: FastAPI):
        if on_startup is not None:
            await on_startup()
        yield
        if on_shutdown is not None:
            await on_shutdown()

    app = FastAPI(title="whirlwind-gateway", lifespan=_lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    if deps.kv is not None:
        app.add_middleware(IdempotencyMiddleware, kv=deps.kv)

    @app.exception_handler(WhirlwindError)
    async def _whirlwind_error(_: Request, exc: WhirlwindError) -> JSONResponse:
        return _error_response(exc)

    # ------------------------------------------------------------- health

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "version": deps.version}

    # ------------------------------------------------------------ images

    @app.post("/images/{name}/build")
    async def build_image(name: str) -> dict[str, Any]:
        recipes = {
            "echo": lambda: echo_image_build(deps.repo_root),
            "dsh": lambda: dsh_image_build(deps.repo_root),
        }
        recipe = recipes.get(name)
        if recipe is None:
            raise NotFound(f"no build recipe for image {name!r} (known: {sorted(recipes)})")
        ref = await deps.images.register(recipe())
        return {"ref": ref}

    # ------------------------------------------------------------ skills

    @app.post("/skills/{name}/{version}")
    async def upload_skill(name: str, version: str, request: Request) -> dict[str, Any]:
        """Register a skill the Hostlet stages into workspaces.

        Body is either a SKILL.md document (any text content type) or a zip
        archive whose root holds SKILL.md (optionally with bundled files).
        """
        body = await request.body()
        if not body:
            raise BadRequest("skill body is empty")
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp)
            if body[:2] == b"PK":
                archive = staged / "skill.zip"
                archive.write_bytes(body)
                try:
                    with zipfile.ZipFile(archive) as zf:
                        zf.extractall(staged / "skill")
                except zipfile.BadZipFile as exc:
                    raise BadRequest(f"skill archive is not a valid zip: {exc}") from exc
                source = staged / "skill"
                if not (source / "SKILL.md").is_file():
                    raise BadRequest("skill archive must contain SKILL.md at its root")
            else:
                source = staged / "SKILL.md"
                source.write_bytes(body)
            ref = await deps.store.save_skill(name, version, source)
        return {"name": ref.name, "version": ref.version}

    # ------------------------------------------------------ catalog (ADR-0011 D7)
    # REST CRUD over seam templates / instances and harness bundles. Upserts
    # validate eagerly (template bodies against the renderer's registry,
    # instance params against their template) so a stored doc is always
    # resolvable; resolution itself happens live at provision.

    def _require_seam_catalog() -> SeamCatalog:
        if deps.seam_catalog is None:
            raise BadRequest("seam catalog is not configured")
        return deps.seam_catalog

    def _require_bundles() -> HarnessBundles:
        if deps.bundles is None:
            raise BadRequest("bundle catalog is not configured")
        return deps.bundles

    async def _parse_body(request: Request, model_cls: type[BaseModel]) -> BaseModel:
        try:
            return model_cls.model_validate_json(await request.body())
        except ValidationError as exc:
            raise BadRequest(f"invalid {model_cls.__name__} body: {exc.errors()[:3]}") from exc

    @app.get("/seam-templates")
    async def list_seam_templates() -> list[dict[str, Any]]:
        return [t.model_dump() for t in await _require_seam_catalog().list_templates()]

    @app.get("/seam-templates/{name}")
    async def get_seam_template(name: str) -> dict[str, Any]:
        template = await _require_seam_catalog().get_template(name)
        if template is None:
            raise NotFound(f"seam template {name!r}")
        return template.model_dump()

    @app.post("/seam-templates")
    async def put_seam_template(request: Request) -> dict[str, Any]:
        template = await _parse_body(request, SeamTemplate)
        return (await _require_seam_catalog().put_template(template)).model_dump()

    @app.put("/seam-templates/{name}")
    async def put_seam_template_named(name: str, request: Request) -> dict[str, Any]:
        template = await _parse_body(request, SeamTemplate)
        if template.name != name:
            raise BadRequest(f"body name {template.name!r} does not match path name {name!r}")
        return (await _require_seam_catalog().put_template(template)).model_dump()

    @app.delete("/seam-templates/{name}")
    async def delete_seam_template(name: str) -> dict[str, Any]:
        await _require_seam_catalog().delete_template(name)
        return {"ok": True}

    @app.get("/seam-instances")
    async def list_seam_instances() -> list[dict[str, Any]]:
        return [i.model_dump() for i in await _require_seam_catalog().list_instances()]

    @app.get("/seam-instances/{name}")
    async def get_seam_instance(name: str) -> dict[str, Any]:
        instance = await _require_seam_catalog().get_instance(name)
        if instance is None:
            raise NotFound(f"seam instance {name!r}")
        return instance.model_dump()

    @app.post("/seam-instances")
    async def put_seam_instance(request: Request) -> dict[str, Any]:
        instance = await _parse_body(request, SeamInstance)
        return (await _require_seam_catalog().put_instance(instance)).model_dump()

    @app.put("/seam-instances/{name}")
    async def put_seam_instance_named(name: str, request: Request) -> dict[str, Any]:
        instance = await _parse_body(request, SeamInstance)
        if instance.name != name:
            raise BadRequest(f"body name {instance.name!r} does not match path name {name!r}")
        return (await _require_seam_catalog().put_instance(instance)).model_dump()

    @app.delete("/seam-instances/{name}")
    async def delete_seam_instance(name: str) -> dict[str, Any]:
        await _require_seam_catalog().delete_instance(name)
        return {"ok": True}

    @app.get("/harness-bundles")
    async def list_harness_bundles() -> list[dict[str, Any]]:
        return [b.model_dump() for b in await _require_bundles().list()]

    @app.get("/harness-bundles/{name}")
    async def get_harness_bundle(name: str) -> dict[str, Any]:
        bundle = await _require_bundles().get(name)
        if bundle is None:
            raise NotFound(f"harness bundle {name!r}")
        return bundle.model_dump()

    @app.post("/harness-bundles")
    async def put_harness_bundle(request: Request) -> dict[str, Any]:
        bundle = await _parse_body(request, HarnessBundle)
        return (await _require_bundles().put(bundle)).model_dump()

    @app.put("/harness-bundles/{name}")
    async def put_harness_bundle_named(name: str, request: Request) -> dict[str, Any]:
        bundle = await _parse_body(request, HarnessBundle)
        if bundle.name != name:
            raise BadRequest(f"body name {bundle.name!r} does not match path name {name!r}")
        return (await _require_bundles().put(bundle)).model_dump()

    @app.delete("/harness-bundles/{name}")
    async def delete_harness_bundle(name: str) -> dict[str, Any]:
        """Remove a STORED bundle; builtin names (echo/dsh) keep resolving —
        deleting a shadow doc restores the builtin, it cannot remove it."""
        await _require_bundles().delete(name)
        return {"ok": True}

    # ------------------------------------------------------------ agents

    @app.post("/agents")
    async def create_agent(payload: AgentIn) -> dict[str, Any]:
        if await deps.store.get_agent_by_name(payload.name) is not None:
            raise Conflict(f"agent {payload.name!r} already exists")
        # full admission validation BEFORE the agent record exists: a rejected
        # payload must not orphan an agent whose version failed to materialize
        await _admit_version_payload(payload.version)
        agent = AgentDefinition(id=f"agt_{payload.name}", name=payload.name, display_name=payload.display_name)
        agent = await deps.store.create_agent(agent)
        version = await _create_version(agent, payload.version)
        return {"agent": agent.model_dump(), "version": version.model_dump()}

    @app.get("/agents")
    async def list_agents() -> list[dict[str, Any]]:
        return [a.model_dump() for a in await deps.store.list_agents()]

    @app.get("/agents/{agent_id}")
    async def get_agent(agent_id: str) -> dict[str, Any]:
        agent = await deps.store.get_agent(agent_id)
        if agent is None:
            agent = await deps.store.get_agent_by_name(agent_id)
        if agent is None:
            raise NotFound(f"agent {agent_id}")
        versions = await deps.store.list_versions(agent.id)
        return {"agent": agent.model_dump(), "versions": [v.model_dump() for v in versions]}

    @app.post("/agents/{agent_id}/versions")
    async def create_version(agent_id: str, payload: VersionIn) -> dict[str, Any]:
        agent = await deps.store.get_agent(agent_id)
        if agent is None:
            raise NotFound(f"agent {agent_id}")
        version = await _create_version(agent, payload)
        return version.model_dump()

    def _validate_version_payload(payload: VersionIn) -> None:
        """D4 checks + secret-store availability; idempotent, safe to call twice."""
        env_values = payload.env or {}
        validate_env_names(env_values)  # reserved namespace / POSIX shape / duplicates
        if env_values and (deps.secret_box is None or deps.secret_store is None):
            raise BadRequest("env secrets requested but the secret store is not configured")
        if payload.seam_instances and deps.seam_catalog is None:
            raise BadRequest("seam_instances requested but the seam catalog is not configured")

    async def _resolve_binding_fields(payload: VersionIn) -> tuple[str, str, list[str]]:
        """ADR-0011 D4/D7: resolve the harness source at admission time.

        With `harness_bundle` the bundle supplies harness/image_ref/entrypoint
        (explicit values must agree — 422, never a silent override); without
        it the legacy explicit pair is required. Returns the denormalized
        (harness, image_ref, entrypoint) stored on the version.
        """
        if not payload.harness_bundle:
            if not payload.harness or not payload.image_ref:
                raise BadRequest("version requires harness + image_ref (or a harness_bundle)")
            return payload.harness, payload.image_ref, payload.entrypoint
        if deps.bundles is None:
            raise BadRequest("harness_bundle requested but the bundle catalog is not configured")
        bundle = await deps.bundles.get(payload.harness_bundle)
        if bundle is None:
            raise NotFound(f"harness bundle {payload.harness_bundle!r}")
        for field, given, wanted in (
            ("harness", payload.harness, bundle.harness),
            ("image_ref", payload.image_ref, bundle.image_ref),
        ):
            if given and given != wanted:
                raise Unprocessable(
                    f"{field} {given!r} disagrees with harness bundle "
                    f"{payload.harness_bundle!r} ({field}={wanted!r}); "
                    "drop the field or fix the value"
                )
        return bundle.harness, bundle.image_ref, payload.entrypoint or bundle.entrypoint

    async def _admit_version_payload(payload: VersionIn) -> tuple[str, str, list[str]]:
        """Full admission validation (ADR-0011 D4/D7), callable before the
        agent record exists: shape checks, bundle existence/agreement, and the
        eager instance resolution (every reference resolves, no seam
        collisions). Returns the denormalized (harness, image_ref, entrypoint)
        for the version record; resolution repeats live at provision — this is
        a gate, not a cache."""
        _validate_version_payload(payload)
        fields = await _resolve_binding_fields(payload)
        if payload.seam_instances:
            draft = AgentVersion(
                id="ver_admission",
                agent_id="agent_admission",
                version=payload.version,
                harness=fields[0],
                image_ref=fields[1],
                seam_instances=payload.seam_instances,
                seam_bindings=[SeamBindingDecl.model_validate(b) for b in payload.seam_bindings],
            )
            await deps.seam_catalog.resolve_version_bindings(draft)
        return fields

    async def _create_version(agent: AgentDefinition, payload: VersionIn) -> AgentVersion:
        harness, image_ref, entrypoint = await _admit_version_payload(payload)
        version_id = f"ver_{agent.name}_{payload.version}"
        version = AgentVersion(
            id=version_id,
            agent_id=agent.id,
            version=payload.version,
            harness=harness,
            image_ref=image_ref,
            entrypoint=entrypoint,
            harness_bundle=payload.harness_bundle,
            seam_instances=payload.seam_instances,
            seam_bindings=payload.seam_bindings,  # type: ignore[assignment]
            skill_refs=payload.skill_refs,  # type: ignore[assignment]
            model_config_decl=payload.model_config_decl,
        )
        env_values = payload.env or {}
        # D1/D6: values sealed once here, stored as envelopes keyed by version;
        # the version itself carries names only. Envelopes go in FIRST (whole-set
        # replace, safe on retry) so a failed metadata write is cleaned up below,
        # and a failed envelope write leaves no version claiming secrets it
        # cannot inject.
        envelopes = deps.secret_box.seal_env(env_values) if env_values else {}
        if envelopes:
            await deps.secret_store.put_version_env(version_id, envelopes)
        version = version.model_copy(update={"env_secrets": sorted(env_values)})
        try:
            version = await deps.store.create_version(version)
        except Exception:
            if envelopes:
                await deps.secret_store.delete_version_env(version_id)
            raise
        agent.default_version_id = version.id
        await deps.store.update_agent(agent)
        return version

    # ----------------------------------------------------------- sessions

    @app.post("/sessions")
    async def create_session(payload: SessionIn) -> dict[str, Any]:
        agent = None
        if payload.agent_id:
            agent = await deps.store.get_agent(payload.agent_id)
        if agent is None and payload.agent_name:
            agent = await deps.store.get_agent_by_name(payload.agent_name)
        if agent is None:
            raise NotFound(f"agent {payload.agent_id or payload.agent_name}")
        version_id = payload.version_id or agent.default_version_id
        if version_id is None:
            raise NotFound(f"agent {agent.id} has no default version")
        session = await deps.manager.create_session(agent.id, version_id)
        if payload.idle_timeout_s is not None or payload.max_duration_s is not None:
            if payload.idle_timeout_s is not None:
                session.idle_timeout_s = payload.idle_timeout_s
            if payload.max_duration_s is not None:
                session.max_duration_s = payload.max_duration_s
            await deps.store.update_session(session)
        return session.model_dump()

    @app.get("/sessions")
    async def list_sessions(agent_id: str | None = None) -> list[dict[str, Any]]:
        return [s.model_dump() for s in await deps.store.list_sessions(agent_id)]

    @app.get("/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        return (await deps.manager.get_session(session_id)).model_dump()

    @app.post("/sessions/{session_id}/turns")
    async def send_turn(session_id: str, payload: TurnIn) -> dict[str, Any]:
        if payload.text is None and payload.content_blocks is None:
            raise BadRequest("turn requires text or content_blocks")
        return await deps.manager.send_turn(session_id, payload.text or "", payload.content_blocks)

    @app.post("/sessions/{session_id}/close")
    async def close_session(session_id: str) -> dict[str, Any]:
        return (await deps.manager.close_session(session_id)).model_dump()

    @app.post("/sessions/{session_id}/suspend")
    async def suspend_session(session_id: str) -> dict[str, Any]:
        return (await deps.manager.suspend_session(session_id)).model_dump()

    @app.post("/sessions/{session_id}/resume")
    async def resume_session(session_id: str) -> dict[str, Any]:
        return (await deps.manager.resume_session(session_id)).model_dump()

    @app.get("/sessions/{session_id}/events")
    async def read_events(session_id: str, from_seq: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        return [e.model_dump() for e in await deps.event_log.read(session_id, from_seq, limit)]

    @app.get("/sessions/{session_id}/stream")
    async def stream_session(session_id: str, request: Request, from_seq: int = 0) -> StreamingResponse:
        session = await deps.manager.get_session(session_id)  # 404 for unknown ids
        last_event_id = request.headers.get("last-event-id")
        cursor = max(from_seq, int(last_event_id) if last_event_id and last_event_id.isdigit() else 0)

        async def frames() -> AsyncIterator[str]:
            sub = await deps.bus.subscribe(f"sessions.{session.id}.stream")
            last = cursor
            try:
                # EventLog.read is exclusive (seq > from_seq); cursor is the last seq sent
                for event in await deps.event_log.read(session.id, from_seq=cursor):
                    last = event.seq
                    yield _sse_frame(event.seq, event.model_dump_json())
                while True:
                    payload = await sub.next(timeout=15.0)
                    if payload is None:
                        yield ": ping\n\n"
                        continue
                    seq = int(payload.get("seq", 0))
                    if seq <= last:
                        continue
                    last = seq
                    yield _sse_frame(seq, json.dumps(payload, separators=(",", ":")))
            finally:
                await deps.bus.unsubscribe(sub)

        return StreamingResponse(frames(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    # -------------------------------------------------------------- crons

    @app.post("/crons")
    async def add_cron(payload: CronIn) -> dict[str, Any]:
        try:
            CronExpr.parse(payload.schedule)
        except CronParseError as exc:
            raise BadRequest(f"invalid cron schedule: {exc}") from exc
        if await deps.store.get_agent(payload.agent_id) is None:
            raise NotFound(f"agent {payload.agent_id}")
        job = CronJob(
            agent_id=payload.agent_id,
            schedule=payload.schedule,
            input_template=payload.input_template,
            session_policy=payload.session_policy,
            session_id=payload.session_id,
            enabled=payload.enabled,
        )
        return (await deps.cron.add(job)).model_dump()

    @app.get("/crons")
    async def list_crons(agent_id: str | None = None) -> list[dict[str, Any]]:
        return [j.model_dump() for j in await deps.store.list_crons(agent_id)]

    @app.delete("/crons/{cron_id}")
    async def remove_cron(cron_id: str) -> dict[str, Any]:
        await deps.cron.remove(cron_id)
        return {"ok": True}

    @app.post("/crons/{cron_id}/trigger")
    async def trigger_cron(cron_id: str) -> dict[str, Any]:
        return await deps.cron.trigger(cron_id)

    # ------------------------------------------------------------- MCP

    @app.post("/mcp/{version_id}")
    async def mcp(version_id: str, request: Request) -> JSONResponse:
        reply = await deps.mcp.handle(version_id, await request.body())
        return JSONResponse(reply, headers={"mcp-session-id": "whirlwind-m1"})

    return app


def _sse_frame(seq: int, data: str) -> str:
    return f"id: {seq}\nevent: message\ndata: {data}\n\n"


async def serve(app: FastAPI, host: str = "127.0.0.1", port: int = 0) -> tuple[uvicorn.Server, asyncio.Task]:
    """Start a real uvicorn server for `app`; returns (server, serve-task)."""
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    task = asyncio.get_running_loop().create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    if not server.started:
        raise RuntimeError("gateway failed to start")
    return server, task
