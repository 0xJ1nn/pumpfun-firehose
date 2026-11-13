from __future__ import annotations

import asyncio
import inspect

from pumpfire.health import EventQueue


def test_drops_oldest_under_pressure_and_counts_each_drop() -> None:
    queue = EventQueue(maxsize=4)

    for index in range(10):
        queue.put_nowait(index)
        census = queue.census()
        assert census.depth <= 4

    assert queue.dropped == 6
    assert queue.census().enqueued == 10
    assert queue.census().maxsize == 4
    assert [queue.get_nowait() for _ in range(4)] == [6, 7, 8, 9]
    assert queue.census().depth == 0


def test_census_is_a_synchronous_read() -> None:
    queue = EventQueue(maxsize=8)
    assert inspect.iscoroutinefunction(EventQueue.census) is False

    for index in range(32):
        queue.put_nowait(index)

    census = queue.census()
    assert census.as_dict() == {"depth": 8, "maxsize": 8, "enqueued": 32, "dropped": 24}


async def test_async_put_never_waits_on_a_full_queue() -> None:
    queue = EventQueue(maxsize=2)

    await queue.put(1)
    await queue.put(2)
    await asyncio.wait_for(queue.put(3), timeout=1.0)

    assert queue.dropped == 1
    assert [queue.get_nowait() for _ in range(2)] == [2, 3]


async def test_flood_does_not_stall_the_reader() -> None:
    queue = EventQueue(maxsize=8)
    received: list[int] = []

    async def reader() -> None:
        while True:
            received.append(await queue.get())

    task = asyncio.create_task(reader())
    for index in range(500):
        queue.put_nowait(index)
        census = queue.census()
        assert census.depth <= 8
        if index % 25 == 0:
            await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert received
    assert received == sorted(received)
    assert queue.dropped > 0
    assert queue.census().depth <= 8
