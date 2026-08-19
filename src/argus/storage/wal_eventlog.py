"""Durable event log: per-session JSONL WAL with group-commit fsync.

Durability contract (architecture 10.3):
  append() returns only once the record has been fsync'd to stable storage.
  A crash or power loss before that point may drop the record, but never
  corrupts the log and never reorders committed records. Reopen runs crash
  recovery: any torn trailing record (a write interrupted mid-line) is
  truncated away before the next append.

Group commit: appends that land inside the same commit batch share one
fsync, so the cost converges to a single disk sync per batch instead of one
per record (per-append fsync measured ~7x slower than the ADR ≥5k/s append
line in M1).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from argus.core import SessionEvent
from argus.core.events import Surface


class WALEventLog:
    """Append-only WAL event log. Owns per-session seq assignment (EventLog)."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._files: dict[str, tuple[Any, int]] = {}  # session_id -> (handle, last_seq)
        self._seq_locks: dict[str, asyncio.Lock] = {}
        self._write_lock = asyncio.Lock()
        self._pending: list[tuple[Any, str]] = []  # (handle, line) awaiting fsync
        self._waiters: list[asyncio.Future] = []
        self._wakeup = asyncio.Event()
        self._committer: asyncio.Task | None = None
        self._closed = False

    # ---------------------------------------------------------------- group commit

    def _ensure_committer(self) -> None:
        if self._committer is None or self._committer.done():
            self._committer = asyncio.create_task(
                self._committer_loop(), name="wal-eventlog-commit"
            )

    async def _committer_loop(self) -> None:
        while not self._closed:
            try:
                await self._wakeup.wait()
                self._wakeup.clear()
                # drain the event loop so concurrent appends join this batch
                await asyncio.sleep(0)
                await self._commit_batch()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # fsync failure: surface to callers, keep going
                await self._fail_batch(exc)
                if self._pending:
                    self._wakeup.set()
            else:
                if self._pending:
                    self._wakeup.set()

    async def _commit_batch(self) -> None:
        async with self._write_lock:
            pending, waiters = self._pending, self._waiters
            self._pending, self._waiters = [], []
        if not pending:
            return
        try:
            # one flush+fsync per file handle, covering every record in the batch
            for handle in {h for h, _ in pending}:
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException as exc:
            for fut in waiters:
                if not fut.done():
                    fut.set_exception(exc)
            raise
        for fut in waiters:
            if not fut.done():
                fut.set_result(None)

    async def _fail_batch(self, exc: BaseException) -> None:
        async with self._write_lock:
            waiters = self._waiters
            self._waiters = []
        for fut in waiters:
            if not fut.done():
                fut.set_exception(exc)

    def _enqueue(self, handle: Any, line: str) -> asyncio.Future:
        loop = asyncio.get_running_loop()
        waiter = loop.create_future()
        self._pending.append((handle, line))
        self._waiters.append(waiter)
        handle.write(line)  # into the OS buffer; fsynced by the committer
        self._wakeup.set()
        return waiter

    # ---------------------------------------------------------------- file layout

    def _path(self, session_id: str) -> Path:
        if "/" in session_id or session_id.startswith("."):
            raise ValueError(f"bad session id: {session_id!r}")
        return self._root / f"{session_id}.jsonl"

    @staticmethod
    def _recover(path: Path) -> int:
        """Scan a WAL file, truncate a torn trailing record, return last seq."""
        if not path.exists():
            return 0
        data = path.read_bytes()
        nl = data.rfind(b"\n")
        if nl < 0:
            path.write_bytes(b"")  # only a torn partial record: drop it
            return 0
        last = 0
        for line in data[: nl + 1].split(b"\n"):
            if line.strip():
                last = json.loads(line.decode("utf-8"))["seq"]
        if nl + 1 < len(data):  # a torn tail after the last complete newline
            with path.open("r+b") as fh:
                fh.truncate(nl + 1)
                os.fsync(fh.fileno())
        return last

    def _open(self, session_id: str) -> tuple[Any, int]:
        cached = self._files.get(session_id)
        if cached:
            return cached
        path = self._path(session_id)
        last = self._recover(path)
        handle = path.open("a", encoding="utf-8")
        entry = (handle, last)
        self._files[session_id] = entry
        return entry

    # ---------------------------------------------------------------- EventLog

    async def append(
        self,
        session_id: str,
        type: str,
        data: dict[str, Any] | None = None,
        surface: Surface | None = None,
    ) -> SessionEvent:
        self._ensure_committer()
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
            async with self._write_lock:
                waiter = self._enqueue(handle, event.model_dump_json() + "\n")
                self._files[session_id] = (handle, last + 1)
        await waiter  # durability: returns only after the group fsync
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
                try:
                    ev = SessionEvent.model_validate(json.loads(line))
                except (json.JSONDecodeError, ValueError):
                    continue  # torn trailing record from a crash mid-write
                if ev.seq > from_seq:
                    out.append(ev)
                    if len(out) >= limit:
                        break
        return out

    async def last_seq(self, session_id: str) -> int:
        cached = self._files.get(session_id)
        if cached:
            return cached[1]
        return self._recover(self._path(session_id))

    def close(self) -> None:
        """Stop the committer and release handles; idempotent."""
        self._closed = True
        self._wakeup.set()
        if self._committer is not None:
            self._committer.cancel()
        # best-effort final durability for anything still buffered
        for handle in {h for h, _ in self._pending}:
            try:
                handle.flush()
                os.fsync(handle.fileno())
            except OSError:
                pass
        for fut in self._waiters:
            if not fut.done():
                fut.set_result(None)
        self._pending.clear()
        self._waiters.clear()
        for handle, _ in self._files.values():
            try:
                handle.close()
            except OSError:
                pass
        self._files.clear()
