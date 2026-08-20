"""Local-filesystem implementations of storage providers (M1 all-in-one)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class LocalObjectStore:
    """Directory-backed blob store. Keys are relative paths; no nesting tricks."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        dest = (self._root / key).resolve()
        if not str(dest).startswith(str(self._root)):
            raise ValueError(f"object key escapes root: {key!r}")
        return dest

    async def put(self, key: str, src: Path) -> str:
        dest = self._resolve(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = src.read_bytes()
        dest.write_bytes(data)
        return f"local://{dest}"

    async def fetch(self, key: str, dest: Path) -> Path:
        src = self._resolve(key)
        if not src.exists():
            raise FileNotFoundError(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(src.read_bytes())
        return dest

    async def delete(self, key: str) -> None:
        p = self._resolve(key)
        if p.exists():
            p.unlink()

    async def stat(self, key: str) -> dict[str, Any] | None:
        p = self._resolve(key)
        if not p.exists():
            return None
        return {"size": p.stat().st_size, "path": str(p)}


class LocalFileSecretStore:
    """File-per-version envelope store (ADR-0010 D6, default backend).

    One JSON file of `{name: envelope}` per version under `<root>/secrets/`,
    written 0600 — the file carries ciphertext only. `put_version_env` replaces
    the whole set (immutable version unit). The default all-in-one runtime
    points `root` at the data_dir, so secret artifacts live beside — but never
    inside — the metadata the gateway dumps.
    """

    def __init__(self, root: Path) -> None:
        self._root = (root / "secrets").resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, version_id: str) -> Path:
        # version ids are platform-generated (ver_*) — refuse path-shaped input
        if "/" in version_id or version_id in {"", ".", ".."}:
            raise ValueError(f"invalid version id: {version_id!r}")
        return self._root / f"{version_id}.json"

    async def put_version_env(self, version_id: str, envelopes: dict[str, str]) -> None:
        target = self._path(version_id)
        target.write_text(json.dumps(envelopes, indent=2))
        target.chmod(0o600)

    async def get_version_env(self, version_id: str) -> dict[str, str]:
        target = self._path(version_id)
        if not target.is_file():
            return {}
        return {name: str(env) for name, env in json.loads(target.read_text()).items()}

    async def delete_version_env(self, version_id: str) -> None:
        target = self._path(version_id)
        if target.exists():
            target.unlink()
