"""LocalRegistry integration tests: real venvs, real pip installs, real launches.

The echo image genuinely installs this repository into the image venv and the
launched sandbox speaks the wire protocol through that venv's interpreter —
no symlinking tricks, no PATH inheritance from the test process.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from argus.core.errors import Conflict
from argus.drivers import ExecSpec, ProcessDriver, SandboxSpec
from argus.harness.protocol import HarnessRpc
from argus.imaging import ImageBuild, ImageNotFound, LocalRegistry, echo_image_build

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_register_and_resolve_roundtrip(tmp_path: Path) -> None:
    registry = LocalRegistry(tmp_path)
    build = echo_image_build(REPO_ROOT)
    ref = await registry.register(build)
    assert ref == f"echo@{build.version}"

    bundle = await registry.resolve("echo")
    assert bundle.harness == "echo"
    assert Path(bundle.launcher[0]).is_file()
    assert bundle.launcher == [str(bundle.root / "venv" / "bin" / "python"), "-m", "argus.harness.echo_server"]
    # the manifest is the registry's source of truth
    manifest = json.loads((bundle.root / "manifest.json").read_text())
    assert manifest["name"] == "echo"
    assert "argus @ file://" in manifest["requirements"][0]


@pytest.mark.asyncio
async def test_register_twice_conflicts(tmp_path: Path) -> None:
    registry = LocalRegistry(tmp_path)
    await registry.register(echo_image_build(REPO_ROOT))
    with pytest.raises(Conflict):
        await registry.register(echo_image_build(REPO_ROOT))


@pytest.mark.asyncio
async def test_resolve_unknown_ref_raises(tmp_path: Path) -> None:
    registry = LocalRegistry(tmp_path)
    with pytest.raises(ImageNotFound):
        await registry.resolve("nope")


@pytest.mark.asyncio
async def test_exact_and_latest_version_resolution(tmp_path: Path) -> None:
    registry = LocalRegistry(tmp_path)
    for version in ("0.1.0", "0.2.0"):
        await registry.register(echo_image_build(REPO_ROOT, version=version))
    exact = await registry.resolve("echo@0.1.0")
    assert exact.ref == "echo@0.1.0"
    latest = await registry.resolve("echo")
    assert latest.ref == "echo@0.2.0"


@pytest.mark.asyncio
async def test_built_image_boots_and_speaks_protocol(tmp_path: Path) -> None:
    """End-to-end: image venv -> driver sandbox -> initialize handshake."""
    registry = LocalRegistry(tmp_path / "registry")
    await registry.register(echo_image_build(REPO_ROOT))
    bundle = await registry.resolve("echo")

    driver = ProcessDriver()
    spec = SandboxSpec(
        sandbox_id="sb-image",
        argv=bundle.launcher,
        bundle_root=bundle.root,
        workspace=tmp_path / "sandboxes" / "sb-image",
        env={"PYTHONUNBUFFERED": "1"},  # image env + this whitelist only
    )
    instance = await driver.create(spec)
    try:
        rpc = HarnessRpc(instance.process)
        rpc.start()
        result = await rpc.request("initialize", {"cwd": str(spec.workspace)})
        assert result["serverInfo"]["name"] == "echo-harness"
        # proves the interpreter really is the image venv's, not the test's
        probe = await driver.exec(
            "sb-image",
            ExecSpec(
                argv=[str(bundle.root / "venv" / "bin" / "python"), "-c", "import sys; print(sys.prefix)"]
            ),
        )
        assert probe.exit_code == 0
        assert str(bundle.root / "venv") in probe.stdout
        assert sys.prefix != str(bundle.root / "venv")
    finally:
        await driver.destroy("sb-image")


@pytest.mark.asyncio
async def test_files_and_env_placeholders_render(tmp_path: Path) -> None:
    registry = LocalRegistry(tmp_path)
    build = ImageBuild(
        name="tiny",
        version="1.0.0",
        harness="echo",
        requirements=["packaging"],  # small real package
        launcher_argv=["{python}", "{root}/probe.py"],
        env={"PROBE_ROOT": "{root}", "PROBE_SP": "{site_packages}"},
        files={"probe.py": "import sys; print(sys.argv[1])\n"},
    )
    await registry.register(build)
    bundle = await registry.resolve("tiny@1.0.0")
    assert (bundle.root / "probe.py").is_file()
    assert bundle.env["PROBE_ROOT"] == str(bundle.root)
    assert "site-packages" in bundle.env["PROBE_SP"]
    # packaging really installed in the image venv
    probe = await LocalRegistry._run(
        [bundle.launcher[0], "-c", "import packaging; print('ok')"], bundle.root
    )
    assert "ok" in probe
