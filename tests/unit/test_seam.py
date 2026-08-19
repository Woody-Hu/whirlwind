"""Seam renderer unit tests: validation, fail-closed, defaults, manifest shape."""

import pytest

from whirlwind.core import AgentVersion, SeamBindingDecl, SeamConsumerDecl, SeamError, SkillRef
from whirlwind.seam import SeamConsumer, SeamRegistry, SeamRenderer


def _version(bindings, harness="dsh") -> AgentVersion:
    return AgentVersion(
        id="ver_1",
        agent_id="agt_1",
        version="1",
        harness=harness,
        image_ref="dsh@0.3",
        seam_bindings=bindings,
    )


def test_render_native_and_mcp_consumers():
    version = _version(
        [
            SeamBindingDecl(
                seam="fs.v1",
                provider="sandbox-fs",
                policy={"mode": "read-only"},
                consumers=[
                    SeamConsumerDecl(harness="dsh", mode="native"),
                    SeamConsumerDecl(harness="*", mode="mcp"),
                ],
            )
        ]
    )
    renderer = SeamRenderer()
    bindings = renderer.render_bindings(version)
    assert len(bindings) == 1
    b = bindings[0]
    assert b.seam == "fs.v1"
    assert b.provider == "sandbox-fs"
    assert b.policy["mode"] == "read-only"
    assert [c.harness for c in b.consumers] == ["dsh", "*"]


def test_default_consumer_matches_harness():
    version = _version([SeamBindingDecl(seam="shell.v1", provider="sandbox-bash")])
    bindings = SeamRenderer().render_bindings(version)
    assert bindings[0].consumers == [SeamConsumer(harness="dsh", mode="native")]


def test_provider_defaults_fill_policy():
    version = _version([SeamBindingDecl(seam="fs.v1", provider="sandbox-fs")])
    bindings = SeamRenderer().render_bindings(version)
    assert bindings[0].policy == {"mode": "workspace-write"}


def test_unknown_seam_fails_closed():
    version = _version([SeamBindingDecl(seam="teleport.v1", provider="sandbox-fs")])
    with pytest.raises(SeamError, match="unknown seam"):
        SeamRenderer().render_bindings(version)


def test_provider_seam_mismatch_fails_closed():
    version = _version([SeamBindingDecl(seam="fs.v1", provider="sandbox-bash")])
    with pytest.raises(SeamError, match="implements"):
        SeamRenderer().render_bindings(version)


def test_unknown_policy_key_rejected():
    version = _version(
        [SeamBindingDecl(seam="fs.v1", provider="sandbox-fs", policy={"oops": 1})]
    )
    with pytest.raises(SeamError, match="rejects policy keys"):
        SeamRenderer().render_bindings(version)


def test_duplicate_seam_rejected():
    version = _version(
        [
            SeamBindingDecl(seam="fs.v1", provider="sandbox-fs"),
            SeamBindingDecl(seam="fs.v1", provider="sandbox-fs"),
        ]
    )
    with pytest.raises(SeamError, match="duplicate"):
        SeamRenderer().render_bindings(version)


def test_manifest_render_includes_skills_and_endpoints():
    version = _version(
        [SeamBindingDecl(seam="memory.v1", provider="workspace-memory")],
    )
    version = version.model_copy(
        update={"model_config_decl": {"provider": "deepseek-official", "model": "deepseek-chat"}}
    )
    manifest = SeamRenderer().render_manifest(
        version,
        session_id="ses_1",
        workspace_root="/ws",
        skills=[(SkillRef(name="demo", version="1.0.0"), "/ws/.whirlwind/skills/demo-1.0.0")],
        llm_relay_url="http://127.0.0.1:7000/relay/llm",
        events_post_url="http://127.0.0.1:7000/events",
    )
    assert manifest.harness == "dsh"
    assert manifest.seams[0].seam == "memory.v1"
    assert manifest.skills[0].name == "demo"
    assert manifest.model_config_decl["model"] == "deepseek-chat"
    assert manifest.llm_relay_url.endswith("/relay/llm")
    # manifest must be JSON-roundtrippable (it crosses the sandbox boundary as a file)
    from whirlwind.seam import InjectionManifest

    assert InjectionManifest.model_validate_json(manifest.model_dump_json()) == manifest


def test_registry_extension():
    from whirlwind.seam import ProviderSpec, SeamDefinition, SeamTool

    registry = SeamRegistry()
    registry.register_seam(
        SeamDefinition(id="db.v1", display_name="DB", description="x", tools=[SeamTool(name="q", description="y")])
    )
    registry.register_provider(ProviderSpec(name="pg", seam="db.v1", policy_fields=("dsn",)))
    version = _version([SeamBindingDecl(seam="db.v1", provider="pg", policy={"dsn": "pg://"})])
    bindings = SeamRenderer(registry).render_bindings(version)
    assert bindings[0].policy == {"dsn": "pg://"}
