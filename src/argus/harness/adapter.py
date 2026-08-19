"""HarnessAdapter: InjectionManifest -> harness-native launch configuration.

The intermediate format (InjectionManifest) is platform-neutral; each adapter
compiles it into what its harness natively understands:

- echo: `ECHO_*` environment variables
- dsh:  a rendered `cordis.yml` (component list) written into the workspace +
        `DSH_*` environment pointing at it

Adapters are pure functions of the manifest (no I/O): the Hostlet writes the
files and applies the env when it binds the sandbox. Unknown harnesses and
unmappable native seams fail closed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

from argus.core import HarnessError
from argus.seam.model import InjectionManifest, SeamBinding


@dataclass(slots=True)
class PreparedHarness:
    env: dict[str, str] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)  # workspace-relative -> content
    session_root: str = ""  # absolute dir holding per-session JSONL (EventTap face)


class HarnessAdapter(Protocol):
    harness: str

    def prepare(self, manifest: InjectionManifest) -> PreparedHarness: ...


class EchoAdapter(HarnessAdapter):
    harness = "echo"

    def prepare(self, manifest: InjectionManifest) -> PreparedHarness:
        env: dict[str, str] = {}
        if manifest.llm_relay_url:
            env["ECHO_LLM_URL"] = manifest.llm_relay_url
        model = manifest.model_config_decl.get("model")
        if model:
            env["ECHO_LLM_MODEL"] = str(model)
        return PreparedHarness(env=env, files={}, session_root="")


# --------------------------------------------------------------------- dsh

# Adopt-or-create shim injected next to the rendered cordis.yml and loaded by
# relative path. The stock sdk-jsonrpc-server always creates a FRESH live
# session on session/prompt; when a persisted JSONL log already owns that
# identity (a sandbox booted from a snapshot) the backend rejects the turn as
# an id collision. The shim mirrors the official apiproxy semantics
# (api-proxy.ts): a session id with a materialized persisted log is ADOPTED
# via ctx.agents.resume, so the conversation continues across sandbox
# reincarnations with no dsh source patch.
DSH_RESUME_SHIM_MJS = """\
// argus adopt-or-create shim (see harness/adapter.py for rationale)
export const name = 'argus-resume-shim'
export const inject = ['agents', 'sessionPersistence']

export function apply(ctx) {
  const registry = ctx.agents
  const originalCreate = registry.create.bind(registry)
  registry.create = async (options) => {
    const sessionId = options?.sessionId
    if (sessionId !== undefined) {
      const persistence = ctx.get('sessionPersistence')
      const stored = persistence === undefined
        ? undefined
        : (await persistence.list()).find((h) => String(h.id) === String(sessionId))
      if (stored !== undefined) {
        return registry.resume({
          resumeSessionId: sessionId,
          agentOptions: options.agentOptions ?? {},
        })
      }
    }
    return originalCreate(options)
  }
  ctx.effect(() => () => { registry.create = originalCreate })
}
"""


class DshAdapter(HarnessAdapter):
    """Renders the manifest as a dsh cordis component list (ADR D5 low layer)."""

    harness = "dsh"

    def prepare(self, manifest: InjectionManifest) -> PreparedHarness:
        ws = manifest.workspace_root.rstrip("/")
        sessions_dir = f"{ws}/.argus/sessions"
        components: list[dict] = [
            {"id": "sdk-jsonrpc-server", "name": "@deepseek-ai/dsh-sdk-jsonrpc-server"},
            {
                "id": "agent-core",
                "name": "@deepseek-ai/dsh-agent-spine-demo",
                "config": {"workspaceContext": {"maxBytes": 65536}},
            },
            {"id": "llm-deepseek", "name": "@deepseek-ai/dsh-llm-deepseek"},
            {
                "id": "sessions",
                "name": "@deepseek-ai/dsh-session-persistence-jsonl",
                "config": {"root": sessions_dir},
            },
            {"id": "session-checkpoints", "name": "@deepseek-ai/dsh-session-checkpoint-policy"},
            {"id": "subprocess", "name": "@deepseek-ai/dsh-subprocess-local"},
            {"id": "resume-shim", "name": "./argus-resume-shim.mjs"},
        ]
        provisioned_dirs = [sessions_dir]
        for binding in manifest.seams:
            if not self._wants_native(binding, manifest.harness):
                continue
            self._mount_native(binding, ws, components, provisioned_dirs)
        if manifest.skills:
            skills_dir = f"{ws}/.argus/skills"
            components.append(
                {
                    "id": "skills",
                    "name": "@deepseek-ai/dsh-skill-filesystem",
                    "config": {"includeDefaultRoots": False, "customSkillDirs": [skills_dir]},
                }
            )
            provisioned_dirs.append(skills_dir)
        env = {
            "DSH_CORDIS_CONFIG": f"{ws}/.argus/cordis.yml",
            "DSH_SESSION_ROOT": sessions_dir,
            "DSH_CWD": ws,
            "DSH_AGENTS_HOME": f"{ws}/.argus/agents-home",
        }
        if manifest.llm_relay_url:
            # The llm-deepseek adapter refuses to run without a key present.
            # The relay owns the real credential host-side and REPLACES the
            # Authorization header on egress, so this placeholder is not a
            # secret and never reaches the upstream provider.
            env["DEEPSEEK_BASE_URL"] = manifest.llm_relay_url
            env["DEEPSEEK_API_KEY"] = "argus-relay"
        files = {
            ".argus/cordis.yml": _yaml_dump(components),
            ".argus/argus-resume-shim.mjs": DSH_RESUME_SHIM_MJS,
            ".argus/provisioned-dirs": "\n".join(provisioned_dirs) + "\n",
        }
        return PreparedHarness(env=env, files=files, session_root=sessions_dir)

    @staticmethod
    def _wants_native(binding: SeamBinding, harness: str) -> bool:
        return any(c.harness == harness and c.mode == "native" for c in binding.consumers)

    @staticmethod
    def _mount_native(
        binding: SeamBinding, ws: str, components: list[dict], provisioned: list[str]
    ) -> None:
        key = (binding.seam, binding.provider)
        if key == ("shell.v1", "sandbox-bash"):
            components.append(
                {
                    "id": "bash",
                    "name": "@deepseek-ai/dsh-bash-local",
                    "config": {"cwd": ws},
                }
            )
        elif key == ("fs.v1", "sandbox-fs"):
            components.append(
                {
                    "id": "fs-local",
                    "name": "@deepseek-ai/dsh-fs-local",
                    "config": {"cwd": ws},
                }
            )
        elif key == ("memory.v1", "workspace-memory"):
            # memory is a provisioned workspace directory consumed via fs tools
            provisioned.append(f"{ws}/.argus/memory")
        else:
            raise HarnessError(
                f"seam {binding.seam!r} via provider {binding.provider!r} has no native "
                f"dsh mapping; declare an mcp consumer instead",
                detail={"seam": binding.seam, "provider": binding.provider},
            )


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, HarnessAdapter] = {}

    def register(self, adapter: HarnessAdapter) -> None:
        self._adapters[adapter.harness] = adapter

    def adapter_for(self, harness: str) -> HarnessAdapter:
        try:
            return self._adapters[harness]
        except KeyError:
            raise HarnessError(f"no adapter registered for harness {harness!r}") from None


def default_registry() -> AdapterRegistry:
    registry = AdapterRegistry()
    registry.register(EchoAdapter())
    registry.register(DshAdapter())
    return registry


# --------------------------------------------------------------- yaml (subset)

def _yaml_dump(value: object, indent: int = 0) -> str:
    """Deterministic YAML for the cordis component structure (lists of scalar
    dicts). No external dependency; anything else raises rather than guessing."""
    pad = "  " * indent
    if isinstance(value, list):
        if not value:
            return "[]"
        chunks = []
        for item in value:
            chunks.append(f"{pad}- " + _yaml_dump(item, indent + 1).lstrip())
        return "\n".join(chunks)
    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = []
        for key, item in value.items():
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{pad}{key}:")
                lines.append(_yaml_dump(item, indent + 1))
            else:
                lines.append(f"{pad}{key}: {_yaml_scalar(item)}")
        return "\n".join(lines)
    return _yaml_scalar(value)


_SAFE_SCALAR = re.compile(r"^[/A-Za-z0-9_][/A-Za-z0-9_./ -]*$")


def _yaml_scalar(value: object) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        if _SAFE_SCALAR.fullmatch(value) and not value.endswith(" ") and ": " not in value:
            return value
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, (int, float)):
        return str(value)
    raise TypeError(f"cannot serialize {type(value).__name__} to cordis yaml")
