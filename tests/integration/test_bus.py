"""In-process EventBus tests: fan-out, wildcards, bounded-buffer catch-up semantics."""

import asyncio

from whirlwind.bus import InProcessEventBus


async def test_exact_topic_fanout():
    bus = InProcessEventBus()
    sub = await bus.subscribe("sessions.ses_1.stream")
    bus.publish("sessions.ses_1.stream", {"seq": 1})
    bus.publish("sessions.ses_2.stream", {"seq": 99})  # different session: not delivered
    item = await sub.next(timeout=1)
    assert item == {"seq": 1}
    assert await sub.next(timeout=0.05) is None
    await bus.close()


async def test_wildcard_subscription():
    bus = InProcessEventBus()
    sub = await bus.subscribe("sessions.*.stream")
    bus.publish("sessions.ses_a.stream", {"seq": 1})
    bus.publish("sessions.ses_b.stream", {"seq": 2})
    got = [await sub.next(timeout=1), await sub.next(timeout=1)]
    assert {g["seq"] for g in got} == {1, 2}
    await bus.close()


async def test_multiple_subscribers_each_get_copy():
    bus = InProcessEventBus()
    a = await bus.subscribe("sessions.ses_1.stream")
    b = await bus.subscribe("sessions.ses_1.stream")
    bus.publish("sessions.ses_1.stream", {"seq": 7})
    assert (await a.next(timeout=1))["seq"] == 7
    assert (await b.next(timeout=1))["seq"] == 7
    await bus.close()


async def test_drop_oldest_under_backpressure():
    bus = InProcessEventBus(buffer=4)
    sub = await bus.subscribe("t")
    for i in range(10):
        bus.publish("t", {"seq": i})
    got = []
    for _ in range(4):
        item = await sub.next(timeout=1)
        got.append(item["seq"])
    # oldest dropped; consumer must catch up from EventLog by seq (contract)
    assert got == [6, 7, 8, 9]
    await bus.close()


async def test_unsubscribe_stops_delivery():
    bus = InProcessEventBus()
    sub = await bus.subscribe("t")
    await bus.unsubscribe(sub)
    bus.publish("t", {"seq": 1})
    assert await sub.next(timeout=0.05) is None
    await bus.close()


async def test_close_wakes_waiting_subscribers():
    bus = InProcessEventBus()
    sub = await bus.subscribe("t")

    async def wait_and_check():
        item = await sub.next(timeout=5)
        return item

    task = asyncio.create_task(wait_and_check())
    await asyncio.sleep(0.05)
    await bus.close()
    assert await task is None
