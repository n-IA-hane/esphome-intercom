"""Bounded, opt-in SIP message capture, exported as Wireshark Upper PDU PCAP.

This observes application I/O, not kernel packets. It deliberately invents no
IP/TCP headers, ACKs, sequence numbers or successful remote delivery evidence.
All mutation runs on the owning event loop; no disk I/O occurs in SIP callbacks.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import secrets
import struct
import time
from weakref import WeakKeyDictionary

# Wireshark wsutil/exported_pdu_tlvs.h; LINKTYPE_WIRESHARK_UPPER_PDU = 252.
_PCAP_HEADER = struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 262144, 252)
_AUTH = re.compile(
    rb"(?im)^(authorization|proxy-authorization|authentication-info|proxy-authentication-info)[ \t]*:[^\r\n]*(?:\r\n[ \t][^\r\n]*)*"
)
_ACTIVE: WeakKeyDictionary[asyncio.AbstractEventLoop, SipCapture] = WeakKeyDictionary()


def _tlv(tag: int, value: bytes) -> bytes:
    value += b"\0" * (-len(value) % 4)
    return struct.pack("!HH", tag, len(value)) + value


def _address(address: tuple, *, source: bool) -> bytes:
    ip = ipaddress.ip_address(address[0].split("%", 1)[0])
    tag = (20 if ip.version == 4 else 22) + (0 if source else 1)
    return _tlv(tag, ip.packed) + _tlv(
        25 if source else 26, struct.pack("!I", address[1])
    )


class SipCapture:
    """One capture per HA event loop; bounded storage is retained for 15 minutes."""

    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.capture_id = ""
        self.buffer = bytearray()
        self.messages = 0
        self.dropped = 0
        self.reason = "not_started"
        self.max_bytes = 0
        self._deadline: asyncio.TimerHandle | None = None
        self._expiry: asyncio.TimerHandle | None = None

    @property
    def active(self) -> bool:
        return _ACTIVE.get(self.loop) is self

    def start(self, duration: int = 120, max_bytes: int = 4 * 1024 * 1024) -> None:
        if self.loop in _ACTIVE:
            raise ValueError("A SIP capture is already running; stop it first")
        self.clear()
        self.capture_id = secrets.token_hex(24)
        self.buffer.extend(_PCAP_HEADER)
        self.messages = self.dropped = 0
        self.max_bytes = max_bytes
        self.reason = "capturing"
        _ACTIVE[self.loop] = self
        self._deadline = self.loop.call_later(duration, self.stop, "duration_limit")

    def stop(self, reason: str = "stopped") -> None:
        if self.active:
            del _ACTIVE[self.loop]
            self.reason = reason
            if self._deadline is not None:
                self._deadline.cancel()
                self._deadline = None
            self._expiry = self.loop.call_later(900, self.clear)

    def clear(self) -> None:
        self.stop()
        if self._expiry is not None:
            self._expiry.cancel()
            self._expiry = None
        self.buffer.clear()
        self.capture_id = ""
        self.messages = self.dropped = 0
        self.reason = "cleared"

    def record(
        self, data: bytes, local: tuple, remote: tuple, tcp: bool, outgoing: bool
    ) -> None:
        if len(data) <= 4:  # CRLF keepalives are not SIP messages.
            return
        if len(data) > 131072:
            self.dropped += 1
            return
        source, destination = (local, remote) if outgoing else (remote, local)
        metadata = (
            _tlv(12, b"sip\0")
            + _address(source, source=True)
            + _address(destination, source=False)
            + _tlv(24, struct.pack("!I", 2 if tcp else 3))
            + _tlv(0, b"")
        )
        head, separator, body = data.partition(b"\r\n\r\n")
        payload = metadata + _AUTH.sub(rb"\1: [redacted]", head) + separator + body
        if (
            len(self.buffer) + 16 + len(payload) > self.max_bytes
            or self.messages >= 20000
        ):
            self.dropped += 1
            self.stop("size_limit")
            return
        now = time.time_ns() // 1000
        self.buffer.extend(
            struct.pack(
                "<IIII", now // 1000000, now % 1000000, len(payload), len(payload)
            )
        )
        self.buffer.extend(payload)
        self.messages += 1

    def status(self) -> dict:
        return {
            "active": self.active,
            "messages": self.messages,
            "bytes": len(self.buffer),
            "dropped": self.dropped,
            "reason": self.reason,
            "format": "pcap_wireshark_upper_pdu",
        }


def capture_io(
    data: bytes,
    transport,
    remote: tuple | None = None,
    *,
    tcp: bool = False,
    outgoing: bool = False,
) -> None:
    """Observe SIP socket I/O once, never the later forwarding/dispatch steps."""
    if not _ACTIVE:
        return
    capture = _ACTIVE.get(asyncio.get_running_loop())
    if capture is None:
        return
    try:
        local = transport.get_extra_info("sockname")
        peer = remote if remote is not None else transport.get_extra_info("peername")
        capture.record(data, local, peer, tcp, outgoing)
    except (
        AttributeError,
        TypeError,
        ValueError,
        OverflowError,
        OSError,
        struct.error,
    ):
        # Diagnostics must never break signaling, even if a closing socket has
        # lost its endpoint metadata. Report the missing observation explicitly.
        capture.dropped += 1


def send_sip_datagram(transport, data: bytes, remote: tuple) -> None:
    """Send through the existing transport, recording only accepted send calls."""
    transport.sendto(data, remote)
    capture_io(data, transport, remote, outgoing=True)
