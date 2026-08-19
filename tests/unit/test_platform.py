"""Platform facts + behaviour-plugin tests (ADR-0007).

Pure logic, runs on every host. The env override here is the SUT's own
feature (identity simulation), exercised with monkeypatch — not a mock of
anything. Honesty boundary asserts probes stay real under override.
"""

import pytest

from whirlwind.core.platform import (
    ENV_PLATFORM,
    SYSTEM_LINUX,
    SYSTEM_MACOS,
    SYSTEM_WINDOWS,
    PlatformImplError,
    current_facts,
    detect_facts,
    platform_impl,
    registered_platforms,
    resolve_impl,
)

# ----------------------------------------------------------------- test plugin

# Registered at import of this module (imports are the wiring, ADR-0007 D3).
# Test-only feature names must not collide with production ones.


@platform_impl("test.platform.echo", "*")
def _echo_default(value: str) -> tuple[str, str]:
    return ("default", value)


@platform_impl("test.platform.echo", SYSTEM_MACOS)
def _echo_macos(value: str) -> tuple[str, str]:
    return (SYSTEM_MACOS, value)


@platform_impl("test.platform.echo", SYSTEM_WINDOWS)
def _echo_windows(value: str) -> tuple[str, str]:
    return (SYSTEM_WINDOWS, value)


# ------------------------------------------------------------------- facts


def test_detect_facts_is_real_and_normalized() -> None:
    facts = detect_facts()
    assert facts.system in (SYSTEM_MACOS, SYSTEM_LINUX, SYSTEM_WINDOWS, "unknown")
    assert facts.machine in ("arm64", "x86_64") or facts.machine  # free-form allowed, never empty
    assert isinstance(facts.vsock, bool)
    assert isinstance(facts.restricted, bool)


def test_auto_and_unset_are_no_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_PLATFORM, raising=False)
    assert current_facts() == detect_facts()
    assert not current_facts().overridden
    monkeypatch.setenv(ENV_PLATFORM, "auto")
    assert current_facts() == detect_facts()
    assert not current_facts().overridden


def test_override_identity_derives_family_semantics(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_PLATFORM, SYSTEM_LINUX)
    facts = current_facts()
    assert facts.system == SYSTEM_LINUX
    assert facts.overridden
    assert facts.rlimit_as_supported
    assert facts.uds_path_max == 108

    monkeypatch.setenv(ENV_PLATFORM, SYSTEM_MACOS)
    facts = current_facts()
    assert facts.system == SYSTEM_MACOS
    assert not facts.rlimit_as_supported
    assert facts.uds_path_max == 104


def test_override_machine_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_PLATFORM, "linux/x86_64")
    assert current_facts().machine == "x86_64"
    monkeypatch.setenv(ENV_PLATFORM, f"{SYSTEM_MACOS}/arm64")
    assert current_facts().machine == "arm64"
    # aarch64/amd64 aliases normalize
    monkeypatch.setenv(ENV_PLATFORM, "linux/aarch64")
    assert current_facts().machine == "arm64"


def test_override_invalid_system_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    for bad in ("darwin", "osx", "linuxish", "Linux"):
        monkeypatch.setenv(ENV_PLATFORM, bad)
        with pytest.raises(ValueError, match=ENV_PLATFORM):
            current_facts()


def test_honesty_boundary_probes_stay_real(monkeypatch: pytest.MonkeyPatch) -> None:
    """Override changes identity only — vsock/restricted mirror the real host."""
    real = detect_facts()
    for raw in (SYSTEM_LINUX, SYSTEM_WINDOWS, f"{SYSTEM_MACOS}/x86_64"):
        monkeypatch.setenv(ENV_PLATFORM, raw)
        facts = current_facts()
        assert facts.vsock == real.vsock
        assert facts.restricted == real.restricted


# ------------------------------------------------------------------ plugins


def test_plugin_dispatch_prefers_active_system(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_PLATFORM, SYSTEM_MACOS)
    assert resolve_impl("test.platform.echo")("x") == (SYSTEM_MACOS, "x")
    monkeypatch.setenv(ENV_PLATFORM, SYSTEM_WINDOWS)
    assert resolve_impl("test.platform.echo")("x") == (SYSTEM_WINDOWS, "x")


def test_plugin_dispatch_falls_back_to_star(monkeypatch: pytest.MonkeyPatch) -> None:
    # linux has no specific impl -> the "*" registration serves it
    monkeypatch.setenv(ENV_PLATFORM, SYSTEM_LINUX)
    assert resolve_impl("test.platform.echo")("x") == ("default", "x")


def test_plugin_dispatch_without_override_matches_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_PLATFORM, raising=False)
    impl = resolve_impl("test.platform.echo")
    assert impl("x") in (("default", "x"), (SYSTEM_MACOS, "x"))


def test_plugin_unregistered_feature_raises() -> None:
    with pytest.raises(PlatformImplError) as excinfo:
        resolve_impl("test.platform.never-registered")
    assert excinfo.value.code == "whirlwind/platform/impl-not-found"


def test_registered_platforms_introspection() -> None:
    assert registered_platforms("test.platform.echo") == ["*", SYSTEM_MACOS, SYSTEM_WINDOWS]
    assert registered_platforms("test.platform.nothing") == []


def test_production_rlimits_plugin_registered() -> None:
    """drivers.process.rlimits ships a macos specialization + POSIX default (ADR-0007 D4)."""
    from whirlwind.drivers import process  # noqa: F401  (import registers the plugins)

    assert SYSTEM_MACOS in registered_platforms("drivers.process.rlimits")
    assert "*" in registered_platforms("drivers.process.rlimits")
