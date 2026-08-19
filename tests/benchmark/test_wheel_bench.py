"""Timing wheel benchmarks vs a naive heapq baseline (real timing, no mocks)."""

from whirlwind.timer import HeapTimerBaseline, HierarchicalTimer


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_wheel_schedule_10k(benchmark):
    def run():
        clock = FakeClock()
        wheel = HierarchicalTimer(tick_ms=20, wheel_size=512, clock=clock)
        for i in range(10_000):
            wheel.schedule((i % 5000) / 1000 + 0.02, lambda: None)
        return wheel.pending

    assert benchmark(run) == 10_000


def test_heap_schedule_10k(benchmark):
    def run():
        timer = HeapTimerBaseline(clock=FakeClock())
        for i in range(10_000):
            timer.schedule((i % 5000) / 1000 + 0.02, lambda: None)

    benchmark(run)


def test_wheel_cancel_10k(benchmark):
    def run():
        clock = FakeClock()
        wheel = HierarchicalTimer(tick_ms=20, wheel_size=512, clock=clock)
        handles = [wheel.schedule(5.0, lambda: None) for _ in range(10_000)]
        for h in handles:
            h.cancel()
        clock.now = 10.0
        fired = wheel.advance()  # reap cancelled handles
        return len(fired), wheel.pending

    fired, pending = benchmark(run)
    assert fired == 0 and pending == 0


def test_heap_cancel_10k(benchmark):
    def run():
        timer = HeapTimerBaseline(clock=FakeClock())
        handles = [timer.schedule(5.0, lambda: None) for _ in range(10_000)]
        for h in handles:
            h.cancel()

    benchmark(run)


def test_wheel_advance_10k_due(benchmark):
    def run():
        clock = FakeClock()
        wheel = HierarchicalTimer(tick_ms=20, wheel_size=512, clock=clock)
        for i in range(10_000):
            wheel.schedule((i % 4000) / 1000 + 0.02, lambda: None)
        clock.now = 10.0
        return len(wheel.advance())

    assert benchmark(run) == 10_000
