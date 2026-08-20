"""Unit tests for agent-defined env secrets (ADR-0010).

Real cryptography against real files — no mocks. Covers the box round-trip,
envelope format, wrong-key/tamper failure modes, key loading precedence, and
the D4 name validation rules.
"""

from __future__ import annotations

import base64
import os
import stat
from pathlib import Path

import pytest

from whirlwind.secrets import (
    KEY_SIZE,
    KEYFILE_NAME,
    SecretBox,
    SecretBoxError,
    SecretNameError,
    validate_env_names,
)


def _b64key(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


# ------------------------------------------------------------------- box

def test_roundtrip_and_envelope_format():
    box = SecretBox.from_master_key(b"k" * KEY_SIZE)
    envelope = box.seal("super-secret-token")
    prefix, key_id, payload = envelope.split(":")
    assert prefix == "v1"
    assert key_id == box.key_id
    assert box.open(envelope) == "super-secret-token"


def test_nonce_is_random_per_record():
    box = SecretBox.from_master_key(b"k" * KEY_SIZE)
    a, b = box.seal("same"), box.seal("same")
    assert a != b  # per-record random nonce, never deterministic
    assert box.open(a) == box.open(b) == "same"


def test_seal_env_and_open_env_roundtrip():
    box = SecretBox.from_master_key(b"k" * KEY_SIZE)
    envelopes = box.seal_env({"GITHUB_TOKEN": "gh_1", "OTHER_KEY": "ok"})
    assert set(envelopes) == {"GITHUB_TOKEN", "OTHER_KEY"}
    assert box.open_env(envelopes) == {"GITHUB_TOKEN": "gh_1", "OTHER_KEY": "ok"}


def test_wrong_key_id_fails_fast():
    box_a = SecretBox.from_master_key(b"a" * KEY_SIZE)
    box_b = SecretBox.from_master_key(b"b" * KEY_SIZE)
    envelope = box_a.seal("value")
    with pytest.raises(SecretBoxError, match="does not match"):
        box_b.open(envelope)


def test_tamper_detection():
    box = SecretBox.from_master_key(b"k" * KEY_SIZE)
    envelope = box.seal("value")
    prefix, key_id, payload = envelope.split(":")
    blob = bytearray(base64.b64decode(payload))
    blob[-1] ^= 0x01  # flip one bit of the tag
    tampered = f"{prefix}:{key_id}:{base64.b64encode(bytes(blob)).decode()}"
    with pytest.raises(SecretBoxError, match="decryption failed"):
        box.open(tampered)


def test_malformed_envelopes_rejected():
    box = SecretBox.from_master_key(b"k" * KEY_SIZE)
    for bad in ["", "v1", "v2:abc:def", "v1:abc:not-base64!!", "v1:abc:"]:
        with pytest.raises(SecretBoxError):
            box.open(bad)


def test_master_key_size_enforced():
    with pytest.raises(SecretBoxError, match="32 bytes"):
        SecretBox.from_master_key(b"short")


# ------------------------------------------------------------ key loading

def test_key_from_env_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("WHIRLWIND_SECRET_KEY", _b64key(b"e" * KEY_SIZE))
    (tmp_path / KEYFILE_NAME).write_text(_b64key(b"f" * KEY_SIZE))
    box = SecretBox.from_env_or_file("WHIRLWIND_SECRET_KEY", tmp_path)
    assert box.key_id == SecretBox.from_master_key(b"e" * KEY_SIZE).key_id


def test_key_from_existing_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("WHIRLWIND_SECRET_KEY", raising=False)
    (tmp_path / KEYFILE_NAME).write_text(_b64key(b"f" * KEY_SIZE))
    box = SecretBox.from_env_or_file("WHIRLWIND_SECRET_KEY", tmp_path)
    assert box.key_id == SecretBox.from_master_key(b"f" * KEY_SIZE).key_id


def test_key_generated_when_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("WHIRLWIND_SECRET_KEY", raising=False)
    box = SecretBox.from_env_or_file("WHIRLWIND_SECRET_KEY", tmp_path)
    key_path = tmp_path / KEYFILE_NAME
    assert key_path.is_file()
    # 0600 — the dev fallback file must not be world/group readable
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    # second boot from the same data_dir reuses the persisted key
    again = SecretBox.from_env_or_file("WHIRLWIND_SECRET_KEY", tmp_path)
    assert again.open(box.seal("x")) == "x"


def test_bad_key_material_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("WHIRLWIND_SECRET_KEY", "!!!not-base64!!!")
    with pytest.raises(SecretBoxError, match="base64"):
        SecretBox.from_env_or_file("WHIRLWIND_SECRET_KEY", tmp_path)
    monkeypatch.setenv("WHIRLWIND_SECRET_KEY", _b64key(b"too-short"))
    with pytest.raises(SecretBoxError, match="32 bytes"):
        SecretBox.from_env_or_file("WHIRLWIND_SECRET_KEY", tmp_path)


# -------------------------------------------------------- name validation

def test_valid_names_accepted():
    validate_env_names(["GITHUB_TOKEN", "MY_API_KEY", "_under", "a1"])
    validate_env_names([])  # empty declaration is legal


@pytest.mark.parametrize(
    "bad",
    [
        "",  # empty
        "1STARTS_WITH_DIGIT",  # not POSIX
        "HAS-DASH",
        "HAS SPACE",
        "WHIRLWIND_SANDBOX_ID",  # platform namespace
        "WHIRLWIND_ANYTHING",
        "DEEPSEEK_API_KEY",  # SecretRelay placeholder
    ],
)
def test_invalid_or_reserved_names_rejected(bad: str):
    with pytest.raises(SecretNameError):
        validate_env_names([bad])


def test_duplicate_names_rejected():
    with pytest.raises(SecretNameError, match="duplicate"):
        validate_env_names(["A", "B", "A"])


def test_errors_are_whirlwind_errors():
    """The error hierarchy rides WhirlwindError so the gateway maps them to HTTP."""
    from whirlwind.core.errors import WhirlwindError

    assert issubclass(SecretBoxError, WhirlwindError)
    assert issubclass(SecretNameError, WhirlwindError)
    assert SecretBoxError.code == "whirlwind/secrets/box"
    assert SecretNameError.code == "whirlwind/secrets/name"
