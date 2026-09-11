"""Shared bounded UDP ingress protocol for SIP clients and trunks."""

from __future__ import annotations

import asyncio

from .sip_capture import capture_io

from .queue_utils import put_drop_oldest


class SipDatagramQueueProtocol(asyncio.DatagramProtocol):
    """Place datagrams in a bounded queue, dropping the oldest on overload."""

    def __init__(self, queue: asyncio.Queue[tuple[bytes, tuple[str, int]]]) -> None:
        self.queue = queue
        self.transport: asyncio.DatagramTransport | None = None
        self.dropped_packets = 0

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr) -> None:
        capture_io(data, self.transport, addr)
        if put_drop_oldest(self.queue, (data, addr)):
            self.dropped_packets += 1
