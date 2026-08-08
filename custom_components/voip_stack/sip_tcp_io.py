"""Governed SIP/TCP writes with explicit backpressure."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from .core import sip

_LOGGER = logging.getLogger(__name__)


async def read_sip_stream_message(
    reader: asyncio.StreamReader,
    *,
    first_byte_timeout: float | None = None,
    frame_timeout: float | None = 10.0,
) -> bytes | None:
    """Read one bounded SIP record from a TCP stream."""

    try:
        if first_byte_timeout is None:
            first = await reader.readexactly(1)
        else:
            async with asyncio.timeout(max(0.001, float(first_byte_timeout))):
                first = await reader.readexactly(1)
    except (TimeoutError, asyncio.IncompleteReadError):
        return None

    if first == b"\r":
        try:
            second = await reader.readexactly(1)
        except asyncio.IncompleteReadError:
            return None
        return b"\r\n" if second == b"\n" else None

    async def _read_remainder() -> bytes | None:
        try:
            head = first + await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            return None
        if len(head) > sip.MAX_SIP_MESSAGE_BYTES:
            return None
        try:
            text = head.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return None
        content_length = 0
        content_length_seen = False
        for line in text.split("\r\n")[1:]:
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            if key.strip().lower() not in {"content-length", "l"}:
                continue
            if content_length_seen:
                return None
            content_length_seen = True
            try:
                content_length = int(value.strip())
            except ValueError:
                return None
        if (
            content_length < 0
            or content_length > sip.MAX_SIP_BODY_BYTES
            or len(head) + content_length > sip.MAX_SIP_MESSAGE_BYTES
        ):
            return None
        try:
            body = await reader.readexactly(content_length) if content_length else b""
        except asyncio.IncompleteReadError:
            return None
        return head + body

    try:
        if frame_timeout is None:
            return await _read_remainder()
        async with asyncio.timeout(max(0.001, float(frame_timeout))):
            return await _read_remainder()
    except TimeoutError:
        return None


class SipTcpWriter:
    """Single-owner async writer for a SIP/TCP connection."""

    def __init__(self, writer: asyncio.StreamWriter, *, label: str, max_queue: int = 64) -> None:
        self.writer = writer
        self.label = label
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=max(1, int(max_queue)))
        self.task = asyncio.create_task(self._run())

    def send_nowait(self, data: bytes) -> bool:
        if self.writer.is_closing() or self.task.done():
            return False
        try:
            self.queue.put_nowait(data)
            return True
        except asyncio.QueueFull:
            _LOGGER.warning("SIP TCP TX queue full for %s; dropping message", self.label)
            return False

    async def send(self, data: bytes) -> bool:
        if self.writer.is_closing() or self.task.done():
            return False
        put_task = asyncio.create_task(self.queue.put(data))
        try:
            done, _ = await asyncio.wait(
                {put_task, self.task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            # queue.put() lives in its own task so it can race writer failure.
            # Explicitly cancel it with the caller or it could enqueue a stale
            # SIP message later, after space becomes available.
            put_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await put_task
            raise
        if put_task in done:
            return not self.task.done()
        put_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await put_task
        return False

    async def close(self) -> None:
        while not self.task.done():
            try:
                self.queue.put_nowait(None)
                break
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    self.queue.get_nowait()
        try:
            await asyncio.wait_for(self.task, timeout=1.0)
        except asyncio.TimeoutError:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task

    async def _run(self) -> None:
        while True:
            data = await self.queue.get()
            if data is None:
                return
            try:
                self.writer.write(data)
                await self.writer.drain()
            except (ConnectionError, RuntimeError, OSError) as err:
                _LOGGER.debug("SIP TCP write failed for %s: %s", self.label, err)
                return
