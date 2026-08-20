"""Unit tests: seam template/instance engine + catalog + harness bundles (ADR-0011).

Pure logic — no subprocesses, no HTTP. The catalog wrapper runs against
MemoryMetadataStore; substitution/validation functions are store-free.
"""

from __future__ import annotations

import pytest

from whirlwind.core import (
    AgentVersion,
    SeamBindingDecl,
    SeamError,
    SeamInstance,
    SeamParamSpec,
    SeamTemplate,
)
from whirlwind.harness.bundles import BUILTIN_HARNESS_BUNDLES, HarnessBundles
from whirlwind.seam import SeamRegistry, SeamRenderer
from whirlwind.seam.catalog import SeamCatalog
from whirlwind.seam.model import (
    extract_placeholders,
    render_template,
    resolve_params,
    substitute,
    validate_template_params,
)
from whirlwind.storage.memory import MemoryMetadataStore


# --------------------------------------------------------------- substitution


def test_extract_placeholders_nested():
    body = {
        "mode": "workspace-${scope}",
        "roots": ["${root_a}", "${root_b}"],
        "nested": {"flag": "${flag}", "plain": "no placeholder"},
    }
    assert extract_placeholders(body) == {"scope", "root_a", "root_b", "flag"}


def test_substitute_whole_value_preserves_type():
    body = {"count": "${n}", "ratio": "${r}", "on": "${flag}", "hosts": "${hosts}"}
    out = substitute(body, {"n": 5, "r": 1.5, "flag": True, "hosts": ["a.example", "b.example"]})
    assert out["count"] == 5
    assert out["ratio"] == 1.5
    assert out["on"] is True
    assert out["hosts"] == ["a.example", "b.example"]


def test_substitute_embedded_stringifies():
    body = {"mode": "workspace-${mode}", "label": "agent-${agent}-x${agent}"}
    out = substitute(body, {"mode": "write", "agent": 7})
    assert out["mode"] == "workspace-write"
    assert out["label"] == "agent-7-x7"


def test_substitute_leaves_unknown_placeholder_verbatim():
    assert substitute({"k": "${missing}"}, {}) == {"k": "${missing}"}


def test_substitute_dicts_and_lists_deeply():
    body = {"a": ["${x}", {"b": "${x}"}], "${x}": "key-substituted"}
    out = substitute(body, {"x": "v"})
    assert out == {"a": ["v", {"b": "v"}], "v": "key-substituted"}


# ------------------------------------------------------ template registration


def _template(**overrides) -> SeamTemplate:
    body = dict(
        name="workspace-fs",
        seam="fs.v1",
        provider="sandbox-fs",
        policy={"mode": "${mode}"},
        params=[SeamParamSpec(name="mode", type="string", required=True)],
    )
    body.update(overrides)
    return SeamTemplate.model_validate(body)


def test_template_registration_ok():
    validate_template_params(_template(), SeamRegistry())


def test_template_registration_rejects_undeclared_placeholder():
    bad = _template(policy={"mode": "${mode}", "root": "${undeclared}"})
    with pytest.raises(SeamError, match="undeclared params.*undeclared"):
        validate_template_params(bad, SeamRegistry())


def test_template_registration_rejects_unknown_seam():
    with pytest.raises(SeamError, match="unknown seam"):
        validate_template_params(_template(seam="nope.v9"), SeamRegistry())


def test_template_registration_rejects_provider_seam_mismatch():
    with pytest.raises(SeamError, match="implements"):
        validate_template_params(_template(provider="relay-web"), SeamRegistry())


def test_template_registration_rejects_unknown_param_type():
    with pytest.raises(SeamError, match="unknown type"):
        validate_template_params(
            _template(params=[SeamParamSpec(name="mode", type="yaml")]), SeamRegistry()
        )


def test_template_registration_optional_param_needs_default():
    with pytest.raises(SeamError, match="needs a default"):
        validate_template_params(
            _template(params=[SeamParamSpec(name="mode", required=False)]), SeamRegistry()
        )


# ---------------------------------------------------------- instance params


def test_resolve_params_applies_defaults():
    tpl = _template(
        params=[
            SeamParamSpec(name="mode", type="string", required=True),
            SeamParamSpec(name="label", type="string", required=False, default="defaulted"),
        ]
    )
    assert resolve_params(tpl, {"mode": "write"}) == {"mode": "write", "label": "defaulted"}


def test_resolve_params_rejects_unknown_key():
    with pytest.raises(SeamError, match="not declared"):
        resolve_params(_template(), {"mode": "write", "extra": 1})


def test_resolve_params_rejects_missing_required():
    with pytest.raises(SeamError, match="requires param 'mode'"):
        resolve_params(_template(), {})


def test_resolve_params_rejects_type_mismatch():
    with pytest.raises(SeamError, match="wants string"):
        resolve_params(_template(), {"mode": 42})


def test_resolve_params_refuses_bool_as_int():
    tpl = _template(
        params=[SeamParamSpec(name="maxBytes", type="int", required=True)]
    )
    with pytest.raises(SeamError, match="wants int"):
        resolve_params(tpl, {"maxBytes": True})


def test_render_template_materializes_decl():
    tpl = SeamTemplate(
        name="web-egress",
        seam="web.v1",
        provider="relay-web",
        policy={"allowHosts": "${hosts}", "timeout_s": "${timeout_s}"},
        params=[
            SeamParamSpec(name="hosts", type="list", required=True),
            SeamParamSpec(name="timeout_s", type="int", required=False, default=30),
        ],
    )
    decl = render_template(tpl, {"hosts": ["api.example.com"]})
    assert decl.seam == "web.v1"
    assert decl.provider == "relay-web"
    # default fills the placeholder; undeclared body keys stay template-fixed
    assert decl.policy == {"allowHosts": ["api.example.com"], "timeout_s": 30}


# -------------------------------------------------------------------- catalog


async def test_catalog_template_roundtrip_and_update_keeps_created_at():
    catalog = SeamCatalog(MemoryMetadataStore())
    first = await catalog.put_template(_template())
    replaced = await catalog.put_template(_template(description="updated"))
    assert replaced.created_at == first.created_at
    got = await catalog.get_template("workspace-fs")
    assert got is not None and got.description == "updated"
    assert [t.name for t in await catalog.list_templates()] == ["workspace-fs"]
    await catalog.delete_template("workspace-fs")
    assert await catalog.get_template("workspace-fs") is None


async def test_catalog_put_template_validates_against_registry():
    catalog = SeamCatalog(MemoryMetadataStore())
    with pytest.raises(SeamError, match="undeclared"):
        await catalog.put_template(_template(policy={"mode": "${oops}"}))


async def test_catalog_instance_requires_known_template_and_valid_params():
    catalog = SeamCatalog(MemoryMetadataStore())
    with pytest.raises(SeamError, match="unknown template"):
        await catalog.put_instance(SeamInstance(name="i1", template="missing"))
    await catalog.put_template(_template())
    with pytest.raises(SeamError, match="requires param"):
        await catalog.put_instance(SeamInstance(name="i1", template="workspace-fs"))
    ok = await catalog.put_instance(
        SeamInstance(name="i1", template="workspace-fs", params={"mode": "read-only"})
    )
    assert ok.params == {"mode": "read-only"}


async def test_resolve_version_bindings_merges_and_dedups():
    catalog = SeamCatalog(MemoryMetadataStore())
    await catalog.put_template(_template())
    await catalog.put_template(
        _template(
            name="bash",
            seam="shell.v1",
            provider="sandbox-bash",
            policy={"mode": "${mode}"},
        )
    )
    await catalog.put_instance(
        SeamInstance(name="prod-fs", template="workspace-fs", params={"mode": "write"})
    )
    version = AgentVersion(
        id="ver_x",
        agent_id="agent_x",
        version="1",
        harness="echo",
        image_ref="echo",
        seam_instances=["prod-fs"],
        seam_bindings=[SeamBindingDecl(seam="shell.v1", provider="sandbox-bash")],
    )
    decls = await catalog.resolve_version_bindings(version)
    assert [d.seam for d in decls] == ["shell.v1", "fs.v1"]
    fs = decls[1]
    assert fs.policy == {"mode": "write"} and fs.instance == "prod-fs"
    assert decls[0].instance == ""


async def test_resolve_version_bindings_fails_closed():
    catalog = SeamCatalog(MemoryMetadataStore())
    version = AgentVersion(
        id="ver_x",
        agent_id="agent_x",
        version="1",
        harness="echo",
        image_ref="echo",
        seam_instances=["nope"],
    )
    with pytest.raises(SeamError, match="unknown seam instance"):
        await catalog.resolve_version_bindings(version)


async def test_resolve_version_bindings_rejects_seam_collision():
    catalog = SeamCatalog(MemoryMetadataStore())
    await catalog.put_template(_template())
    await catalog.put_instance(
        SeamInstance(name="prod-fs", template="workspace-fs", params={"mode": "write"})
    )
    version = AgentVersion(
        id="ver_x",
        agent_id="agent_x",
        version="1",
        harness="echo",
        image_ref="echo",
        seam_instances=["prod-fs"],
        seam_bindings=[SeamBindingDecl(seam="fs.v1", provider="sandbox-fs")],
    )
    with pytest.raises(SeamError, match="collides"):
        await catalog.resolve_version_bindings(version)


async def test_rendered_manifest_carries_instance_provenance():
    catalog = SeamCatalog(MemoryMetadataStore())
    await catalog.put_template(_template())
    await catalog.put_instance(
        SeamInstance(name="prod-fs", template="workspace-fs", params={"mode": "read-only"})
    )
    version = AgentVersion(
        id="ver_x",
        agent_id="agent_x",
        version="1",
        harness="echo",
        image_ref="echo",
        seam_instances=["prod-fs"],
    )
    resolved = version.model_copy(
        update={"seam_bindings": await catalog.resolve_version_bindings(version)}
    )
    manifest = SeamRenderer().render_manifest(resolved, "sess_1", "/ws")
    assert manifest.seams[0].instance == "prod-fs"
    assert manifest.seams[0].policy == {"mode": "read-only"}


# ------------------------------------------------------------ harness bundles


async def test_harness_bundles_builtin_fallback_and_shadowing():
    bundles = HarnessBundles(MemoryMetadataStore())
    got = await bundles.get("dsh")
    assert got is not None and got.harness == "dsh"
    assert "fs.v1" in got.native_seams
    # a stored doc with the same name shadows the builtin
    await bundles.put(
        got.model_copy(update={"description": "custom dsh image", "image_ref": "dsh-plus"})
    )
    shadowed = await bundles.get("dsh")
    assert shadowed is not None and shadowed.image_ref == "dsh-plus"
    listed = {b.name: b for b in await bundles.list()}
    assert set(listed) >= {"echo", "dsh"}
    assert listed["echo"].harness == "echo"


async def test_harness_bundles_resolve_fails_closed():
    bundles = HarnessBundles(MemoryMetadataStore())
    from whirlwind.core import HarnessError

    with pytest.raises(HarnessError, match="unknown harness bundle"):
        await bundles.resolve("nope")


def test_builtin_bundles_match_adapters():
    # dsh's native surface mirrors what DshAdapter._mount_native handles
    assert set(BUILTIN_HARNESS_BUNDLES["dsh"].native_seams) == {
        "fs.v1",
        "shell.v1",
        "memory.v1",
    }
    assert BUILTIN_HARNESS_BUNDLES["echo"].native_seams == []
