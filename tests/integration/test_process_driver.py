"""ProcessDriver integration tests: real child processes, real filesystems.

Every sandbox below is a real process group spawned by the driver; pause/resume
use real POSIX signals, checkpoints are real directory copies verified by
content digests. No mocks anywhere.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

import pytest

from whirlwind.core import SnapshotKind
from whirlwind.core.errors import WhirlwindError
from whirlwind.core.platform import current_facts
from whirlwind.drivers import (
    Density,
    DriverError,
    ExecSpec,
    Isolation,
    ProcessDriver,
    Resources,
    SandboxNotFound,
    UnsupportedCapability,
)

SRC = str(Path(__file__).resolve().parents[2] / "src")
ECHO_ARGV = [sys.executable, "-m", "whirlwind.harness.echo_server"]
BASE_ENV = {"PYTHONPATH": SRC, "PYTHONUNBUFFERED": "1"}

# RLIMIT_AS (virtual-memory ceiling) is a Linux enforcement path: the kernel on
# macOS rejects the soft=hard form of setrlimit(RLIMIT_AS, x) with "current
# limit exceeds maximum limit", so the process driver's platform policy drops
# memory ceilings there instead of silently claiming them (green/truthful:
# never claim stronger than declared). RLIMIT_NPROC / RLIMIT_CPU DO work on
# macOS. Single source of truth: core/platform facts (ADR-0007).
RLIMIT_AS_SUPPORTED = current_facts().rlimit_as_supported


def _spec(tmp_path: Path, sandbox_id: str, argv: list[str], env: dict | None = None) -> object:
    from whirlwind.drivers import SandboxSpec

    return SandboxSpec(
        sandbox_id=sandbox_id,
        argv=argv,
        bundle_root=tmp_path / "bundle",
        workspace=tmp_path / "sandboxes" / sandbox_id,
        env=dict(env or BASE_ENV),
    )


async def _read_json_line(stream: asyncio.StreamReader, timeout: float = 5.0) -> dict:
    line = await asyncio.wait_for(stream.readline(), timeout=timeout)
    return json.loads(line.decode())


@pytest.mark.asyncio
async def test_capabilities_reported_truthfully() -> None:
    caps = ProcessDriver().capabilities()
    assert caps.isolation == Isolation.PROCESS
    assert caps.snapshot_data is True
    assert caps.snapshot_full is False
    assert caps.net_policy is False
    assert caps.density == Density.HIGH


@pytest.mark.asyncio
async def test_create_spawns_real_process_in_workspace(tmp_path: Path) -> None:
    driver = ProcessDriver()
    spec = _spec(tmp_path, "sb-create", ECHO_ARGV)
    instance = await driver.create(spec)
    try:
        assert instance.pid is not None and instance.pid > 0
        # the process group leader is the child itself (start_new_session)
        assert os.getpgid(instance.pid) == instance.pid
        # stdio attach: Hostlet-style initialize handshake over the pipes
        assert instance.process is not None and instance.process.stdin is not None
        instance.process.stdin.write(
            (json.dumps({"jsonrpc": "2.0", "id": "1", "method": "initialize", "params": {}}) + "\n").encode()
        )
        await instance.process.stdin.drain()
        reply = await _read_json_line(instance.process.stdout)
        assert reply["result"]["serverInfo"]["name"] == "echo-harness"
    finally:
        await driver.destroy("sb-create")


@pytest.mark.asyncio
async def test_env_is_whitelist_not_inheritance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHIRLWIND_HOST_SECRET", "do-not-leak")
    driver = ProcessDriver()
    env = {**BASE_ENV, "WHIRLWIND_SANDBOX_MARK": "42"}
    spec = _spec(tmp_path, "sb-env", ["/bin/sleep", "30"], env=env)
    await driver.create(spec)
    try:
        result = await driver.exec("sb-env", ExecSpec(argv=["/usr/bin/env"]))
        assert result.exit_code == 0
        assert "WHIRLWIND_SANDBOX_MARK=42" in result.stdout
        assert "WHIRLWIND_HOST_SECRET" not in result.stdout
        var_names = {line.split("=", 1)[0] for line in result.stdout.splitlines() if "=" in line}
        assert "PATH" not in var_names  # even PATH does not leak
    finally:
        await driver.destroy("sb-env")


# ------------------------------------------------- resource limits (ADR-0005 D1)


def _limits_spec(tmp_path: Path, sandbox_id: str, argv: list[str], resources: "Resources"):
    from whirlwind.drivers import SandboxSpec

    return SandboxSpec(
        sandbox_id=sandbox_id,
        argv=argv,
        bundle_root=tmp_path / "bundle",
        workspace=tmp_path / "sandboxes" / sandbox_id,
        env=dict(BASE_ENV),
        resources=resources,
    )


@pytest.mark.asyncio
@pytest.mark.skipif(
    not RLIMIT_AS_SUPPORTED,
    reason="RLIMIT_AS (VA ceiling) cannot be enforced as soft=hard on macOS; "
    "code path is Linux-only (honest: never claim stronger than declared)",
)
async def test_resources_mem_limit_kills_allocation(tmp_path: Path) -> None:
    """A sandbox that allocates past its RLIMIT_AS dies of MemoryError —
    real kernel enforcement, not a declared number."""
    driver = ProcessDriver()
    argv = [
        sys.executable,
        "-c",
        "buf = bytearray(1024 * 1024 * 1024)\nprint('allocated', len(buf))",
    ]
    # 512MB VA ceiling: interpreter + 1GB allocation cannot fit
    spec = _limits_spec(tmp_path, "sb-mem", argv, Resources(mem_limit_mb=512))
    instance = await driver.create(spec)
    try:
        assert instance.process is not None
        await asyncio.wait_for(instance.process.wait(), timeout=30)
        assert instance.process.returncode not in (0, None)  # died, not succeeded
    finally:
        await driver.destroy("sb-mem")


@pytest.mark.asyncio
async def test_resources_cpu_seconds_kills_busy_loop(tmp_path: Path) -> None:
    """A busy loop past its RLIMIT_CPU budget is killed by the kernel.
    With soft==hard, SIGXCPU and SIGKILL race — either signal death proves
    the budget was enforced."""
    driver = ProcessDriver()
    argv = [sys.executable, "-c", "while True: pass"]
    spec = _limits_spec(tmp_path, "sb-cpu", argv, Resources(cpu_seconds=1))
    instance = await driver.create(spec)
    try:
        assert instance.process is not None
        await asyncio.wait_for(instance.process.wait(), timeout=30)
        assert instance.process.returncode in (-9, -24)  # SIGKILL or SIGXCPU
    finally:
        await driver.destroy("sb-cpu")


@pytest.mark.asyncio
async def test_resources_rlimits_visible_in_proc(tmp_path: Path) -> None:
    """The applied rlimits are visible inside the sandbox.

    RLIMIT_NPROC / RLIMIT_CPU are enforced on both macOS and Linux (asserted
    here); the RLIMIT_AS (VA ceiling) assertion is Linux-only for the memory
    path (see RLIMIT_AS_SUPPORTED). Verified via resource.getrlimit which is
    portable — /proc/self/limits is Linux-only."""
    driver = ProcessDriver()
    # print the RLIMIT_AS / RLIMIT_NPROC soft limits as JSON for portability
    argv = [
        sys.executable,
        "-c",
        "import resource, json; "
        "print(json.dumps({'as': resource.getrlimit(resource.RLIMIT_AS), "
        "'nproc': resource.getrlimit(resource.RLIMIT_NPROC)}))",
    ]
    spec = _limits_spec(
        tmp_path, "sb-limits", argv, Resources(mem_limit_mb=256, pids_max=256)
    )
    instance = await driver.create(spec)
    try:
        assert instance.process is not None and instance.process.stdout is not None
        out = (await asyncio.wait_for(instance.process.stdout.read(), timeout=30)).decode()
        import json as _json

        limits = _json.loads(out.strip())
        if RLIMIT_AS_SUPPORTED:
            # 256MB in bytes = 268435456 (soft, hard may be capped by the host)
            assert limits["as"][0] == 268435456
        # RLIMIT_NPROC ceiling is enforced on both platforms
        assert limits["nproc"][0] == 256
    finally:
        await driver.destroy("sb-limits")


@pytest.mark.asyncio
async def test_resources_none_mean_no_limits(tmp_path: Path) -> None:
    """Default Resources() adds no preexec_fn — behavior identical to before."""
    driver = ProcessDriver()
    spec = _limits_spec(tmp_path, "sb-nolimit", ["/bin/sleep", "30"], Resources())
    instance = await driver.create(spec)
    try:
        argv = [
            sys.executable,
            "-c",
            "import resource; "
            "print(resource.getrlimit(resource.RLIMIT_AS)[0] == resource.RLIM_INFINITY)",
        ]
        result = await driver.exec(
            "sb-nolimit", ExecSpec(argv=argv, env_extra=BASE_ENV)
        )
        assert result.exit_code == 0
        assert "True" in result.stdout.strip()  # address space stays unlimited
    finally:
        await driver.destroy("sb-nolimit")


@pytest.mark.asyncio
async def test_exec_runs_cwd_pinned_to_workspace(tmp_path: Path) -> None:
    driver = ProcessDriver()
    spec = _spec(tmp_path, "sb-cwd", ["/bin/sleep", "30"])
    await driver.create(spec)
    try:
        result = await driver.exec("sb-cwd", ExecSpec(argv=["/bin/pwd"]))
        assert result.exit_code == 0
        assert Path(result.stdout.strip()).resolve() == spec.workspace.resolve()
    finally:
        await driver.destroy("sb-cwd")


@pytest.mark.asyncio
async def test_pause_and_resume_with_real_signals(tmp_path: Path) -> None:
    driver = ProcessDriver()
    spec = _spec(tmp_path, "sb-pause", ECHO_ARGV)
    instance = await driver.create(spec)
    try:
        await driver.pause("sb-pause")
        assert instance.paused is True
        assert instance.process is not None and instance.process.stdin is not None
        instance.process.stdin.write(
            (json.dumps({"jsonrpc": "2.0", "id": "1", "method": "initialize", "params": {}}) + "\n").encode()
        )
        await instance.process.stdin.drain()
        with pytest.raises(asyncio.TimeoutError):
            await _read_json_line(instance.process.stdout, timeout=0.4)  # SIGSTOPped: silence

        await driver.resume("sb-pause")
        reply = await _read_json_line(instance.process.stdout, timeout=5.0)
        assert reply["result"]["serverInfo"]["name"] == "echo-harness"
        assert instance.paused is False
    finally:
        await driver.destroy("sb-pause")


@pytest.mark.asyncio
async def test_data_checkpoint_roundtrip(tmp_path: Path) -> None:
    driver = ProcessDriver(snapshots_root=tmp_path / "snapshots")
    spec = _spec(tmp_path, "sb-snap", ["/bin/sleep", "30"])
    await driver.create(spec)
    (spec.workspace / "data.txt").write_text("state-42")
    (spec.workspace / "sub").mkdir()
    (spec.workspace / "sub" / "nested.json").write_text('{"k": 1}')

    artifact = await driver.checkpoint("sb-snap", SnapshotKind.DATA)
    try:
        assert artifact.kind == SnapshotKind.DATA
        assert artifact.subject == "sb-snap"
        assert artifact.size > 0
        assert artifact.manifest["files"] == 2
        assert (artifact.path / "data.txt").read_text() == "state-42"

        # diverge the live workspace after the checkpoint
        (spec.workspace / "data.txt").write_text("state-mutated")
        (spec.workspace / "extra.log").write_text("noise")

        # restore into a fresh sandbox
        spec2 = _spec(tmp_path, "sb-restore", ["/bin/sleep", "30"])
        await driver.create(spec2, from_snapshot=artifact)
        try:
            assert (spec2.workspace / "data.txt").read_text() == "state-42"
            assert (spec2.workspace / "sub" / "nested.json").read_text() == '{"k": 1}'
            assert not (spec2.workspace / "extra.log").exists()  # post-snapshot drift absent
        finally:
            await driver.destroy("sb-restore")
    finally:
        await driver.destroy("sb-snap")


@pytest.mark.asyncio
async def test_full_snapshot_rejected_as_unsupported(tmp_path: Path) -> None:
    driver = ProcessDriver(snapshots_root=tmp_path / "snapshots")
    spec = _spec(tmp_path, "sb-full", ["/bin/sleep", "30"])
    await driver.create(spec)
    try:
        with pytest.raises(UnsupportedCapability):
            await driver.checkpoint("sb-full", SnapshotKind.FULL)
    finally:
        await driver.destroy("sb-full")


@pytest.mark.asyncio
async def test_destroy_reaps_process_group(tmp_path: Path) -> None:
    driver = ProcessDriver()
    spec = _spec(tmp_path, "sb-kill", ECHO_ARGV)
    instance = await driver.create(spec)
    await driver.destroy("sb-kill", grace_s=2.0)
    assert instance.process is not None
    assert instance.process.returncode is not None
    with pytest.raises(SandboxNotFound):
        driver.instance("sb-kill")


@pytest.mark.asyncio
async def test_errors_on_unknown_sandbox_and_bad_launcher(tmp_path: Path) -> None:
    driver = ProcessDriver()
    with pytest.raises(SandboxNotFound):
        await driver.exec("nope", ExecSpec(argv=["/bin/true"]))
    with pytest.raises(DriverError):
        await driver.create(_spec(tmp_path, "sb-bad", ["/nonexistent/launcher"]))


@pytest.mark.asyncio
async def test_merkle_root_detects_content_change(tmp_path: Path) -> None:
    driver = ProcessDriver(snapshots_root=tmp_path / "snapshots")
    spec = _spec(tmp_path, "sb-merkle", ["/bin/sleep", "30"])
    await driver.create(spec)
    (spec.workspace / "f.txt").write_text("aaa")
    a1 = await driver.checkpoint("sb-merkle", SnapshotKind.DATA)
    (spec.workspace / "f.txt").write_text("bbb")
    a2 = await driver.checkpoint("sb-merkle", SnapshotKind.DATA)
    try:
        assert a1.merkle != a2.merkle
        assert a1.size == a2.size  # same byte count, different content
    finally:
        await driver.destroy("sb-merkle")
