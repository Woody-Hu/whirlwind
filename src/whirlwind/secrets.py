"""Agent-defined environment secrets: sealing, key loading, name validation (ADR-0010).

Host-side ONLY. This module imports pynacl (~60ms measured on Linux) and is
therefore never imported by the sandbox cold-start chain (agent.server /
echo_server) — the ADR-0008-era cold-start budget is untouched. Consumers are
the gateway (seal on version write) and the hostlet (open on provision).

Envelope format (versioned, the rotation seam): `v1:<key_id>:<b64(nonce||ct||tag)>`
where key_id is a non-secret digest prefix of the master key: wrong-key decrypts
fail fast with a clear error instead of a bare MAC failure.

The trust model (D3/D5): user secrets are a *different trust zone* from the
platform LLM credential. The DeepSeek key stays host-side behind the SecretRelay
(sandbox sees the placeholder); user env values are *meant* to reach the harness
process env — the platform protects at-rest artifacts (ciphertext on disk) and
API surfaces (values never returned), not the sandbox itself.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
from pathlib import Path
from typing import Iterable

from nacl.secret import SecretBox as _NaclSecretBox
from nacl.utils import random as _random_bytes

from whirlwind.core.errors import WhirlwindError

KEY_SIZE = _NaclSecretBox.KEY_SIZE  # 32 bytes
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
RESERVED_PREFIX = "WHIRLWIND_"  # platform injection namespace (D4)
RESERVED_NAMES = frozenset({"DEEPSEEK_API_KEY"})  # SecretRelay placeholder (D4)
KEYFILE_NAME = "secret.key"
_ENV_PREFIX = "v1"


class SecretBoxError(WhirlwindError):
    """Seal/open failures: malformed envelope, wrong key, tamper detection."""

    code = "whirlwind/secrets/box"


class SecretNameError(WhirlwindError):
    """Name validation failure (reserved namespace / invalid / duplicate)."""

    code = "whirlwind/secrets/name"


def validate_env_names(names: Iterable[str]) -> None:
    """D4: reject anything that could clobber the platform env contract.

    - `WHIRLWIND_*` is the platform injection namespace
      (WHIRLWIND_SANDBOX_ID, WHIRLWIND_MANIFEST, ...);
    - `DEEPSEEK_API_KEY` is the SecretRelay placeholder — a real user key there
      would smuggle a live platform credential into the sandbox;
    - empty/non-POSIX/duplicate names are rejected outright.
    """
    seen: set[str] = set()
    for name in names:
        if not name or not _ENV_NAME_RE.match(name):
            raise SecretNameError(f"invalid environment variable name: {name!r}")
        if name.startswith(RESERVED_PREFIX):
            raise SecretNameError(
                f"{name!r} is reserved for platform injection ({RESERVED_PREFIX}* namespace)"
            )
        if name in RESERVED_NAMES:
            raise SecretNameError(f"{name!r} is reserved for the platform LLM relay boundary")
        if name in seen:
            raise SecretNameError(f"duplicate secret name: {name!r}")
        seen.add(name)


class SecretBox:
    """Authenticated encryption for secret values (XSalsa20-Poly1305 via libsodium).

    One box per master key; `key_id` (sha256 prefix) travels in every envelope
    so a wrong-key decrypt fails fast and rotation tooling can find its seam.
    """

    def __init__(self, key: bytes) -> None:
        if len(key) != KEY_SIZE:
            raise SecretBoxError(f"master key must be {KEY_SIZE} bytes, got {len(key)}")
        self._box = _NaclSecretBox(key)
        self.key_id = hashlib.sha256(key).digest()[:8].hex()

    @classmethod
    def from_master_key(cls, key: bytes) -> SecretBox:
        return cls(key)

    # ------------------------------------------------------------------ seal

    def seal(self, plaintext: str) -> str:
        """Value -> envelope `v1:<key_id>:<b64(nonce||ct||tag)>`. Random 192-bit nonce per record."""
        blob = bytes(self._box.encrypt(plaintext.encode("utf-8")))
        payload = base64.b64encode(blob).decode("ascii")
        return f"{_ENV_PREFIX}:{self.key_id}:{payload}"

    def seal_env(self, values: dict[str, str]) -> dict[str, str]:
        """Name -> value map -> name -> envelope map (whole set in one call)."""
        return {name: self.seal(value) for name, value in values.items()}

    # ------------------------------------------------------------------ open

    def open(self, envelope: str) -> str:
        """Envelope -> value. Wrong key_id / tamper / malformed -> SecretBoxError."""
        parts = envelope.split(":")
        if len(parts) != 3 or parts[0] != _ENV_PREFIX:
            raise SecretBoxError(f"malformed secret envelope (want 'v1:<key_id>:<b64>')")
        _, key_id, payload = parts
        if key_id != self.key_id:
            raise SecretBoxError(
                f"envelope key_id {key_id!r} does not match this box {self.key_id!r}"
                " (master key rotated or wrong key?)"
            )
        try:
            blob = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise SecretBoxError("malformed secret envelope payload (bad base64)") from exc
        try:
            plaintext = self._box.decrypt(blob)
        except Exception as exc:  # nacl raises CryptoError on tamper
            raise SecretBoxError("secret decryption failed (tampered or wrong key)") from exc
        try:
            return plaintext.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SecretBoxError("secret plaintext is not valid UTF-8") from exc

    def open_env(self, envelopes: dict[str, str]) -> dict[str, str]:
        return {name: self.open(envelope) for name, envelope in envelopes.items()}

    # ------------------------------------------------------------------- key

    @classmethod
    def from_env_or_file(cls, env_name: str, data_dir: Path) -> SecretBox:
        """Master key resolution (D3): env var wins; else data_dir/secret.key; else generate.

        Honest caveat: the fallback key file sits on the same disk as the
        ciphertexts — it protects metadata artifacts against accidental exposure
        (backup leaks, dumps, repo commits), NOT against host-filesystem access.
        Production deployments provide the env var.
        """
        raw = os.environ.get(env_name, "").strip()
        if raw:
            return cls._decode_key(raw, source=f"env {env_name!r}")
        key_path = data_dir / KEYFILE_NAME
        if key_path.is_file():
            return cls._decode_key(key_path.read_text().strip(), source=str(key_path))
        key = _random_bytes(KEY_SIZE)
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_text(base64.b64encode(key).decode("ascii") + "\n")
        key_path.chmod(0o600)
        return cls.from_master_key(key)

    @classmethod
    def _decode_key(cls, raw: str, *, source: str) -> SecretBox:
        try:
            key = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise SecretBoxError(f"master key from {source} is not valid base64") from exc
        if len(key) != KEY_SIZE:
            raise SecretBoxError(
                f"master key from {source} must be {KEY_SIZE} bytes "
                f"(base64 of 32 bytes), got {len(key)}"
            )
        return cls.from_master_key(key)
