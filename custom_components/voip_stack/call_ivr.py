"""Interruptible prompts and keypad input on an existing call media channel."""

from __future__ import annotations

import asyncio
from contextlib import aclosing


class CallIVR:
    """One call's keypad, retaining the digit that interrupts a prompt."""

    def __init__(self, media, create_task):
        self.media = media
        self.create_task = create_task
        self.digits = asyncio.Queue(maxsize=64)
        self.playback = None
        self.closed = False

    def feed(self, digit: str) -> bool:
        if self.closed or len(digit) != 1 or digit not in "0123456789*#ABCD":
            return False
        try:
            self.digits.put_nowait(digit)
        except asyncio.QueueFull:
            return False
        if self.playback is not None and not self.playback.done():
            self.playback.cancel()
        self.media.discard_output()
        return True

    async def play(self, chunks, *, source_format=None) -> bool:
        """Return false on keypad interruption; cancellation still ends the task."""
        async with aclosing(chunks):
            if self.closed or not self.digits.empty():
                return False

            async def stream():
                if source_format is None:
                    await self.media.play_pcm_stream(chunks)
                else:
                    await self.media.play_pcm_stream(chunks, source_format=source_format)
                await self.media.tx_queue.join()

            task = self.create_task(stream(), name="ivr-prompt")
            self.playback = task
            try:
                await task
                return True
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling() or self.closed:
                    raise
                return False
            finally:
                self.playback = None
                self.media.discard_output()

    async def next_digit(self, timeout):
        return await asyncio.wait_for(self.digits.get(), timeout)

    async def collect(self, *, maximum=10, timeout):
        """Collect a numeric selection terminated by #; * cancels selection."""
        value = ""
        while True:
            digit = await self.next_digit(timeout)
            if digit == "*":
                return None
            if digit == "#":
                return value
            if digit not in "0123456789" or len(value) >= maximum:
                raise ValueError("Invalid keypad selection")
            value += digit

    async def close(self):
        self.closed = True
        task = self.playback
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.media.discard_output()
