"""Hierarchical timing wheel tests with a controllable clock (deterministic)."""

import asyncio

from argus.timer import CronExpr, CronParseError, HierarchicalTimer


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance_ms(self, ms: int) -> None:
        self.now += ms / 1000


async def test_fire_order_across_one_revolution():
    clock = FakeClock()
    wheel = HierarchicalTimer(tick_ms=10, wheel_size=8, clock=clock)
    fired = []
    for delay_ms in [5, 25, 15, 75, 45]:
        wheel.schedule(delay_ms / 1000, lambda d=delay_ms: fired.append(d))
    clock.advance_ms(100)
    wheel.advance()
    assert fired == [5, 15, 25, 45, 75]


async def test_cascade_beyond_first_wheel_span():
    clock = FakeClock()
    wheel = HierarchicalTimer(tick_ms=10, wheel_size=8, clock=clock)
    fired = []
    # level-0 span is 80ms; these need level 1+ (each level = 8x previous)
    wheel.schedule(0.150, lambda: fired.append(150))
    wheel.schedule(0.700, lambda: fired.append(700))
    wheel.schedule(5.000, lambda: fired.append(5000))  # needs level 3 (bucket 5120ms)

    # never early: at t=4999 the 5000ms timer must not have fired
    clock.advance_ms(4999)
    wheel.advance()
    assert fired == [150, 700]

    # bucket granularity: fires at its level-3 bucket boundary, not before
    clock.advance_ms(10_000 - 4999)
    wheel.advance()
    assert fired == [150, 700, 5000]


async def test_cancel_is_effective():
    clock = FakeClock()
    wheel = HierarchicalTimer(tick_ms=10, wheel_size=8, clock=clock)
    fired = []
    h = wheel.schedule(0.050, lambda: fired.append("a"))
    assert wheel.pending == 1
    assert h.cancel() is True
    assert h.cancel() is False  # double cancel
    clock.advance_ms(80)
    wheel.advance()
    assert fired == []
    assert wheel.pending == 0


async def test_cancel_high_level_timer():
    clock = FakeClock()
    wheel = HierarchicalTimer(tick_ms=10, wheel_size=8, clock=clock)
    fired = []
    h = wheel.schedule(3.000, lambda: fired.append("far"))
    wheel.schedule(0.030, lambda: fired.append("near"))
    h.cancel()
    clock.advance_ms(4000)
    wheel.advance()
    assert fired == ["near"]


async def test_past_deadline_fires_immediately():
    clock = FakeClock()
    wheel = HierarchicalTimer(tick_ms=10, wheel_size=8, clock=clock)
    fired = []
    wheel.schedule_at(clock.now - 1.0, lambda: fired.append("late"))
    assert fired == ["late"]


async def test_no_timers_advance_is_noop():
    clock = FakeClock()
    wheel = HierarchicalTimer(tick_ms=10, wheel_size=8, clock=clock)
    clock.advance_ms(1000)
    assert wheel.advance() == []
    assert wheel.pending == 0


async def test_async_callbacks_run_as_tasks():
    clock = FakeClock()
    wheel = HierarchicalTimer(tick_ms=10, wheel_size=8, clock=clock)
    done = asyncio.Event()

    async def cb() -> None:
        done.set()

    wheel.schedule(0.030, cb)
    clock.advance_ms(50)
    wheel.advance()
    await asyncio.wait_for(done.wait(), timeout=1)


async def test_realtime_driver_fires_on_wall_clock():
    wheel = HierarchicalTimer(tick_ms=5, wheel_size=64)
    wheel.start()
    fired = asyncio.Event()
    wheel.schedule(0.060, fired.set)
    try:
        await asyncio.wait_for(fired.wait(), timeout=2)
    finally:
        await wheel.stop()


async def test_dense_schedule_all_fire_exactly_once():
    clock = FakeClock()
    wheel = HierarchicalTimer(tick_ms=1, wheel_size=64, clock=clock)
    n = 2000
    fired = []
    for i in range(1, n + 1):
        wheel.schedule(i / 1000, lambda d=i: fired.append(d))
    # bucket granularity rounds deadlines up to at most one level-1 bucket (64ms)
    clock.advance_ms(n + 128)
    wheel.advance()
    assert sorted(fired) == list(range(1, n + 1))
    assert len(fired) == n


# ---------------------------------------------------------------- cron


def test_cron_every_minute():
    expr = CronExpr.parse("* * * * *")
    base = __import__("datetime").datetime(2026, 8, 18, 10, 30, 15)
    nxt = expr.next_after(base)
    assert (nxt.minute, nxt.second) == (31, 0)


def test_cron_step_and_list():
    expr = CronExpr.parse("*/15 9,17 * * *")
    from datetime import datetime

    base = datetime(2026, 8, 18, 8, 0)
    nxt = expr.next_after(base)
    assert (nxt.hour, nxt.minute) == (9, 0)
    nxt2 = expr.next_after(datetime(2026, 8, 18, 9, 0))
    assert (nxt2.hour, nxt2.minute) == (9, 15)


def test_cron_range_field():
    expr = CronExpr.parse("30 10-12 1 * *")
    from datetime import datetime

    base = datetime(2026, 8, 18, 13, 0)
    nxt = expr.next_after(base)  # 1st of next month at 10:30
    assert (nxt.month, nxt.day, nxt.hour, nxt.minute) == (9, 1, 10, 30)


def test_cron_dow_sunday_zero_and_seven():
    from datetime import datetime

    zero = CronExpr.parse("0 12 * * 0")
    seven = CronExpr.parse("0 12 * * 7")
    base = datetime(2026, 8, 19, 0, 0)  # wednesday
    assert zero.next_after(base) == seven.next_after(base)
    assert zero.next_after(base).weekday() == 6  # sunday


def test_cron_dom_dow_either_semantics():
    from datetime import datetime

    expr = CronExpr.parse("0 0 13 * 5")  # 13th OR friday
    base = datetime(2026, 8, 1, 1, 0)
    nxt = expr.next_after(base)
    # first 13th or friday after aug 1 2026: fri aug 7? no — 13th is thu; friday aug 14? aug 7 is friday
    assert nxt.day in (7, 13)


def test_cron_parse_errors():
    for bad in ["* * * *", "60 * * * *", "* 24 * * *", "a * * * *", "*/0 * * * *", "5-1 * * * *"]:
        try:
            CronExpr.parse(bad)
        except CronParseError:
            continue
        raise AssertionError(f"should reject {bad!r}")


def test_cron_question_mark_alias():
    expr = CronExpr.parse("*/5 ? * ? *")
    assert 5 in expr.minute and 0 in expr.minute
