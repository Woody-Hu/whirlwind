"""HarnessAdapter unit tests: manifest -> native config compilation.

The rendered cordis.yml is parsed back with a real YAML parser where one is
importable (uvicorn ships none; the runtime env may) — otherwise the structure
is asserted textually. Fail-closed behaviour is the point of several cases.
"""

from __future__ import annotations

import json

import pytest

from argus.core import AgentVersion, HarnessError, SeamBindingDecl, SeamConsumerDecl, SkillRef
from argus.harness.adapter import DshAdapter, EchoAdapter, _yaml_dump, default_registry
from argus.seam.model import SeamRenderer

WS = "/srv/sandbox/ws"


def _version(harness: str, seams: list[SeamBindingDecl], skills: list[SkillRef] | None = None) -> AgentVersion:
    return AgentVersion(
        id="ver_1",
        agent_id="agent_1",
        version="1.0.0",
        harness=harness,
        image_ref="echo",
        seam_bindings=seams,
        skill_refs=skills or [],
        model_config_decl={"provider": "deepseek-official", "model": "deepseek-chat"},
    )


def _manifest(version: AgentVersion, **kwargs):
    renderer = SeamRenderer()
    return renderer.render_manifest(version, session_id="sess_1", workspace_root=WS, **kwargs)


def _decl(seam: str, provider: str, consumers: list[SeamConsumerDecl] | None = None) -> SeamBindingDecl:
    return SeamBindingDecl(seam=seam, provider=provider, consumers=consumers or [])


# ------------------------------------------------------------------- echo

def test_echo_adapter_maps_relay_and_model() -> None:
    manifest = _manifest(_version("echo", [_decl("shell.v1", "sandbox-bash")]), llm_relay_url="http://127.0.0.1:9")
    prepared = EchoAdapter().prepare(manifest)
    assert prepared.env["ECHO_LLM_URL"] == "http://127.0.0.1:9"
    assert prepared.env["ECHO_LLM_MODEL"] == "deepseek-chat"
    assert prepared.files == {}


# -------------------------------------------------------------------- dsh

def test_dsh_adapter_renders_spine_and_native_seams() -> None:
    seams = [
        _decl("shell.v1", "sandbox-bash"),
        _decl("fs.v1", "sandbox-fs"),
        _decl("memory.v1", "workspace-memory"),
    ]
    prepared = DshAdapter().prepare(_manifest(_version("dsh", seams)))
    cordis = prepared.files[".argus/cordis.yml"]
    # spine is always present
    for name in (
        "@deepseek-ai/dsh-sdk-jsonrpc-server",
        "@deepseek-ai/dsh-agent-spine-demo",
        "@deepseek-ai/dsh-llm-deepseek",
        "@deepseek-ai/dsh-session-persistence-jsonl",
        "@deepseek-ai/dsh-subprocess-local",
    ):
        assert name in cordis
    # native seam components mounted with absolute workspace paths
    assert f"cwd: {WS}" in cordis
    assert "@deepseek-ai/dsh-bash-local" in cordis
    assert "@deepseek-ai/dsh-fs-local" in cordis
    # memory is a provisioned dir, not a component
    assert f"{WS}/.argus/memory" in prepared.files[".argus/provisioned-dirs"]
    # env points the runtime at the rendered config
    assert prepared.env["DSH_CORDIS_CONFIG"] == f"{WS}/.argus/cordis.yml"
    assert prepared.env["DSH_SESSION_ROOT"] == f"{WS}/.argus/sessions"
    assert prepared.env["DSH_CWD"] == WS
    assert prepared.session_root == f"{WS}/.argus/sessions"
    assert "DEEPSEEK_BASE_URL" not in prepared.env  # no relay declared


def test_dsh_adapter_relay_url_becomes_base_url() -> None:
    manifest = _manifest(_version("dsh", []), llm_relay_url="http://127.0.0.1:7712/relay/llm")
    prepared = DshAdapter().prepare(manifest)
    assert prepared.env["DEEPSEEK_BASE_URL"] == "http://127.0.0.1:7712/relay/llm"


def test_dsh_adapter_mounts_skills_component() -> None:
    skills = [SkillRef(name="pdf-tools", version="1.2.0")]
    prepared = DshAdapter().prepare(_manifest(_version("dsh", []), skills=[(skills[0], "/stage/pdf-tools")]))
    cordis = prepared.files[".argus/cordis.yml"]
    assert "@deepseek-ai/dsh-skill-filesystem" in cordis
    assert f"- {WS}/.argus/skills" in cordis
    assert "includeDefaultRoots: false" in cordis


def test_dsh_adapter_fail_closed_on_unmappable_native_seam() -> None:
    seams = [_decl("web.v1", "relay-web")]  # no native dsh mapping in M1
    with pytest.raises(HarnessError, match="no native"):
        DshAdapter().prepare(_manifest(_version("dsh", seams)))


def test_dsh_adapter_skips_mcp_only_bindings() -> None:
    seams = [
        _decl(
            "web.v1",
            "relay-web",
            consumers=[SeamConsumerDecl(harness="*", mode="mcp")],
        )
    ]
    prepared = DshAdapter().prepare(_manifest(_version("dsh", seams)))
    assert "@deepseek-ai/dsh-bash-local" not in prepared.files[".argus/cordis.yml"]
    assert "relay-web" not in prepared.files[".argus/cordis.yml"]


def test_rendered_cordis_is_parseable_yaml() -> None:
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        pytest.skip("pyyaml not importable in this env")
    seams = [_decl("shell.v1", "sandbox-bash")]
    skills = [SkillRef(name="s", version="1")]
    prepared = DshAdapter().prepare(_manifest(_version("dsh", seams), skills=[(skills[0], "/x")]))
    doc = yaml.safe_load(prepared.files[".argus/cordis.yml"])
    assert isinstance(doc, list)
    by_id = {c["id"]: c for c in doc}
    assert by_id["skills"]["config"]["customSkillDirs"] == [f"{WS}/.argus/skills"]
    assert by_id["skills"]["config"]["includeDefaultRoots"] is False
    assert by_id["sessions"]["config"]["root"] == f"{WS}/.argus/sessions"


def test_registry_resolves_and_fails_closed() -> None:
    registry = default_registry()
    assert registry.adapter_for("dsh").harness == "dsh"
    assert registry.adapter_for("echo").harness == "echo"
    with pytest.raises(HarnessError, match="no adapter"):
        registry.adapter_for("bogus")


def test_yaml_dump_escapes_and_serializes_scalars() -> None:
    assert _yaml_dump({"k": "it's: fine"}) == "k: 'it''s: fine'"
    assert _yaml_dump({"n": None, "t": True, "f": False, "i": 3}) == "n: null\nt: true\nf: false\ni: 3"
    assert _yaml_dump([]) == "[]"
    assert _yaml_dump({}) == "{}"
    with pytest.raises(TypeError):
        _yaml_dump({"bad": object()})
