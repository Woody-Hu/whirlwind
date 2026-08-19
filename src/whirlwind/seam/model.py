"""Capability Seam model (architecture 4.6 / ADR D5).

Three roles, mirroring the DeepSeek Harness capability-seam design:
- SeamDefinition: the capability contract (e.g. `fs.v1`) with its tool surface;
- Provider: a named implementation of a seam with a policy (e.g. `sandbox-fs`
  with `mode: workspace-write`);
- Consumer: how the seam is exposed to a given harness (`dsh` native, or any
  harness via the MCP fallback).

The Renderer compiles an AgentVersion's declarations into an
`InjectionManifest` — the intermediate format that crosses the sandbox boundary.
Unknown seams or providers fail closed (SeamError), never silently degrade.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from whirlwind.core import AgentVersion, SeamBindingDecl, SeamError, SkillRef


class SeamTool(BaseModel):
    """One tool of a seam's surface. Doubles as the MCP tool descriptor."""

    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)


class SeamDefinition(BaseModel):
    id: str  # "fs.v1"
    display_name: str
    description: str
    tools: list[SeamTool]


class ProviderSpec(BaseModel):
    name: str
    seam: str
    description: str = ""
    default_policy: dict[str, Any] = Field(default_factory=dict)
    policy_fields: tuple[str, ...] = ()


class SeamConsumer(BaseModel):
    harness: str  # "dsh" | "*" | adapter id
    mode: str = "native"  # "native" | "mcp"
    config: dict[str, Any] = Field(default_factory=dict)


class SeamBinding(BaseModel):
    """Rendered triple: Definition id + Provider + policy + Consumers."""

    seam: str
    provider: str
    policy: dict[str, Any]
    consumers: list[SeamConsumer]


class SkillInjection(BaseModel):
    name: str
    version: str
    source_path: str  # staging dir inside the sandbox


class InjectionManifest(BaseModel):
    """The intermediate format: everything a harness adapter needs inside the sandbox."""

    session_id: str
    agent_version_id: str
    harness: str
    workspace_root: str
    seams: list[SeamBinding] = Field(default_factory=list)
    skills: list[SkillInjection] = Field(default_factory=list)
    model_config_decl: dict[str, Any] = Field(default_factory=dict)
    llm_relay_url: str = ""
    events_post_url: str = ""


# --------------------------------------------------------------- builtin seams

FS_V1 = SeamDefinition(
    id="fs.v1",
    display_name="Filesystem",
    description="Read and write files inside the agent workspace",
    tools=[
        SeamTool(
            name="fs_read",
            description="Read a file from the workspace",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        ),
        SeamTool(
            name="fs_write",
            description="Write a file in the workspace",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        ),
    ],
)

SHELL_V1 = SeamDefinition(
    id="shell.v1",
    display_name="Shell",
    description="Execute shell commands inside the sandbox",
    tools=[
        SeamTool(
            name="shell_exec",
            description="Run a shell command in the workspace",
            input_schema={
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        ),
    ],
)

WEB_V1 = SeamDefinition(
    id="web.v1",
    display_name="Web",
    description="Search and fetch web content via the platform relay",
    tools=[
        SeamTool(
            name="web_fetch",
            description="Fetch a URL's content",
            input_schema={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        ),
    ],
)

MEMORY_V1 = SeamDefinition(
    id="memory.v1",
    display_name="Memory",
    description="Persistent memory scoped to the session",
    tools=[
        SeamTool(
            name="memory_save",
            description="Save a note to session memory",
            input_schema={
                "type": "object",
                "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
                "required": ["key", "value"],
            },
        ),
        SeamTool(
            name="memory_load",
            description="Load notes from session memory",
            input_schema={"type": "object", "properties": {}},
        ),
    ],
)

BUILTIN_SEAMS: dict[str, SeamDefinition] = {s.id: s for s in (FS_V1, SHELL_V1, WEB_V1, MEMORY_V1)}

BUILTIN_PROVIDERS: dict[str, ProviderSpec] = {
    spec.name: spec
    for spec in (
        ProviderSpec(
            name="sandbox-fs",
            seam="fs.v1",
            description="In-sandbox filesystem provider",
            default_policy={"mode": "workspace-write"},
            policy_fields=("mode", "workspaceRoot"),
        ),
        ProviderSpec(
            name="sandbox-bash",
            seam="shell.v1",
            description="In-sandbox shell provider",
            default_policy={"mode": "workspace-write"},
            policy_fields=("mode",),
        ),
        ProviderSpec(
            name="relay-web",
            seam="web.v1",
            description="Host-relay web provider (egress via sidecar)",
            default_policy={"allowHosts": []},
            policy_fields=("allowHosts",),
        ),
        ProviderSpec(
            name="workspace-memory",
            seam="memory.v1",
            description="Workspace-local persistent memory directory",
            default_policy={"scope": "session"},
            policy_fields=("scope",),
        ),
    )
}


class SeamRegistry:
    """Seam definitions + provider specs. Extensible by registration."""

    def __init__(
        self,
        seams: dict[str, SeamDefinition] | None = None,
        providers: dict[str, ProviderSpec] | None = None,
    ) -> None:
        self._seams = dict(seams or BUILTIN_SEAMS)
        self._providers = dict(providers or BUILTIN_PROVIDERS)

    def seam(self, seam_id: str) -> SeamDefinition:
        try:
            return self._seams[seam_id]
        except KeyError:
            raise SeamError(f"unknown seam {seam_id!r}") from None

    def provider(self, name: str) -> ProviderSpec:
        try:
            return self._providers[name]
        except KeyError:
            raise SeamError(f"unknown provider {name!r}") from None

    def register_seam(self, definition: SeamDefinition) -> None:
        self._seams[definition.id] = definition

    def register_provider(self, spec: ProviderSpec) -> None:
        self._providers[spec.name] = spec


class SeamRenderer:
    """Compiles AgentVersion declarations into the injection manifest (ADR D5)."""

    def __init__(self, registry: SeamRegistry | None = None) -> None:
        self.registry = registry or SeamRegistry()

    def render_bindings(self, version: AgentVersion) -> list[SeamBinding]:
        rendered: list[SeamBinding] = []
        seen: set[str] = set()
        for decl in version.seam_bindings:
            self._validate_decl(decl)
            if decl.seam in seen:
                raise SeamError(f"duplicate seam binding {decl.seam!r}")
            seen.add(decl.seam)
            spec = self.registry.provider(decl.provider)
            policy = {**spec.default_policy, **decl.policy}
            unknown = set(policy) - set(spec.policy_fields) if spec.policy_fields else set()
            if unknown:
                raise SeamError(f"provider {decl.provider!r} rejects policy keys: {sorted(unknown)}")
            consumers = [
                SeamConsumer(harness=c.harness, mode=c.mode, config=c.config)
                for c in decl.consumers
            ] or [SeamConsumer(harness=version.harness, mode="native")]
            rendered.append(
                SeamBinding(seam=decl.seam, provider=decl.provider, policy=policy, consumers=consumers)
            )
        return rendered

    def render_manifest(
        self,
        version: AgentVersion,
        session_id: str,
        workspace_root: str,
        skills: list[tuple[SkillRef, str]] | None = None,
        llm_relay_url: str = "",
        events_post_url: str = "",
    ) -> InjectionManifest:
        return InjectionManifest(
            session_id=session_id,
            agent_version_id=version.id,
            harness=version.harness,
            workspace_root=workspace_root,
            seams=self.render_bindings(version),
            skills=[
                SkillInjection(name=ref.name, version=ref.version, source_path=path)
                for ref, path in (skills or [])
            ],
            model_config_decl=version.model_config_decl,
            llm_relay_url=llm_relay_url,
            events_post_url=events_post_url,
        )

    def _validate_decl(self, decl: SeamBindingDecl) -> None:
        seam = self.registry.seam(decl.seam)
        spec = self.registry.provider(decl.provider)
        if spec.seam != seam.id:
            raise SeamError(f"provider {decl.provider!r} implements {spec.seam!r}, not {decl.seam!r}")
