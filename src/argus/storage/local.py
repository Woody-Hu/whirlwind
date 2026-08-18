"""Local-filesystem implementations of ObjectStore / EventLog (M1 all-in-one)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from argus.core import SessionEvent
from argus.core.events import Surface


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


class JSONLEventLog:
    """Append-only JSONL log, one file per session. Owns seq assignment.

    Writes are flushed on append; durability is the process lifetime in M1
    (provider contract allows swap to a durable log system per architecture 10.3).
    """

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._files: dict[str, tuple[Any, int]] = {}  # session_id -> (handle, last_seq)
        self._seq_locks: dict[str, asyncio.Lock] = {}

    def _path(self, session_id: str) -> Path:
        if "/" in session_id or session_id.startswith("."):
            raise ValueError(f"bad session id: {session_id!r}")
        return self._root / f"{session_id}.jsonl"

    def _open(self, session_id: str) -> tuple[Any, int]:
        cached = self._files.get(session_id)
        if cached:
            return cached
        path = self._path(session_id)
        last = 0
        if path.exists():
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        last = json.loads(line)["seq"]
        handle = path.open("a", encoding="utf-8")
        entry = (handle, last)
        self._files[session_id] = entry
        return entry

    async def append(
        self,
        session_id: str,
        type: str,
        data: dict[str, Any] | None = None,
        surface: Surface | None = None,
    ) -> SessionEvent:
        lock = self._seq_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            handle, last = self._open(session_id)
            event = SessionEvent(
                session_id=session_id,
                seq=last + 1,
                type=type,
                data=data or {},
                surface=surface or Surface(),
            )
            handle.write(event.model_dump_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            self._files[session_id] = (handle, last + 1)
            return event

    async def read(self, session_id: str, from_seq: int = 0, limit: int = 1000) -> list[SessionEvent]:
        path = self._path(session_id)
        if not path.exists():
            return []
        out: list[SessionEvent] = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                ev = SessionEvent.model_validate(json.loads(line))
                if ev.seq > from_seq:
                    out.append(ev)
                    if len(out) >= limit:
                        break
        return out

    async def last_seq(self, session_id: str) -> int:
        cached = self._files.get(session_id)
        if cached:
            return cached[1]
        path = self._path(session_id)
        if not path.exists():
            return 0
        last = 0
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    last = json.loads(line)["seq"]
        return last

    def close(self) -> None:
        for handle, _ in self._files.values():
            handle.close()
        self._files.clear()
