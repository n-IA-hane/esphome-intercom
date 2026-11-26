"""Bounded frame admission uses queue shutdown without per-frame tasks."""

import asyncio

import pytest

from custom_components.voip_stack.browser_paging import BrowserPagingMedia, BROWSER_PAGING_PCM

pytestmark = pytest.mark.ha


async def test_pcm_frame_order_and_padding_need_no_per_frame_tasks():
    media = BrowserPagingMedia(None)
    frame_size = BROWSER_PAGING_PCM.nominal_frame_bytes
    expected = [bytes([index]) * frame_size for index in range(30)]
    expected.append(b"\x42" * 2 + bytes(frame_size - 2))
    received = []
    async def consume():
        for _ in expected:
            received.append(await media.tx_queue.get())
            media.tx_queue.task_done()
    async def chunks():
        yield b"".join(expected[:-1]) + b"\x42" * 2
    consumer = asyncio.create_task(consume())
    loop = asyncio.get_running_loop()
    previous = loop.get_task_factory()
    created = []
    def count(loop, coroutine, **kwargs):
        created.append(coroutine)
        return asyncio.Task(coroutine, loop=loop, **kwargs)
    loop.set_task_factory(count)
    try:
        await media.play_pcm_stream(chunks())
        await consumer
    finally:
        loop.set_task_factory(previous)
        await media.stop()
    assert received == expected
    assert created == []


async def test_shutdown_wakes_blocked_frame_admission_and_rejects_later_frames():
    media = BrowserPagingMedia(None)
    frame = bytes(BROWSER_PAGING_PCM.nominal_frame_bytes)
    for _ in range(media.tx_queue.maxsize):
        await media._enqueue_frame(frame)
    producer = asyncio.create_task(media._enqueue_frame(frame))
    await asyncio.sleep(0)
    assert not producer.done()
    await media.stop()
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(producer, 1)
    with pytest.raises(ConnectionError):
        await media._enqueue_frame(frame)
    assert media.tx_queue.empty()
    await media.tx_queue.join()
