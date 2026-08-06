"""DTMF collection helpers for SIP trunk inbound routing."""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import struct
import time
from typing import Callable

from .core.rtp import parse_packet


_LOGGER = logging.getLogger(__name__)
_EVENT_DIGITS = {
    0: "0",
    1: "1",
    2: "2",
    3: "3",
    4: "4",
    5: "5",
    6: "6",
    7: "7",
    8: "8",
    9: "9",
    10: "*",
    11: "#",
    12: "A",
    13: "B",
    14: "C",
    15: "D",
}
_INFO_SIGNAL_RE = re.compile(r"(?:^|\r?\n)\s*Signal\s*=\s*([0-9]{1,2}|[*#A-D])\s*(?:\r?\n|$)", re.IGNORECASE)
_INFO_EVENT_CODES = {"10": "*", "11": "#", "12": "A", "13": "B", "14": "C", "15": "D"}
_DIGIT_EVENT_CODES = {digit: event for event, digit in _EVENT_DIGITS.items()}


def parse_sip_info_digit(content_type: str, body: bytes) -> str:
    """Parse one legacy DTMF digit carried by an in-dialog SIP INFO."""
    media_type = str(content_type or "").split(";", 1)[0].strip().lower()
    text = body.decode("ascii", errors="ignore").strip()
    if media_type == "application/dtmf-relay":
        match = _INFO_SIGNAL_RE.search(text)
        if not match:
            return ""
        signal = match.group(1).upper()
        return _INFO_EVENT_CODES.get(signal, signal if signal in "0123456789*#ABCD" else "")
    if media_type == "application/dtmf" and text:
        digit = text[0].upper()
        return digit if digit in "0123456789*#ABCD" else ""
    return ""


def telephone_event_code(digit: str) -> int | None:
    """Return the RFC 4733 event code for one DTMF symbol."""

    return _DIGIT_EVENT_CODES.get(str(digit or "").strip().upper())


def build_telephone_event_payload(
    digit: str,
    *,
    duration: int,
    end: bool = False,
    volume: int = 10,
) -> bytes:
    """Build the four-byte RFC 4733 named-event payload."""

    event = telephone_event_code(digit)
    if event is None:
        raise ValueError("unsupported DTMF digit")
    if not 1 <= int(duration) <= 0xFFFF:
        raise ValueError("telephone-event duration must be between 1 and 65535")
    if not 0 <= int(volume) <= 63:
        raise ValueError("telephone-event volume must be between 0 and 63")
    flags = (0x80 if end else 0) | int(volume)
    return struct.pack("!BBH", event, flags, int(duration))


class RtpDtmfDecoder:
    """Decode one event per RFC 4733 DTMF press from a negotiated RTP PT."""

    def __init__(self, payload_type: int) -> None:
        self.payload_type = int(payload_type)
        self.ssrc: int | None = None
        self._seen_events: set[tuple[int, int]] = set()

    def decode(self, data: bytes, *, expected_ssrc: int | None = None) -> str:
        try:
            packet = parse_packet(data)
        except ValueError:
            return ""
        if packet.payload_type != self.payload_type or len(packet.payload) < 4:
            return ""
        if expected_ssrc is not None and packet.ssrc != expected_ssrc:
            return ""
        if self.ssrc is None:
            self.ssrc = packet.ssrc
        elif packet.ssrc != self.ssrc:
            return ""
        event = packet.payload[0]
        digit = _EVENT_DIGITS.get(event, "")
        if not digit:
            return ""
        key = (packet.timestamp, event)
        if key in self._seen_events:
            return ""
        self._seen_events.add(key)
        if len(self._seen_events) > 256:
            self._seen_events.clear()
            self._seen_events.add(key)
        return digit


async def collect_info_digits(
    queue: asyncio.Queue[str],
    *,
    routes: dict[str, str],
    timeout: float,
    first_digit_timeout: float | None = None,
    terminator: str = "",
) -> tuple[str, str]:
    """Collect SIP INFO digits with separate first and inter-digit timers."""
    started = time.monotonic()
    inter_digit_timeout = max(0.1, float(timeout))
    first_timeout = max(
        inter_digit_timeout,
        float(first_digit_timeout or inter_digit_timeout),
    )
    buffer = ""
    destination = ""
    while True:
        try:
            digit = await asyncio.wait_for(
                queue.get(),
                timeout=first_timeout if not buffer else inter_digit_timeout,
            )
        except asyncio.TimeoutError:
            break
        buffer += digit
        destination, terminal = _match_dtmf(buffer, routes, terminator=terminator)
        if terminal:
            break
    if not destination:
        destination = routes.get(buffer, "")
    _LOGGER.info(
        "SIP trunk INFO DTMF collection finished buffer=%s destination=%s elapsed_ms=%d",
        buffer or "-",
        destination or "-",
        int((time.monotonic() - started) * 1000),
    )
    return buffer, destination


def _match_dtmf(buffer: str, routes: dict[str, str], *, terminator: str = "") -> tuple[str, bool]:
    if terminator and buffer.endswith(terminator):
        code = buffer[: -len(terminator)]
        return (routes.get(code, ""), True)
    if buffer in routes:
        ambiguous = any(key != buffer and key.startswith(buffer) for key in routes)
        if not ambiguous:
            return (routes[buffer], True)
    return ("", False)


class _DtmfProtocol(asyncio.DatagramProtocol):
    def __init__(
        self,
        payload_type: int,
        on_digit: Callable[[str], None],
        *,
        remote_host: str = "",
    ) -> None:
        self.payload_type = int(payload_type)
        self.on_digit = on_digit
        self.remote_host = str(remote_host or "")
        self._decoder = RtpDtmfDecoder(self.payload_type)

    def datagram_received(self, data: bytes, addr) -> None:
        if self.remote_host and str(addr[0]) != self.remote_host:
            return
        digit = self._decoder.decode(data)
        if not digit:
            return
        packet = parse_packet(data)
        _LOGGER.debug(
            "SIP trunk DTMF RX digit=%s seq=%s end=%s from=%s:%s",
            digit,
            packet.sequence,
            bool(packet.payload[1] & 0x80),
            addr[0],
            addr[1],
        )
        self.on_digit(digit)


class DtmfCollector:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        payload_type: int,
        routes: dict[str, str],
        timeout: float,
        first_digit_timeout: float | None = None,
        terminator: str = "",
        remote_host: str = "",
    ) -> None:
        self.host = host
        self.port = int(port)
        self.payload_type = int(payload_type)
        self.routes = routes
        self.timeout = max(0.1, float(timeout))
        self.first_digit_timeout = max(
            self.timeout,
            float(first_digit_timeout or self.timeout),
        )
        self.terminator = terminator
        self.remote_host = str(remote_host or "")
        self.buffer = ""
        self.transport: asyncio.DatagramTransport | None = None
        self._done: asyncio.Future[str] | None = None
        self._activity: asyncio.Event | None = None

    async def collect(self) -> tuple[str, str]:
        loop = asyncio.get_running_loop()
        self._done = loop.create_future()
        self._activity = asyncio.Event()
        protocol = _DtmfProtocol(
            self.payload_type,
            self._on_digit,
            remote_host=self.remote_host,
        )
        transport, _ = await loop.create_datagram_endpoint(
            lambda: protocol,
            local_addr=(self.host, self.port),
            family=socket.AF_INET,
        )
        self.transport = transport  # type: ignore[assignment]
        started = time.monotonic()
        try:
            destination = ""
            while not self._done.done():
                assert self._activity is not None
                self._activity.clear()
                try:
                    await asyncio.wait_for(
                        self._activity.wait(),
                        timeout=(
                            self.first_digit_timeout
                            if not self.buffer
                            else self.timeout
                        ),
                    )
                except asyncio.TimeoutError:
                    break
            if self._done.done():
                destination = self._done.result()
            elif self.buffer:
                destination = self.routes.get(self.buffer, "")
            _LOGGER.info(
                "SIP trunk DTMF collection finished buffer=%s destination=%s elapsed_ms=%d",
                self.buffer or "-",
                destination or "-",
                int((time.monotonic() - started) * 1000),
            )
            return self.buffer, destination
        finally:
            transport.close()
            self.transport = None

    def _on_digit(self, digit: str) -> None:
        if self._done is None or self._done.done():
            return
        self.buffer += digit
        if self._activity is not None:
            self._activity.set()
        destination, terminal = _match_dtmf(self.buffer, self.routes, terminator=self.terminator)
        if terminal:
            self._done.set_result(destination)
