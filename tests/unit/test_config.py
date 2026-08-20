"""Unit tests for the unified configuration loader (ADR-0009).

Pure logic against real files: the precedence ladder (defaults < TOML < env <
CLI), discovery order, schema validation, compound-value round-trips. No
mocks — every layer is a real tmp file or a real env var via monkeypatch.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from whirlwind.config import (
    DEFAULT_CONFIG_FILENAME,
    ConfigError,
    discover_config_path,
    load_settings,
    render_toml,
)
from whirlwind.core.errors import WhirlwindError
from whirlwind.drivers import Resources


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every WHIRLWIND_* env var is a precedence layer — strip the host's, and
    chdir into a clean dir so the conventional ./whirlwind.toml never leaks in
    from the repo checkout."""
    for name in list(os.environ):
        if name.startswith("WHIRLWIND_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


# ------------------------------------------------------------ defaults layer


def test_defaults_with_no_layers(tmp_path: Path) -> None:
    settings = load_settings()
    assert settings.config_path is None
    assert settings.server.host == "127.0.0.1"
    assert settings.server.port == 8410
    assert settings.runtime.data_dir == tmp_path / ".whirlwind"  # resolved absolute
    assert settings.runtime.api_key_env == "DEEPSEEK_API_KEY"
    assert settings.runtime.llm_upstream == "https://api.deepseek.com"
    assert settings.runtime.metadata_backend == "memory"
    assert settings.runtime.kv_backend == "memory"
    assert settings.runtime.max_live_sessions is None
    assert settings.runtime.sandbox_resources is None
    assert settings.runtime.warm_pool is None
    assert settings.logging.level == "INFO"
    assert settings.logging.format == "json"
    assert settings.logging.environment == "unknown"


def test_data_dir_resolves_absolute() -> None:
    settings = load_settings(cli={"runtime.data_dir": "some/where"})
    assert settings.runtime.data_dir.is_absolute()
    assert settings.runtime.data_dir == Path.cwd() / "some" / "where"


# --------------------------------------------------------- precedence ladder


def test_toml_overrides_defaults(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path / "whirlwind.toml",
        '[server]\nport = 9000\nhost = "0.0.0.0"\n\n[runtime]\napi_key_env = "MY_KEY"\n',
    )
    settings = load_settings(config_path=cfg)
    assert settings.config_path == cfg.resolve()
    assert settings.server.port == 9000
    assert settings.server.host == "0.0.0.0"
    assert settings.runtime.api_key_env == "MY_KEY"
    assert settings.runtime.llm_upstream == "https://api.deepseek.com"  # untouched default


def test_env_overrides_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _write(tmp_path / "whirlwind.toml", '[server]\nport = 9000\n')
    monkeypatch.setenv("WHIRLWIND_PORT", "9001")
    settings = load_settings(config_path=cfg)
    assert settings.server.port == 9001


def test_cli_overrides_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _write(tmp_path / "whirlwind.toml", '[server]\nport = 9000\n')
    monkeypatch.setenv("WHIRLWIND_PORT", "9001")
    settings = load_settings(config_path=cfg, cli={"server.port": 9002})
    assert settings.server.port == 9002


def test_cli_none_sentinel_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """argparse flags not given arrive as None — that must mean 'absent', not 'null out'."""
    monkeypatch.setenv("WHIRLWIND_PORT", "9001")
    settings = load_settings(cli={"server.port": None, "runtime.data_dir": None})
    assert settings.server.port == 9001
    assert settings.runtime.data_dir == Path.cwd() / ".whirlwind"


def test_empty_env_value_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHIRLWIND_API_KEY_ENV", "")
    settings = load_settings()
    assert settings.runtime.api_key_env == "DEEPSEEK_API_KEY"


def test_env_int_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHIRLWIND_PORT", "9001")
    monkeypatch.setenv("WHIRLWIND_MAX_LIVE_SESSIONS", "7")
    settings = load_settings()
    assert settings.server.port == 9001
    assert settings.runtime.max_live_sessions == 7


def test_logging_env_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHIRLWIND_LOG_LEVEL", "debug")
    monkeypatch.setenv("WHIRLWIND_LOG_FORMAT", "text")
    monkeypatch.setenv("WHIRLWIND_LOG_ENV", "production")
    settings = load_settings()
    assert settings.logging.level == "DEBUG"  # case-normalized
    assert settings.logging.format == "text"
    assert settings.logging.environment == "production"


def test_logging_level_enum_is_validated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHIRLWIND_LOG_LEVEL", "LOUD")
    with pytest.raises(ConfigError, match="logging.level must be one of"):
        load_settings()


def test_logging_format_unknown_value_is_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHIRLWIND_LOG_FORMAT", "yaml")
    with pytest.raises(ConfigError, match="logging.format must be one of"):
        load_settings()


def test_env_int_garbage_is_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHIRLWIND_PORT", "not-a-port")
    with pytest.raises(ConfigError, match="WHIRLWIND_PORT"):
        load_settings()


# ----------------------------------------------------------- discovery order


def test_discovery_explicit_beats_env_and_conventional(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = _write(tmp_path / "explicit.toml", '[server]\nport = 1\n')
    via_env = _write(tmp_path / "via-env.toml", '[server]\nport = 2\n')
    _write(tmp_path / DEFAULT_CONFIG_FILENAME, '[server]\nport = 3\n')
    monkeypatch.setenv("WHIRLWIND_CONFIG", str(via_env))
    assert discover_config_path(explicit) == explicit.resolve()
    assert load_settings(config_path=explicit).server.port == 1


def test_discovery_env_beats_conventional(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    via_env = _write(tmp_path / "via-env.toml", '[server]\nport = 2\n')
    _write(tmp_path / DEFAULT_CONFIG_FILENAME, '[server]\nport = 3\n')
    monkeypatch.setenv("WHIRLWIND_CONFIG", str(via_env))
    assert discover_config_path() == via_env.resolve()
    assert load_settings().server.port == 2


def test_discovery_conventional_when_present(tmp_path: Path) -> None:
    conventional = _write(tmp_path / DEFAULT_CONFIG_FILENAME, '[server]\nport = 3\n')
    assert discover_config_path() == conventional.resolve()
    assert load_settings().server.port == 3


def test_discovery_none_when_absent(tmp_path: Path) -> None:
    assert discover_config_path() is None
    assert load_settings().config_path is None


def test_explicit_missing_file_is_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="config file not found"):
        load_settings(config_path=tmp_path / "nope.toml")


def test_env_config_missing_file_is_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHIRLWIND_CONFIG", str(tmp_path / "nope.toml"))
    with pytest.raises(ConfigError, match="WHIRLWIND_CONFIG"):
        load_settings()


# --------------------------------------------------------- schema validation


def test_unknown_section_is_error(tmp_path: Path) -> None:
    cfg = _write(tmp_path / "whirlwind.toml", "[nope]\nport = 1\n")
    with pytest.raises(ConfigError, match="unknown config section"):
        load_settings(config_path=cfg)


def test_unknown_key_is_error(tmp_path: Path) -> None:
    cfg = _write(tmp_path / "whirlwind.toml", '[server]\nprot = 8410\n')  # typo
    with pytest.raises(ConfigError, match=r"\[server\].prot"):
        load_settings(config_path=cfg)


def test_wrong_type_is_error(tmp_path: Path) -> None:
    cfg = _write(tmp_path / "whirlwind.toml", '[server]\nport = "eight"\n')
    with pytest.raises(ConfigError, match="server.port must be an integer"):
        load_settings(config_path=cfg)


def test_bool_is_not_an_int(tmp_path: Path) -> None:
    cfg = _write(tmp_path / "whirlwind.toml", "[sandbox]\nmax_live_sessions = true\n")
    with pytest.raises(ConfigError, match="an integer"):
        load_settings(config_path=cfg)


def test_bad_toml_is_error(tmp_path: Path) -> None:
    cfg = _write(tmp_path / "whirlwind.toml", "[server\nport = ")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_settings(config_path=cfg)


def test_section_not_a_table_is_error(tmp_path: Path) -> None:
    cfg = _write(tmp_path / "whirlwind.toml", "server = 5\n")
    with pytest.raises(ConfigError, match=r"\[server\] must be a table"):
        load_settings(config_path=cfg)


def test_unknown_cli_key_is_error() -> None:
    with pytest.raises(ConfigError, match="unknown CLI override key"):
        load_settings(cli={"server.nope": 1})


def test_config_error_is_whirlwind_error() -> None:
    assert issubclass(ConfigError, WhirlwindError)
    assert ConfigError.code == "whirlwind/config"


# ------------------------------------------------------- compound round-trips


def test_warm_pool_and_resources_round_trip(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path / "whirlwind.toml",
        "\n".join([
            "[runtime.warm_pool]",
            '"ver_abc" = 2',
            '"ver_def" = 1',
            "",
            "[sandbox.resources]",
            "mem_limit_mb = 512",
            "cpu_seconds = 3600",
            "pids_max = 256",
            "",
        ]),
    )
    settings = load_settings(config_path=cfg)
    assert settings.runtime.warm_pool == {"ver_abc": 2, "ver_def": 1}
    assert settings.runtime.sandbox_resources == Resources(mem_limit_mb=512, cpu_seconds=3600, pids_max=256)


def test_warm_pool_value_type_is_error(tmp_path: Path) -> None:
    cfg = _write(tmp_path / "whirlwind.toml", '[runtime.warm_pool]\n"ver_abc" = "two"\n')
    with pytest.raises(ConfigError, match="min_warm integers"):
        load_settings(config_path=cfg)


def test_resources_unknown_key_is_error(tmp_path: Path) -> None:
    cfg = _write(tmp_path / "whirlwind.toml", "[sandbox.resources]\nmem_limit_mb = 512\nram = 1\n")
    with pytest.raises(ConfigError, match="sandbox.resources has unknown key 'ram'"):
        load_settings(config_path=cfg)


# ------------------------------------------------------------------ rendering


def test_render_toml_reports_provenance(tmp_path: Path) -> None:
    cfg = _write(tmp_path / "whirlwind.toml", '[server]\nport = 9000\n')
    text = render_toml(load_settings(config_path=cfg))
    assert str(cfg.resolve()) in text
    assert "port = 9000" in text
    assert "max_live_sessions: unset" in text  # unset keys stay visible comments


def test_render_toml_round_trips_through_the_loader(tmp_path: Path) -> None:
    """The rendered effective config must itself be loadable (minus the comment
    header) — `config show` output is a valid starting point for a real file."""
    cfg = _write(
        tmp_path / "whirlwind.toml",
        '[server]\nport = 9000\n\n[runtime.warm_pool]\n"ver_abc" = 2\n\n[sandbox.resources]\nmem_limit_mb = 512\n',
    )
    rendered = render_toml(load_settings(config_path=cfg))
    body = "\n".join(line for line in rendered.splitlines() if not line.startswith("#"))
    again = _write(tmp_path / "roundtrip.toml", body + "\n")
    first = load_settings(config_path=cfg)
    second = load_settings(config_path=again)
    assert first.server == second.server
    assert first.runtime.warm_pool == second.runtime.warm_pool
    assert first.runtime.sandbox_resources == second.runtime.sandbox_resources


def test_shipped_example_config_loads(tmp_path: Path) -> None:
    """deploy/whirlwind.example.toml is operator-facing documentation — it must
    always pass the real loader (schema + types)."""
    example = Path(__file__).resolve().parents[2] / "deploy" / "whirlwind.example.toml"
    settings = load_settings(config_path=example)
    assert settings.server == load_settings().server  # example mirrors defaults
    assert settings.runtime.api_key_env == "DEEPSEEK_API_KEY"
