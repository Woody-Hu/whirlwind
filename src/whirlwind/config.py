"""Unified configuration loading (ADR-0009).

One TOML file, layered injection:

    code defaults  <  TOML file  <  WHIRLWIND_* env vars  <  explicit CLI flags

The loader produces a frozen `Settings` (uvicorn bind + `RuntimeConfig` +
provenance). `RuntimeConfig` keeps its signature: tests and embedders
construct it directly; this module is the *only* place operator-facing
defaults are spelled out.

Deliberately NOT config-file values (ADR-0009 D8): secret *values* (only the
env var *name* is configured, via `runtime.api_key_env`), sandbox-internal
injected vars, `WHIRLWIND_PLATFORM` (ADR-0007, env-only identity simulation),
`WHIRLWIND_URL` (client-side), and harness test knobs.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from whirlwind.core.errors import WhirlwindError
from whirlwind.drivers import Resources

if TYPE_CHECKING:
    from whirlwind.runtime import RuntimeConfig

DEFAULT_CONFIG_FILENAME = "whirlwind.toml"
ENV_CONFIG_PATH = "WHIRLWIND_CONFIG"


class ConfigError(WhirlwindError):
    code = "whirlwind/config"


@dataclass(frozen=True, slots=True)
class ServerSettings:
    host: str = "127.0.0.1"
    port: int = 8410


@dataclass(frozen=True, slots=True)
class LoggingSettings:
    level: str = "INFO"  # DEBUG | INFO | WARNING | ERROR | CRITICAL
    format: str = "json"  # json | text (ADR-0013 P2.2)
    environment: str = "unknown"  # injected into every structured log record


@dataclass(frozen=True, slots=True)
class Settings:
    server: ServerSettings
    runtime: "RuntimeConfig"
    config_path: Path | None  # provenance: the file that was loaded, if any
    logging: LoggingSettings = LoggingSettings()


# ---------------------------------------------------------------- schema


_DEFAULTS: dict[str, Any] = {
    "server.host": "127.0.0.1",
    "server.port": 8410,
    "logging.level": "INFO",
    "logging.format": "json",
    "logging.environment": "unknown",
    "runtime.data_dir": ".whirlwind",
    "runtime.repo_root": None,
    "runtime.api_key_env": "DEEPSEEK_API_KEY",
    "runtime.secret_key_env": "WHIRLWIND_SECRET_KEY",
    "runtime.llm_upstream": "https://api.deepseek.com",
    "runtime.wheel_tick_ms": 20,
    "runtime.warm_pool": None,
    "storage.metadata_backend": "memory",
    "storage.postgres_dsn": None,
    "storage.kv_backend": "memory",
    "storage.redis_url": None,
    "sandbox.max_live_sessions": None,
    "sandbox.resources": None,
    "sandbox.driver": "process",
    "sandbox.snapshot_mode": "full",
    "sandbox.snapshot_chain_max": 16,
}

# section -> keys allowed inside it (ADR-0009 D4); unknown = hard error
_SCHEMA: dict[str, set[str]] = {
    "server": {"host", "port"},
    "logging": {"level", "format", "environment"},
    "runtime": {"data_dir", "repo_root", "api_key_env", "secret_key_env", "llm_upstream", "wheel_tick_ms", "warm_pool"},
    "storage": {"metadata_backend", "postgres_dsn", "kv_backend", "redis_url"},
    "sandbox": {"max_live_sessions", "resources", "driver", "snapshot_mode", "snapshot_chain_max"},
}

_ENV_TO_KEY: dict[str, str] = {
    "WHIRLWIND_HOST": "server.host",
    "WHIRLWIND_PORT": "server.port",
    "WHIRLWIND_LOG_LEVEL": "logging.level",
    "WHIRLWIND_LOG_FORMAT": "logging.format",
    "WHIRLWIND_LOG_ENV": "logging.environment",
    "WHIRLWIND_DATA_DIR": "runtime.data_dir",
    "WHIRLWIND_REPO_ROOT": "runtime.repo_root",
    "WHIRLWIND_API_KEY_ENV": "runtime.api_key_env",
    "WHIRLWIND_SECRET_KEY_ENV": "runtime.secret_key_env",
    "WHIRLWIND_LLM_UPSTREAM": "runtime.llm_upstream",
    "WHIRLWIND_WHEEL_TICK_MS": "runtime.wheel_tick_ms",
    "WHIRLWIND_METADATA_BACKEND": "storage.metadata_backend",
    "WHIRLWIND_POSTGRES_DSN": "storage.postgres_dsn",
    "WHIRLWIND_KV_BACKEND": "storage.kv_backend",
    "WHIRLWIND_REDIS_URL": "storage.redis_url",
    "WHIRLWIND_MAX_LIVE_SESSIONS": "sandbox.max_live_sessions",
    "WHIRLWIND_SANDBOX_DRIVER": "sandbox.driver",
    "WHIRLWIND_SNAPSHOT_MODE": "sandbox.snapshot_mode",
    "WHIRLWIND_SNAPSHOT_CHAIN_MAX": "sandbox.snapshot_chain_max",
}

_INT_KEYS = {"server.port", "runtime.wheel_tick_ms", "sandbox.max_live_sessions", "sandbox.snapshot_chain_max"}
_ENUM_KEYS: dict[str, set[str]] = {
    # closed value sets validated at load time (ADR-0012 D6); the caps-vs-mode
    # cross-check lives in the composition root, which owns the driver
    "sandbox.driver": {"process", "runsc", "microsandbox"},
    "sandbox.snapshot_mode": {"full", "delta"},
    "logging.level": {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"},
    "logging.format": {"json", "text"},
}
_STR_KEYS = {
    "server.host",
    "logging.environment",
    "runtime.data_dir",
    "runtime.api_key_env",
    "runtime.secret_key_env",
    "runtime.llm_upstream",
    "storage.metadata_backend",
    "storage.kv_backend",
    "sandbox.driver",
    "sandbox.snapshot_mode",
}
_STR_OR_NONE_KEYS = {"runtime.repo_root", "storage.postgres_dsn", "storage.redis_url"}
_RESOURCE_KEYS = {"mem_limit_mb", "cpu_seconds", "pids_max"}


# ---------------------------------------------------------------- loading


def discover_config_path(explicit: str | Path | None = None) -> Path | None:
    """`--config PATH` > `$WHIRLWIND_CONFIG` > `./whirlwind.toml` (if present) > None."""
    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        return path.resolve()
    env_value = os.environ.get(ENV_CONFIG_PATH, "").strip()
    if env_value:
        path = Path(env_value).expanduser()
        if not path.is_file():
            raise ConfigError(f"{ENV_CONFIG_PATH} points to a missing file: {path}")
        return path.resolve()
    conventional = Path.cwd() / DEFAULT_CONFIG_FILENAME
    return conventional if conventional.is_file() else None


def _load_file(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc

    unknown_sections = set(data) - set(_SCHEMA)
    if unknown_sections:
        raise ConfigError(f"unknown config section(s) in {path}: {', '.join(sorted(unknown_sections))}")

    flat: dict[str, Any] = {}
    for section, allowed_keys in _SCHEMA.items():
        body = data.get(section)
        if body is None:
            continue
        if not isinstance(body, dict):
            raise ConfigError(f"[{section}] must be a table in {path}")
        for key, value in body.items():
            if key not in allowed_keys:
                raise ConfigError(f"unknown config key in {path}: [{section}].{key}")
            flat[f"{section}.{key}"] = value
    return flat


def _validate(dotted: str, value: Any, layer: str) -> None:
    def fail(expected: str) -> None:
        raise ConfigError(f"{layer}: {dotted} must be {expected}, got {value!r}")

    # None = "unset": only the defaults layer can carry it (TOML has no null,
    # env vars are strings, CLI None sentinels are skipped in load_settings).
    if value is None:
        return

    if dotted in _INT_KEYS:
        if isinstance(value, bool) or not isinstance(value, int):
            fail("an integer")
        if dotted == "sandbox.snapshot_chain_max" and value < 1:
            raise ConfigError(f"{layer}: {dotted} must be >= 1, got {value!r}")
    elif dotted in _ENUM_KEYS:
        allowed = _ENUM_KEYS[dotted]
        if value not in allowed:
            raise ConfigError(f"{layer}: {dotted} must be one of {sorted(allowed)}, got {value!r}")
    elif dotted in _STR_KEYS:
        if not isinstance(value, str):
            fail("a string")
    elif dotted in _STR_OR_NONE_KEYS:
        if value is not None and not isinstance(value, str):
            fail("a string or null")
    elif dotted == "runtime.warm_pool":
        if value is None:
            return
        if not isinstance(value, dict):
            fail("a table of agent_version_id = min_warm integers")
        for key, entry in value.items():
            if not isinstance(key, str) or isinstance(entry, bool) or not isinstance(entry, int):
                fail("a table of agent_version_id = min_warm integers")
    elif dotted == "sandbox.resources":
        if value is None:
            return
        if not isinstance(value, dict):
            fail(f"a table with keys {sorted(_RESOURCE_KEYS)}")
        for key, entry in value.items():
            if key not in _RESOURCE_KEYS:
                raise ConfigError(f"{layer}: sandbox.resources has unknown key {key!r} (expected one of {sorted(_RESOURCE_KEYS)})")
            if isinstance(entry, bool) or not isinstance(entry, int):
                fail(f"an integer for sandbox.resources.{key}")


def load_settings(
    *,
    config_path: str | Path | None = None,
    cli: Mapping[str, Any] | None = None,
) -> Settings:
    """Resolve effective settings through the precedence ladder (ADR-0009 D1).

    `cli` maps dotted config keys to explicitly-given CLI values (None = flag
    not present, ignored); callers pass only what argparse actually received.
    """
    from whirlwind.runtime import RuntimeConfig  # lazy: keeps client-side CLI imports light

    resolved_path = discover_config_path(config_path)
    values: dict[str, Any] = dict(_DEFAULTS)

    if resolved_path is not None:
        values.update(_load_file(resolved_path))

    for env_name, dotted in _ENV_TO_KEY.items():
        raw = os.environ.get(env_name)
        if raw is None or raw == "":
            continue
        if dotted in _INT_KEYS:
            try:
                raw = int(raw)
            except ValueError:
                raise ConfigError(f"{env_name}: expected an integer, got {raw!r}") from None
        values[dotted] = raw

    if cli is not None:
        for dotted, value in cli.items():
            if dotted not in _DEFAULTS:
                raise ConfigError(f"unknown CLI override key: {dotted}")
            if value is None:
                continue
            values[dotted] = value

    # case-insensitive log level from any layer
    values["logging.level"] = str(values["logging.level"]).upper()

    for dotted, value in values.items():
        layer = f"config file {resolved_path}" if resolved_path is not None else "defaults"
        _validate(dotted, value, layer)

    def path_of(dotted: str) -> Path | None:
        raw = values[dotted]
        return Path(raw).expanduser().resolve() if raw else None

    warm_pool = values["runtime.warm_pool"]
    resources_raw = values["sandbox.resources"]
    runtime = RuntimeConfig(
        data_dir=Path(values["runtime.data_dir"]).expanduser().resolve(),
        repo_root=path_of("runtime.repo_root"),
        api_key_env=values["runtime.api_key_env"],
        secret_key_env=values["runtime.secret_key_env"],
        llm_upstream=values["runtime.llm_upstream"],
        wheel_tick_ms=values["runtime.wheel_tick_ms"],
        warm_pool=dict(warm_pool) if warm_pool else None,
        metadata_backend=values["storage.metadata_backend"],
        postgres_dsn=values["storage.postgres_dsn"],
        kv_backend=values["storage.kv_backend"],
        redis_url=values["storage.redis_url"],
        sandbox_resources=(
            Resources(
                mem_limit_mb=resources_raw.get("mem_limit_mb"),
                cpu_seconds=resources_raw.get("cpu_seconds"),
                pids_max=resources_raw.get("pids_max"),
            )
            if resources_raw
            else None
        ),
        max_live_sessions=values["sandbox.max_live_sessions"],
        driver=values["sandbox.driver"],
        snapshot_mode=values["sandbox.snapshot_mode"],
        snapshot_chain_max=values["sandbox.snapshot_chain_max"],
    )
    server = ServerSettings(host=values["server.host"], port=values["server.port"])
    logging = LoggingSettings(
        level=values["logging.level"],
        format=values["logging.format"],
        environment=values["logging.environment"],
    )
    return Settings(server=server, runtime=runtime, config_path=resolved_path, logging=logging)


# ---------------------------------------------------------------- rendering


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_toml(settings: Settings) -> str:
    """Effective settings as TOML; unset optional keys appear as comments
    (TOML has no null). Human-auditable and machine-parseable for what IS set."""
    runtime = settings.runtime
    lines: list[str] = []
    source = str(settings.config_path) if settings.config_path else "<no config file: defaults + env + cli>"
    lines.append(f"# effective configuration (loaded from: {source})")
    lines.append("")
    lines.append("[server]")
    lines.append(f"host = {_quote(settings.server.host)}")
    lines.append(f"port = {settings.server.port}")
    lines.append("")
    lines.append("[logging]")
    lines.append(f"level = {_quote(settings.logging.level)}")
    lines.append(f"format = {_quote(settings.logging.format)}")
    lines.append(f"environment = {_quote(settings.logging.environment)}")
    lines.append("")
    lines.append("[runtime]")
    lines.append(f"data_dir = {_quote(str(runtime.data_dir))}")
    if runtime.repo_root is not None:
        lines.append(f"repo_root = {_quote(str(runtime.repo_root))}")
    else:
        lines.append("# repo_root: unset (auto-detect)")
    lines.append(f"api_key_env = {_quote(runtime.api_key_env)}")
    lines.append(f"secret_key_env = {_quote(runtime.secret_key_env)}")
    lines.append(f"llm_upstream = {_quote(runtime.llm_upstream)}")
    lines.append(f"wheel_tick_ms = {runtime.wheel_tick_ms}")
    if runtime.warm_pool:
        lines.append("")
        lines.append("[runtime.warm_pool]")
        for version_id, minimum in runtime.warm_pool.items():
            lines.append(f"{_quote(version_id)} = {minimum}")
    else:
        lines.append("# warm_pool: unset (prewarming off)")
    lines.append("")
    lines.append("[storage]")
    lines.append(f"metadata_backend = {_quote(runtime.metadata_backend)}")
    if runtime.postgres_dsn is not None:
        lines.append(f"postgres_dsn = {_quote(runtime.postgres_dsn)}")
    else:
        lines.append("# postgres_dsn: unset")
    lines.append(f"kv_backend = {_quote(runtime.kv_backend)}")
    if runtime.redis_url is not None:
        lines.append(f"redis_url = {_quote(runtime.redis_url)}")
    else:
        lines.append("# redis_url: unset")
    lines.append("")
    lines.append("[sandbox]")
    if runtime.max_live_sessions is not None:
        lines.append(f"max_live_sessions = {runtime.max_live_sessions}")
    else:
        lines.append("# max_live_sessions: unset (uncapped)")
    lines.append(f"driver = {_quote(runtime.driver)}")
    lines.append(f"snapshot_mode = {_quote(runtime.snapshot_mode)}")
    lines.append(f"snapshot_chain_max = {runtime.snapshot_chain_max}")
    resources = runtime.sandbox_resources
    if resources is not None:
        lines.append("")
        lines.append("[sandbox.resources]")
        for key in ("mem_limit_mb", "cpu_seconds", "pids_max"):
            value = getattr(resources, key)
            if value is not None:
                lines.append(f"{key} = {value}")
    else:
        lines.append("# sandbox.resources: unset (no per-sandbox ceilings)")
    return "\n".join(lines) + "\n"
