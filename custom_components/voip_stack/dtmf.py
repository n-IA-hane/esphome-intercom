"""DTMF collection helpers for SIP trunk inbound routing."""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import struct
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from .core.rtp import RtpReceiveState, parse_packet


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


def parse_sip_info_duration(body: bytes) -> int:
    """Preserve the stated press duration, using the legacy default if absent."""
    match = re.search(r"^Duration\s*=\s*(\d+)\s*$", body.decode("ascii", errors="ignore"), re.I | re.M)
    if match is None:
        return 160
    value = match[1].lstrip("0") or "0"
    return 5000 if len(value) > 4 else max(40, min(5000, int(value)))


def build_sip_info_dtmf_body(digit: str, *, duration_ms: int = 160) -> bytes:
    """Build one legacy application/dtmf-relay body for an active dialog."""

    normalized = str(digit or "").strip().upper()
    if telephone_event_code(normalized) is None:
        raise ValueError("unsupported DTMF digit")
    duration = max(40, min(5000, int(duration_ms)))
    return f"Signal={normalized}\r\nDuration={duration}\r\n".encode("ascii")


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


async def send_rtp_dtmf_event(
    digit: str,
    *,
    clock_rate: int,
    duration_ms: int,
    emit: Callable[[int, bool, bool, int], Awaitable[bool]],
    maximum_duration_ms: int | None = 5000,
    initial_duration_ticks: int = 1,
) -> bool:
    """Pace one RFC 4733 event and repeat its final report three times."""

    if telephone_event_code(digit) is None:
        return False
    rate = max(1, int(clock_rate))
    duration_ms = max(40, int(duration_ms))
    if maximum_duration_ms is not None:
        duration_ms = min(maximum_duration_ms, duration_ms)
    elapsed_ms = 0
    if not await emit(max(1, initial_duration_ticks), True, False, 0):
        return False
    previous_segment = 0
    while elapsed_ms < duration_ms:
        step_ms = min(50, duration_ms - elapsed_ms)
        await asyncio.sleep(step_ms / 1000)
        elapsed_ms += step_ms
        duration = max(1, round(elapsed_ms * rate / 1000))
        for offset, segment_duration, end in telephone_event_segments(duration,
                previous_segment=previous_segment, end=elapsed_ms >= duration_ms):
            if not await emit(segment_duration, False, end, offset):
                return False
        previous_segment = (duration - 1) // 65535
    for _repeat in range(2):
        await asyncio.sleep(0.05)
        offset, segment_duration, _end = next(telephone_event_segments(duration,
            previous_segment=previous_segment, end=True))
        if not await emit(segment_duration, False, True, offset):
            return False
    return True


@dataclass(frozen=True, slots=True)
class RtpDtmfReport:
    digit: str
    timestamp: int
    ssrc: int
    duration: int
    end: bool
    volume: int
    marker: bool
    first: bool
    source_changed: bool = False
    sequence: int = 0
    sample_rate: int = 8000
    shared_audio: bool = False


def telephone_event_segments(duration, *, previous_segment=0, end=False):
    """Split a long RFC 4733 event into continuous 16-bit duration segments."""
    duration = max(1, int(duration))
    segment = (duration - 1) // 65535
    if segment < previous_segment:
        return
    for index in range(previous_segment, segment):
        yield index * 65535, 65535, False
    yield segment * 65535, (duration - 1) % 65535 + 1, end


class RtpDtmfDecoder:
    """Decode one event per RFC 4733 DTMF press from a negotiated RTP PT."""

    def __init__(self, payload_type, *, receive_state=None, audio_receiver=None) -> None:
        from .core.sdp_types import RtpDtmfFormat

        formats = (RtpDtmfFormat(int(payload_type), 8000),) if isinstance(payload_type, int) else tuple(payload_type)
        self.formats = {fmt.payload_type: fmt for fmt in formats}
        self.payload_type = next(iter(self.formats))
        self.audio_receiver = audio_receiver
        self._clock_states = {}
        self.ssrc: int | None = None
        self._seen_events: dict[tuple[int, int, int], tuple[int, bool, RtpDtmfFormat]] = {}
        self._receive_state = receive_state if receive_state is not None else RtpReceiveState()
        self._clock_states[self.formats[self.payload_type].sample_rate] = self._receive_state
        self._active_events = {}

    @property
    def receive_state(self):
        return self._receive_state

    def accepts(self, payload_type):
        return payload_type in self.formats

    def decode(self, data: bytes, *, expected_ssrc: int | None = None) -> str:
        report = self.decode_report(data, expected_ssrc=expected_ssrc)
        return report.digit if report is not None and report.first else ""

    def decode_report(self, data: bytes, *, expected_ssrc: int | None = None) -> RtpDtmfReport | None:
        """Keep duration/progress/end while presenting one callback per press."""
        try:
            packet = parse_packet(data)
        except ValueError:
            return None
        fmt = self.formats.get(packet.payload_type)
        if fmt is None or len(packet.payload) < 4:
            return None
        event = packet.payload[0]
        digit = _EVENT_DIGITS.get(event, "")
        flags, duration = packet.payload[1], int.from_bytes(packet.payload[2:4], "big")
        if not digit or event not in fmt.events or flags & 64 or not duration:
            return None
        key = (packet.ssrc, packet.timestamp, event)
        previous = self._seen_events.get(key)
        if previous is not None and previous[2].sample_rate != fmt.sample_rate:
            return None
        receiver = self.audio_receiver
        # SSRC owns the RTP sequence space even when telephone-event and audio
        # use different timestamp clocks. A separate event SSRC keeps its guard.
        shared = bool(receiver is not None and (
            packet.ssrc == receiver.source_ssrc
            or packet.ssrc == receiver.receive_state.probation_ssrc
            or (receiver.source_ssrc is None and expected_ssrc == packet.ssrc)))
        if shared:
            self._receive_state = receiver.receive_state
        else:
            state = next((state for state in self._clock_states.values()
                          if state.ssrc == packet.ssrc), None)
            if state is None:
                state = self._clock_states.get(fmt.sample_rate)
            if state is None:
                state = self._clock_states[fmt.sample_rate] = RtpReceiveState()
            self._receive_state = state
        if self._receive_state.ssrc is None and expected_ssrc is not None:
            self._receive_state.ssrc = expected_ssrc
        if not self._receive_state.admit(packet, clock_rate=fmt.sample_rate):
            return None
        if self._receive_state.changed:
            self._seen_events = {key: value for key, value in self._seen_events.items() if key[0] != packet.ssrc}
            self._active_events = {key: value for key, value in self._active_events.items() if key[0] != packet.ssrc}
        self.ssrc = packet.ssrc
        key = (packet.ssrc, packet.timestamp, event)
        previous = self._seen_events.get(key)
        ended = bool(flags & 128)
        if previous is not None and (duration < previous[0] or (previous[1] and not ended)):
            return None
        self._seen_events[key] = (duration, ended, fmt)
        if len(self._seen_events) > 256:
            del self._seen_events[next(iter(self._seen_events))]
        active_key = (packet.ssrc, event)
        active = self._active_events.get(active_key)
        first = previous is None
        origin, elapsed = packet.timestamp, duration
        if active is not None:
            source_timestamp, origin, base, active_ended, active_clock = active
            if (source_timestamp != packet.timestamp and previous is not None
                    and ((packet.timestamp - source_timestamp) & 0xFFFFFFFF) > 0x80000000):
                return None
            if source_timestamp == packet.timestamp:
                elapsed += base
            elif (not active_ended and not packet.marker
                    and ((packet.timestamp - source_timestamp) & 0xFFFFFFFF) == 65535):
                if active_clock != fmt.sample_rate:
                    return None
                base += 65535
                elapsed += base
                first = False
            else:
                origin, base = packet.timestamp, 0
        else:
            base = 0
        self._active_events[active_key] = (packet.timestamp, origin, base, ended, fmt.sample_rate)
        if len(self._active_events) > 64:
            del self._active_events[next(iter(self._active_events))]
        return RtpDtmfReport(digit, origin, packet.ssrc, elapsed, ended,
            flags & 63, packet.marker, first, self._receive_state.changed, packet.sequence, fmt.sample_rate, shared)


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
        rtp_socket: socket.socket | None = None,
    ) -> None:
        self.rtp_socket = rtp_socket
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
        transport = None
        borrowed_fd = None
        if self.rtp_socket is None:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: protocol,
                local_addr=(self.host, self.port),
                family=socket.AF_INET,
            )
            self.transport = transport  # type: ignore[assignment]
        else:
            if self.rtp_socket.getsockname()[1] != self.port or self.rtp_socket.getblocking():
                raise ValueError("DTMF socket does not match the reserved RTP port")
            borrowed_fd = self.rtp_socket.fileno()

            def receive():
                try:
                    data, address = self.rtp_socket.recvfrom(65535)
                except BlockingIOError:
                    return
                protocol.datagram_received(data, address)

            loop.add_reader(borrowed_fd, receive)
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
            if borrowed_fd is not None:
                loop.remove_reader(borrowed_fd)
            if transport is not None:
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
