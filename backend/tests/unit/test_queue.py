import asyncio

import pytest

from backend.app.daemon.queue import EventQueue


@pytest.mark.asyncio
async def test_flush_respects_batch_size():
    batches = []
    queue = EventQueue(save_func=lambda events: batches.append(list(events)), batch_size=2)

    await queue.put({"id": 1})
    await queue.put({"id": 2})
    await queue.put({"id": 3})

    assert await queue._flush_remaining() is True
    assert [len(batch) for batch in batches] == [2]
    assert queue._queue.qsize() == 1

    assert await queue._flush_remaining() is True
    assert [len(batch) for batch in batches] == [2, 1]


@pytest.mark.asyncio
async def test_background_worker_flushes_on_interval():
    batches = []
    queue = EventQueue(
        save_func=lambda events: batches.append(list(events)),
        flush_interval=0.01,
        batch_size=10,
    )

    queue.start()
    try:
        await queue.put({"id": "timed"})
        await asyncio.wait_for(_wait_for_batches(batches), timeout=0.5)
    finally:
        await queue.stop()

    assert batches == [[{"id": "timed"}]]


@pytest.mark.asyncio
async def test_failed_flush_is_retried_without_dropping_events():
    calls = 0
    batches = []

    def flaky_save(events):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("database is locked")
        batches.append(list(events))

    queue = EventQueue(save_func=flaky_save, batch_size=10)
    event = {"id": "kept"}
    await queue.put(event)

    assert await queue._flush_remaining() is False
    assert queue._retry_buffer == [event]

    assert await queue._flush_remaining() is True
    assert batches == [[event]]


async def _wait_for_batches(batches):
    while not batches:
        await asyncio.sleep(0.005)
