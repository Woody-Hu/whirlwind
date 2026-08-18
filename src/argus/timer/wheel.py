"""Kafka-style hierarchical timing wheel (faithful SystemTimer model).

Structure (mirrors Kafka's `SystemTimer`):
- level L has `wheel_size` buckets, bucket granularity `tick_ms * wheel_size**L`;
- a task lands on the lowest level whose window (`wheel_size` buckets) covers it,
  in the bucket whose expiration is the deadline rounded UP to the bucket boundary
  (tasks therefore never fire early; worst case one bucket-granularity late);
- buckets (not tasks) live in a delay heap; `advance(now)` pops expired buckets,
  firing due tasks and re-inserting not-yet-due ones into lower levels (cascade);
- cancellation is O(1): mark-and-skip at bucket processing time.

Driven either by a real-time ticker task (`start()`) or manually (`advance()`).
"""

from __future__ import annotations

import asyncio
import heapq
import time
from typing import Awaitable, Callable


class TimerHandle:
    __slots__ = ("deadline_ms", "callback", "cancelled")

    def __init__(self, deadline_ms: int, callback: Callable[[], Awaitable[None] | None]) -> None:
        self.deadline_ms = deadline_ms
        self.callback = callback
        self.cancelled = False

    def cancel(self) -> bool:
        if self.cancelled:
            return False
        self.cancelled = True
        return True


class _Bucket:
    __slots__ = ("expiration_ms", "tasks")

    def __init__(self) -> None:
        self.expiration_ms = -1
        self.tasks: list[TimerHandle] = []

    def add(self, handle: TimerHandle) -> None:
        self.tasks.append(handle)

    def take(self) -> list[TimerHandle]:
        tasks = self.tasks
        self.tasks = []
        self.expiration_ms = -1
        return tasks


class HierarchicalTimer:
    def __init__(
        self,
        tick_ms: int = 20,
        wheel_size: int = 512,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._tick_ms = tick_ms
        self._size = wheel_size
        self._clock = clock
        self._levels: list[list[_Bucket]] = [
            [_Bucket() for _ in range(wheel_size)] for _ in range(1)
        ]
        self._level_ticks = [tick_ms]  # ms per bucket at level i
        self._heap: list[tuple[int, int, _Bucket]] = []
        self._seq = 0
        self._driver: asyncio.Task | None = None
        self._stopped = False
        self._pending = 0

    @property
    def pending(self) -> int:
        return self._pending

    # ------------------------------------------------------------------ api

    def schedule(self, delay_s: float, callback: Callable[[], Awaitable[None] | None]) -> TimerHandle:
        return self.schedule_at(self._clock() + delay_s, callback)

    def schedule_at(self, deadline: float, callback: Callable[[], Awaitable[None] | None]) -> TimerHandle:
        if self._stopped:
            raise RuntimeError("timer stopped")
        handle = TimerHandle(int(deadline * 1000), callback)
        now_ms = int(self._clock() * 1000)
        if handle.deadline_ms <= now_ms:
            self._fire(handle)
            return handle
        self._insert(handle, now_ms)
        return handle

    def start(self) -> None:
        """Drive the wheel with a real-time ticker task."""
        if self._driver is not None or self._stopped:
            return

        async def drive() -> None:
            while not self._stopped:
                await asyncio.sleep(self._tick_ms / 1000)
                self.advance()

        self._driver = asyncio.get_running_loop().create_task(drive())

    async def stop(self) -> None:
        self._stopped = True
        if self._driver is not None:
            self._driver.cancel()
            try:
                await self._driver
            except asyncio.CancelledError:
                pass
            self._driver = None

    def advance(self, now_ms: int | None = None) -> list[TimerHandle]:
        """Fire every task due before `now` (default: real clock). Never fires early."""
        now = now_ms if now_ms is not None else int(self._clock() * 1000)
        fired: list[TimerHandle] = []
        while self._heap and self._heap[0][0] <= now:
            expiration, _, bucket = heapq.heappop(self._heap)
            if bucket.expiration_ms != expiration:
                continue  # stale heap entry (bucket was reset and re-queued)
            for handle in bucket.take():
                if handle.cancelled:
                    self._pending -= 1
                elif handle.deadline_ms <= now:
                    self._pending -= 1
                    fired.append(handle)
                    self._fire(handle)
                else:
                    # cascade: re-insert; it will land on a lower level now
                    self._insert(handle, now)
        return fired

    # ------------------------------------------------------------- internals

    def _insert(self, handle: TimerHandle, now_ms: int) -> None:
        for level, buckets in enumerate(self._levels):
            tick = self._level_ticks[level]
            expiration = -(-handle.deadline_ms // tick) * tick  # ceil to bucket boundary
            virtual_now = now_ms // tick * tick
            if expiration < virtual_now + tick * self._size:
                idx = (expiration // tick) % self._size
                bucket = buckets[idx]
                if bucket.expiration_ms != expiration:
                    # fresh fill cycle for this bucket (previous contents were taken)
                    bucket.take()
                    bucket.expiration_ms = expiration
                    self._seq += 1
                    heapq.heappush(self._heap, (expiration, self._seq, bucket))
                bucket.add(handle)
                self._pending += 1
                return
            # grow one level and retry
            if level + 1 == len(self._levels):
                self._levels.append([_Bucket() for _ in range(self._size)])
                self._level_ticks.append(tick * self._size)
        raise AssertionError("unreachable")

    def _fire(self, handle: TimerHandle) -> None:
        result = handle.callback()
        if asyncio.iscoroutine(result):
            asyncio.get_running_loop().create_task(result)


class HeapTimerBaseline:
    """Naive per-task heapq scheduler; the honest benchmark comparison target."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._heap: list[tuple[int, int, TimerHandle]] = []
        self._clock = clock
        self._seq = 0

    def schedule(self, delay_s: float, callback: Callable[[], Awaitable[None] | None]) -> TimerHandle:
        handle = TimerHandle(int((self._clock() + delay_s) * 1000), callback)
        self._seq += 1
        heapq.heappush(self._heap, (handle.deadline_ms, self._seq, handle))
        return handle

    def advance(self, now_ms: int | None = None) -> list[TimerHandle]:
        now = now_ms if now_ms is not None else int(self._clock() * 1000)
        fired: list[TimerHandle] = []
        while self._heap and self._heap[0][0] <= now:
            _, _, handle = heapq.heappop(self._heap)
            if not handle.cancelled:
                fired.append(handle)
                self._fire(handle)
        return fired

    def _fire(self, handle: TimerHandle) -> None:
        result = handle.callback()
        if asyncio.iscoroutine(result):
            asyncio.get_running_loop().create_task(result)
