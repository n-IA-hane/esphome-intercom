"""Bounded video datagram I/O below the browser session lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .core import rtp
from .core.sip_stun import is_stun_message

_MAX_VIDEO_DATAGRAM_BYTES = 65_507


class _ByteBudgetQueue(asyncio.Queue):
    """An asyncio queue bounded by both item count and retained payload bytes."""

    def __init__(
        self,
        *,
        maxsize: int,
        max_bytes: int,
        item_size: Callable[[object], int],
    ) -> None:
        super().__init__(maxsize=maxsize)
        self.max_bytes = int(max_bytes)
        self.queued_bytes = 0
        self._item_size = item_size

    def _put(self, item) -> None:
        self.queued_bytes += max(0, int(self._item_size(item)))
        super()._put(item)

    def _get(self):
        item = super()._get()
        self.queued_bytes = max(
            0,
            self.queued_bytes - max(0, int(self._item_size(item))),
        )
        return item

    def can_fit(self, item) -> bool:
        size = max(0, int(self._item_size(item)))
        return size <= self.max_bytes and self.queued_bytes + size <= self.max_bytes

    def put_nowait(self, item) -> None:
        """Enforce the byte invariant even when a future caller forgets can_fit."""

        if not self.can_fit(item):
            raise asyncio.QueueFull
        super().put_nowait(item)


class _RtpVideoProtocol(asyncio.DatagramProtocol):
    def __init__(
        self,
        queue: _ByteBudgetQueue,
        *,
        source_allowed: Callable[[str], bool],
        min_datagram_bytes: int,
        max_datagram_bytes: int = _MAX_VIDEO_DATAGRAM_BYTES,
    ) -> None:
        self.queue = queue
        self.source_allowed = source_allowed
        self.min_datagram_bytes = int(min_datagram_bytes)
        self.max_datagram_bytes = int(max_datagram_bytes)
        self.dropped_packets = 0
        self._queue_drops = 0
        self._source_drops = 0
        self._size_drops = 0

    def _record_drop(self, reason: str) -> None:
        self.dropped_packets += 1
        if reason == "queue":
            self._queue_drops += 1
        elif reason == "source":
            self._source_drops += 1
        else:
            self._size_drops += 1

    def take_drop_counts(self) -> tuple[int, int, int]:
        """Return new queue/source/size drops since the preceding snapshot."""

        counts = (self._queue_drops, self._source_drops, self._size_drops)
        self._queue_drops = 0
        self._source_drops = 0
        self._size_drops = 0
        return counts

    def datagram_received(self, data: bytes, addr) -> None:
        if is_stun_message(data):
            return
        if not self.min_datagram_bytes <= len(data) <= self.max_datagram_bytes:
            self._record_drop("size")
            return
        if not self.source_allowed(str(addr[0])):
            self._record_drop("source")
            return
        item = (data, addr)
        while self.queue.full() or not self.queue.can_fit(item):
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                self._record_drop("size")
                return
            self._record_drop("queue")
        self.queue.put_nowait(item)



def close_detached_video_sockets(session) -> None:
    """Close sockets still retained by the existing session, clearing references."""
    for name in ("rtp_socket", "rtcp_socket"):
        sock = getattr(session, name)
        if sock is not None:
            sock.close()
            setattr(session, name, None)


async def async_open_video_receiver(session, protocol) -> asyncio.DatagramTransport:
    """Acquire one RTP reader or release the detached sockets on any failure."""
    loop = asyncio.get_running_loop()
    try:
        if session.rtp_socket is not None:
            transport, _ = await loop.create_datagram_endpoint(lambda: protocol, sock=session.rtp_socket)
        else:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: protocol, local_addr=("0.0.0.0", int(session.local_rtp_port))
            )
    except BaseException:
        close_detached_video_sockets(session)
        raise
    return transport


@dataclass(frozen=True, slots=True)
class ReceiveTranscodePipeline:
    """Acquired I/O values; the calling media session retains their ownership."""
    transport: asyncio.DatagramTransport
    protocol: _RtpVideoProtocol
    queue: _ByteBudgetQueue
    transcoder: Any


async def async_open_receive_transcoder(
    hass, session, *, transcoder_factory, max_packets: int, max_bytes: int,
    close_parent_io: Callable[[], None] | None = None,
) -> ReceiveTranscodePipeline:
    """Acquire only the receive-side pipeline, rolling back partial setup."""
    queue = _ByteBudgetQueue(maxsize=max_packets, max_bytes=max_bytes,
                             item_size=lambda item: len(item[0]))
    protocol = _RtpVideoProtocol(queue, source_allowed=lambda host: host in {"127.0.0.1", "::1"},
                                 min_datagram_bytes=rtp.RTP_HEADER_SIZE)
    transport = None
    transcoder = None
    try:
        transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: protocol, local_addr=("127.0.0.1", 0)
        )
        transcoder = transcoder_factory(hass=hass, call_id=session.call_id,
            input_format=session.recv_video_format,
            output_port=int(transport.get_extra_info("sockname")[1]))
        await transcoder.async_start()
        return ReceiveTranscodePipeline(transport, protocol, queue, transcoder)
    except BaseException:
        if close_parent_io is not None:
            close_parent_io()
        if transport is not None:
            transport.close()
        if transcoder is not None:
            await transcoder.async_close()
        raise
