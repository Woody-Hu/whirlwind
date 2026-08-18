"""Local-filesystem implementations of storage providers (M1 all-in-one)."""

from __future__ import annotations

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
