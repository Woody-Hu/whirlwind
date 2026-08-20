"""Integration: `whirlwind config show` / `serve` config resolution as real
subprocesses (ADR-0009 D6/D7). No mocks — the console entrypoint runs for real,
real TOML files on disk, real env vars in the child process.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


async def _run(
    *args: str, env_extra: dict[str, str] | None = None, timeout_s: float = 60.0
) -> tuple[int, str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("WHIRLWIND_")}
    env.update(env_extra or {})
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "whirlwind.cli",
        *args,
        cwd=str(REPO_ROOT),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    return proc.returncode, out.decode(), err.decode()


@pytest.mark.asyncio
async def test_config_show_reflects_file_and_env(tmp_path: Path) -> None:
    cfg = tmp_path / "whirlwind.toml"
    cfg.write_text('[server]\nport = 9000\n\n[runtime]\napi_key_env = "MY_KEY"\n')
    code, out, err = await _run(
        "config", "show", "--config", str(cfg),
        env_extra={"WHIRLWIND_DATA_DIR": str(tmp_path / "state")},
    )
    assert code == 0, err
    assert str(cfg) in out  # provenance header names the loaded file
    assert "port = 9000" in out  # from the file
    assert 'api_key_env = "MY_KEY"' in out
    assert f'data_dir = "{tmp_path / "state"}"' in out  # env layer overrides defaults


@pytest.mark.asyncio
async def test_config_show_defaults_when_no_layers(tmp_path: Path) -> None:
    code, out, err = await _run("config", "show", env_extra={"WHIRLWIND_DATA_DIR": str(tmp_path)})
    assert code == 0, err
    assert "<no config file: defaults + env + cli>" in out
    assert "port = 8410" in out
    assert "metadata_backend = \"memory\"" in out


@pytest.mark.asyncio
async def test_config_show_bad_file_exits_nonzero(tmp_path: Path) -> None:
    cfg = tmp_path / "whirlwind.toml"
    cfg.write_text("[nope]\nx = 1\n")
    code, out, err = await _run("config", "show", "--config", str(cfg))
    assert code == 1
    assert "unknown config section" in out + err


@pytest.mark.asyncio
async def test_serve_rejects_bad_config_instead_of_booting_defaults(tmp_path: Path) -> None:
    """serve must fail fast (exit 1) on an invalid config — a typo'd file may
    never silently fall back to defaults and bind the wrong surface."""
    cfg = tmp_path / "whirlwind.toml"
    cfg.write_text('[server]\nport = "eight"\n')
    code, out, err = await _run("serve", "--config", str(cfg), timeout_s=30)
    assert code == 1
    assert "server.port must be an integer" in out + err
