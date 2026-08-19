"""In-process EventBus: topic fan-out over asyncio queues (architecture 10.3)."""

from __future__ import annotations

import asyncio
import fnmatch
from typing import Any, AsyncIterator


class Subscription:
    """Async iterator over a topic. Bounded buffer; slow consumers replay from EventLog by seq."""

    def __init__(self, topic: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self.topic = topic
        self.queue = queue
        self._closed = False

    async def next(self, timeout: float | None = None) -> dict[str, Any] | None:
        if timeout is None:
            item = await self.queue.get()
        else:
            try:
                item = await asyncio.wait_for(self.queue.get(), timeout)
            except asyncio.TimeoutError:
                return None
        if item is _CLOSED:
            self._closed = True
            return None
        return item

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        return self

    async def __anext__(self) -> dict[str, Any]:
        item = await self.next()
        if item is None:
            raise StopAsyncIteration
        return item

    @property
    def closed(self) -> bool:
        return self._closed


_CLOSED = object()


class InProcessEventBus:
    """Exact-topic and wildcard (`sessions.*.stream`) pub/sub, at-least-once to each subscriber."""

    def __init__(self, buffer: int = 1024) -> None:
        self._buffer = buffer
        self._subs: dict[str, list[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, topic: str) -> Subscription:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._buffer)
        async with self._lock:
            self._subs.setdefault(topic, []).append(q)
        return Subscription(topic, q)

    async def unsubscribe(self, sub: Subscription) -> None:
        async with self._lock:
            queues = self._subs.get(sub.topic, [])
            self._subs[sub.topic] = [q for q in queues if q is not sub.queue]

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        for pattern, queues in list(self._subs.items()):
            if fnmatch.fnmatchcase(topic, pattern) or fnmatch.fnmatchcase(pattern, topic):
                for q in queues:
                    try:
                        q.put_nowait(payload)
                    except asyncio.QueueFull:
                        # drop-oldest: consumer catches up via EventLog seq
                        try:
                            q.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                        q.put_nowait(payload)

    async def close(self) -> None:
        for queues in self._subs.values():
            for q in queues:
                q.put_nowait(_CLOSED)
        self._subs.clear()
