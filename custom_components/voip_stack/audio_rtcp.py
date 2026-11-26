"""One call leg's RFC 3550 audio reports and owned RTCP UDP lifetime.

RTP owners call observe_receive/observe_send only after admitting or sending an
actual packet. RTCP never owns call termination, RTP decode or media routing.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import random
import socket
import struct
import time

from .core import rtp
from .core.video_rtcp import RtcpError, parse_compound, build_sdes_cname


def _packet(count: int, packet_type: int, payload: bytes) -> bytes:
    return struct.pack("!BBH", 0x80 | count, packet_type, len(payload) // 4) + payload


def _sdes_names(packet) -> dict[int, str]:
    body, offset, names = packet.payload, 0, {}
    if not packet.fmt:
        raise RtcpError("Empty audio SDES packet")
    for _chunk in range(packet.fmt):
        if offset + 4 > len(body):
            raise RtcpError("Truncated audio SDES source")
        source = struct.unpack_from("!I", body, offset)[0]
        offset += 4
        while True:
            if offset >= len(body):
                raise RtcpError("Unterminated audio SDES chunk")
            kind = body[offset]
            offset += 1
            if kind == 0:
                break
            if offset >= len(body):
                raise RtcpError("Truncated audio SDES item")
            size = body[offset]
            offset += 1
            if offset + size > len(body):
                raise RtcpError("Truncated audio SDES value")
            if kind == 1 and size:
                names[source] = body[offset:offset + size].decode("utf-8", errors="replace")
            offset += size
        while offset % 4:
            if offset >= len(body) or body[offset]:
                raise RtcpError("Invalid audio SDES alignment")
            offset += 1
    if offset != len(body):
        raise RtcpError("Unexpected audio SDES payload")
    return names


def report_interval(minimum: float, *, initial: bool, random_value: float | None = None) -> float:
    """RFC 3550 6.2/A.7: randomize the two-member audio profile minimum."""
    # For two unicast audio participants the minimum dominates the bandwidth
    # term. Apply initial halving and compensation before scheduling the timer.
    minimum = float(minimum) / (2 if initial else 1)
    return minimum * (0.5 + (random.random() if random_value is None else random_value)) / 1.21828


@dataclass(slots=True)
class ReceptionReport:
    ssrc: int = 0
    base: int | None = None
    highest: int = 0
    received: int = 0
    previous_expected: int = 0
    previous_received: int = 0
    jitter: float = 0
    last_arrival: float | None = None
    last_timestamp: int = 0
    last_sr: int = 0
    last_sr_arrival: float = 0

    def observe(self, packet: rtp.RtpPacket, clock_rate: int, arrival: float) -> None:
        if self.base is None or packet.ssrc != self.ssrc:
            changed_ssrc = packet.ssrc != self.ssrc
            self.ssrc = packet.ssrc
            self.base = self.highest = packet.sequence
            self.received = self.previous_expected = self.previous_received = 0
            self.jitter = 0
            self.last_arrival = None
            if changed_ssrc:
                self.last_sr = 0
                self.last_sr_arrival = 0
        extended = self.highest + ((packet.sequence - self.highest + 32768) % 65536 - 32768)
        self.highest = max(self.highest, extended)
        self.received += 1
        if self.last_arrival is not None:
            timestamp_delta = (packet.timestamp - self.last_timestamp + 2**31) % 2**32 - 2**31
            difference = abs((arrival - self.last_arrival) * clock_rate - timestamp_delta)
            self.jitter += (difference - self.jitter) / 16
        self.last_arrival, self.last_timestamp = arrival, packet.timestamp

    def block(self, now: float) -> bytes:
        expected = self.highest - self.base + 1
        expected_interval = expected - self.previous_expected
        received_interval = self.received - self.previous_received
        lost_interval = expected_interval - received_interval
        fraction = min(255, (lost_interval << 8) // expected_interval) if expected_interval > 0 and lost_interval > 0 else 0
        self.previous_expected, self.previous_received = expected, self.received
        cumulative = max(-0x800000, min(0x7FFFFF, expected - self.received)) & 0xFFFFFF
        delay = min(0xFFFFFFFF, int((now - self.last_sr_arrival) * 65536)) if self.last_sr else 0
        return struct.pack("!IIIIII", self.ssrc, fraction << 24 | cumulative,
            self.highest & 0xFFFFFFFF, int(self.jitter), self.last_sr, delay)


class AudioRtcpSession(asyncio.DatagramProtocol):
    """Own one RTCP socket and report timer, preserving actual RTP identities."""

    def __init__(self, *, local_port: int, remote_host: str, remote_port: int,
                 local_ssrc: int, clock_rate: int, minimum_interval: float):
        if not 0 < local_port <= 65535 or not 0 < remote_port <= 65535 or minimum_interval <= 0:
            raise ValueError("Invalid audio RTCP endpoint or configured interval")
        self.local_port, self.remote_host, self.remote_port = local_port, remote_host, remote_port
        self.local_ssrc, self.clock_rate = local_ssrc, clock_rate
        self.minimum_interval = minimum_interval
        self.cname = f"voip-stack-{local_ssrc:08x}"
        self.auxiliary_reception = None
        self.auxiliary_sender = None
        self.reception = ReceptionReport()
        self.transport = None
        self.task = None
        self.closed = False
        self.close_with_media = True
        self.sent_packets = self.sent_octets = 0
        self.last_send_time = 0.0
        self.last_send_timestamp = 0
        self.reports_sent = self.reports_received = self.invalid_packets = 0
        self.peer_report = {}
        self.auxiliary_peer_report = {}
        self.peer_cname = ""
        self.auxiliary_peer_bye = False
        self.peer_bye = False
        self._close_timer = None
        self._start_task = None
        self._bind_started = False
        self.bound_socket = None
        self.reporting_allowed = True

    async def start(self, *, sock: socket.socket | None = None, start_reports: bool = True) -> None:
        if self.closed:
            if sock is not None:
                sock.close()
            raise RuntimeError("Audio RTCP session is closed")
        if self._start_task is None:
            # Acquire an explicit descriptor before the queued bind can be cancelled.
            self.bound_socket = sock
            self._start_task = asyncio.create_task(self._start(sock=sock),
                name=f"voip-audio-rtcp-start-{self.local_port}")
        await asyncio.shield(self._start_task)
        if start_reports:
            self.enable_reports()

    def set_reporting_allowed(self, allowed: bool) -> None:
        """Pause reports without discarding the call-owned socket or counters."""
        self.reporting_allowed = bool(allowed)
        if not allowed:
            if self.task is not None and not self.task.done():
                self.task.cancel()
        else:
            self.enable_reports()

    def enable_reports(self) -> None:
        if self.closed or not self.reporting_allowed or self.transport is None:
            return
        if self.task is not None and not self.task.done():
            if self.task.cancelling():
                self.task.add_done_callback(lambda _task: self.enable_reports())
            return
        self.task = asyncio.create_task(self._reports(), name=f"voip-audio-rtcp-{self.local_port}")

    async def _start(self, *, sock: socket.socket | None = None) -> None:
        self._bind_started = True
        try:
            if self.closed:
                raise RuntimeError("Audio RTCP session is closed")
            if self.transport is not None:
                return
            loop = asyncio.get_running_loop()
            if sock is not None:
                self.transport, _ = await loop.create_datagram_endpoint(lambda: self, sock=sock)
            else:
                self.transport, _ = await loop.create_datagram_endpoint(lambda: self, local_addr=("0.0.0.0", self.local_port))
        except BaseException:
            # asyncio has unwound any partial transport before returning here.
            if self.transport is None and self.bound_socket is not None:
                self.bound_socket.close()
            raise
        # Reporting begins only when its owning media transaction commits.

    def observe_receive(self, packet: rtp.RtpPacket | bytes, *, clock_rate: int | None = None, source=None) -> None:
        if isinstance(packet, bytes):
            packet = rtp.parse_packet(packet)
        self.reception.observe(packet, clock_rate or self.clock_rate, time.monotonic())
        # The media owner already admitted this RTP packet. RTCP never admits
        # another RTP source or changes the signaling owner's media route.

    def observe_auxiliary_receive(self, packet: rtp.RtpPacket | bytes, *, clock_rate: int) -> None:
        """Observe only an event SSRC admitted by the existing DTMF decoder."""
        if isinstance(packet, bytes):
            packet = rtp.parse_packet(packet)
        if packet.ssrc == self.reception.ssrc and self.reception.base is not None:
            self.observe_receive(packet, clock_rate=clock_rate)
            return
        if self.auxiliary_reception is None:
            self.auxiliary_reception = ReceptionReport()
        self.auxiliary_reception.observe(packet, clock_rate, time.monotonic())

    def observe_auxiliary_send(self, packet: rtp.RtpPacket | bytes, *, clock_rate: int) -> None:
        """Keep one actual auxiliary sender inside this leg's same RTCP owner."""
        if isinstance(packet, bytes):
            packet = rtp.parse_packet(packet)
        if packet.ssrc == self.local_ssrc:
            self.observe_send(packet, clock_rate=clock_rate)
            return
        previous = self.auxiliary_sender
        if previous is None or previous["ssrc"] != packet.ssrc:
            if previous is not None:
                self.send_report(bye_ssrcs=(previous["ssrc"],))
            previous = self.auxiliary_sender = {"ssrc": packet.ssrc, "clock_rate": clock_rate,
                "packets": 0, "octets": 0, "timestamp": packet.timestamp, "time": 0}
        previous["clock_rate"] = clock_rate
        previous["packets"] += 1
        previous["octets"] += len(packet.payload)
        previous["timestamp"], previous["time"] = packet.timestamp, time.monotonic()

    def update_sender(self, ssrc: int, clock_rate: int) -> None:
        """Commit an actual audio-source change; auxiliary SSRCs never call this."""
        if int(ssrc) != self.local_ssrc:
            try:
                if self.reports_sent or self.sent_packets:
                    self.send_report(bye_ssrcs=(self.local_ssrc,))
            except (OSError, RuntimeError):
                pass
            self.local_ssrc = int(ssrc)
            self.sent_packets = self.sent_octets = 0
            self.last_send_time = 0
            self.last_send_timestamp = 0
        self.clock_rate = int(clock_rate)

    def observe_send(self, packet: rtp.RtpPacket | bytes, *, clock_rate: int | None = None) -> None:
        if isinstance(packet, bytes):
            packet = rtp.parse_packet(packet)
        if packet.ssrc != self.local_ssrc:
            return
        self.clock_rate = clock_rate or self.clock_rate
        self.sent_packets += 1
        self.sent_octets += len(packet.payload)
        self.last_send_timestamp, self.last_send_time = packet.timestamp, time.monotonic()

    def build_report(self, *, bye: bool = False, bye_ssrcs=()) -> bytes:
        now = time.monotonic()
        blocks = [state.block(now) for state in (self.reception, self.auxiliary_reception)
            if state is not None and state.base is not None]
        block = b"".join(blocks)
        count = len(blocks)
        if self.sent_packets:
            ntp = time.time() + 2208988800
            seconds = int(ntp)
            fraction = int((ntp - seconds) * 2**32)
            timestamp = (self.last_send_timestamp + int((now - self.last_send_time) * self.clock_rate)) & 0xFFFFFFFF
            report = _packet(count, 200, struct.pack("!IIIIII", self.local_ssrc, seconds & 0xFFFFFFFF,
                fraction, timestamp, self.sent_packets & 0xFFFFFFFF, self.sent_octets & 0xFFFFFFFF) + block)
        else:
            report = _packet(count, 201, struct.pack("!I", self.local_ssrc) + block)
        auxiliary = self.auxiliary_sender
        if auxiliary is not None:
            ntp = time.time() + 2208988800
            seconds = int(ntp)
            timestamp = (auxiliary["timestamp"] + int((now - auxiliary["time"]) * auxiliary["clock_rate"])) & 0xFFFFFFFF
            report += _packet(0, 200, struct.pack("!IIIIII", auxiliary["ssrc"], seconds & 0xFFFFFFFF,
                int((ntp - seconds) * 2**32), timestamp, auxiliary["packets"] & 0xFFFFFFFF,
                auxiliary["octets"] & 0xFFFFFFFF))
        report += build_sdes_cname(self.local_ssrc, self.cname)
        if auxiliary is not None:
            report += build_sdes_cname(auxiliary["ssrc"], self.cname)
        if bye:
            bye_ssrcs = (self.local_ssrc,) + ((auxiliary["ssrc"],) if auxiliary is not None else ())
        if bye_ssrcs:
            report += _packet(len(bye_ssrcs), 203, b"".join(struct.pack("!I", ssrc) for ssrc in bye_ssrcs))
        return report

    def datagram_received(self, data, address) -> None:
        if self.closed or address[0] != self.remote_host or len(data) > 4096:
            return
        try:
            packets = parse_compound(data)
            if packets[0].packet_type not in {200, 201} or not any(item.packet_type == 202 for item in packets):
                raise RtcpError("An audio compound report needs SR/RR and SDES")
            names = {}
            for packet in packets:
                if packet.packet_type in {200, 201}:
                    offset = 24 if packet.packet_type == 200 else 4
                    if len(packet.payload) != offset + packet.fmt * 24:
                        raise RtcpError("Invalid audio report length")
                elif packet.packet_type == 202:
                    names.update(_sdes_names(packet))
                elif packet.packet_type == 203:
                    offset = packet.fmt * 4
                    if not packet.fmt or len(packet.payload) < offset:
                        raise RtcpError("Invalid audio BYE sources")
                    if len(packet.payload) > offset:
                        end = offset + 1 + packet.payload[offset]
                        if end > len(packet.payload) or any(packet.payload[end:]):
                            raise RtcpError("Invalid audio BYE reason")
            first_source = struct.unpack_from("!I", packets[0].payload)[0]
            if first_source not in names:
                raise RtcpError("Audio report sender lacks its SDES CNAME")
            for packet in packets:
                body = packet.payload
                if packet.packet_type in {200, 201}:
                    offset = 24 if packet.packet_type == 200 else 4
                    if len(body) != offset + packet.fmt * 24:
                        raise RtcpError("Invalid audio report length")
                    source_ssrc = struct.unpack_from("!I", body)[0]
                    reception = self.reception
                    if self.auxiliary_reception is not None and source_ssrc == self.auxiliary_reception.ssrc:
                        reception = self.auxiliary_reception
                    elif reception.base is not None and source_ssrc != reception.ssrc:
                        continue
                    if reception.base is None:
                        reception.ssrc = source_ssrc
                    if reception is self.reception:
                        self.peer_cname = names.get(source_ssrc, self.peer_cname)
                    if packet.packet_type == 200:
                        ntp_seconds, ntp_fraction = struct.unpack_from("!II", body, 4)
                        reception.last_sr = ((ntp_seconds & 0xFFFF) << 16) | (ntp_fraction >> 16)
                        reception.last_sr_arrival = time.monotonic()
                    for index in range(packet.fmt):
                        values = struct.unpack_from("!IIIIII", body, offset + index * 24)
                        auxiliary_ssrc = self.auxiliary_sender["ssrc"] if self.auxiliary_sender else None
                        if values[0] in {self.local_ssrc, auxiliary_ssrc}:
                            loss = values[1] & 0xFFFFFF
                            result = {"fraction_lost": values[1] >> 24,
                                "cumulative_lost": loss - 2**24 if loss & 0x800000 else loss,
                                "highest_sequence": values[2], "jitter": values[3], "lsr": values[4], "dlsr": values[5]}
                            if values[0] == self.local_ssrc:
                                self.peer_report = result
                            else:
                                self.auxiliary_peer_report = result
                elif packet.packet_type == 203:
                    if len(body) < packet.fmt * 4:
                        raise RtcpError("Invalid audio BYE length")
                    sources = {struct.unpack_from("!I", body, index * 4)[0] for index in range(packet.fmt)}
                    self.peer_bye |= self.reception.ssrc in sources
                    self.auxiliary_peer_bye |= bool(self.auxiliary_reception
                        and self.auxiliary_reception.ssrc in sources)
            self.reports_received += 1
        except (RtcpError, struct.error):
            self.invalid_packets += 1

    def send_report(self, *, bye: bool = False, bye_ssrcs=()) -> None:
        if self.reporting_allowed and self.transport is not None and not self.transport.is_closing():
            self.transport.sendto(self.build_report(bye=bye, bye_ssrcs=bye_ssrcs), (self.remote_host, self.remote_port))
            self.reports_sent += 1

    async def _reports(self) -> None:
        initial = True
        while not self.closed and self.reporting_allowed:
            await asyncio.sleep(report_interval(self.minimum_interval, initial=initial))
            if self.closed:
                return
            try:
                self.send_report()
            except (OSError, RuntimeError):
                pass  # Optional RTCP cannot terminate the owned audio call.
            initial = False

    def begin_close(self):
        """Close descriptors synchronously before the media owner releases ports."""
        if self.closed:
            return self._close_timer
        try:
            if self.reports_sent or self.sent_packets or self.auxiliary_sender is not None:
                self.send_report(bye=True)
        except (OSError, RuntimeError):
            pass
        self.closed = True
        if self._start_task is not None and not self._start_task.done():
            self._start_task.cancel()
        if not self._bind_started and self.bound_socket is not None:
            self.bound_socket.close()
        task, self.task = self.task, None
        self._close_timer = task
        if task is not None:
            task.cancel()
        transport, self.transport = self.transport, None
        if transport is not None:
            transport.close()
        return task

    async def close(self) -> None:
        task = self.begin_close()
        pending = [pending for pending in (task, self._start_task) if pending is not None]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


def audio_rtcp_target(remote_sdp) -> tuple[str, int]:
    """Project the selected RTP/AVP audio section through the existing SDP parser."""
    from .core.sdp_parser import parse_media_sections

    connection, _direction, sections = parse_media_sections(remote_sdp)
    selected = next((section for section in sections if section["media"] == "audio"
        and section["transport"] == "RTP/AVP" and section["port"] > 0), None)
    if selected is None:
        raise ValueError("No active RTP/AVP audio section")
    host = selected["rtcp_address"] or selected["connection_ip"] or connection
    port = selected["rtcp_port"] or selected["port"] + 1
    if not host or not 0 < port <= 65535:
        raise ValueError("Invalid audio RTCP target")
    # This profile does not negotiate audio RTCP multiplexing, so an optional
    # peer rtcp-mux offer retains its ordinary separate-port fallback.
    return str(host), int(port)


async def async_get_or_create(hass, owner_session, reservation, local_rtp_port: int, *,
                              remote_host: str, remote_port: int, local_ssrc: int,
                              clock_rate: int, minimum_interval: float,
                              rtcp_socket: socket.socket | None = None,
                              audio_rtcp_allowed: bool = True) -> AudioRtcpSession | None:
    """Adopt one call resource before yielding, then await its single bind owner."""
    from .endpoint_session import CleanupStage

    name = f"audio_rtcp:{int(local_rtp_port)}"
    resource = next((resource for resource in owner_session.resources if resource.name == name), None)
    if not audio_rtcp_allowed:
        if resource is None:
            return None
        resource.value.set_reporting_allowed(False)
        return resource.value
    if resource is None:
        owner = AudioRtcpSession(local_port=int(local_rtp_port) + 1,
            remote_host=remote_host, remote_port=remote_port, local_ssrc=local_ssrc,
            clock_rate=clock_rate, minimum_interval=minimum_interval)
        async def close(_reason):
            await owner.close()
        owner.close_with_media = False
        owner_session.add_resource(name, owner, close, stage=CleanupStage.MEDIA)
        sock = rtcp_socket
        if sock is None and reservation is not None:
            sock = reservation.take_audio_socket(int(local_rtp_port) + 1)
    else:
        owner, sock = resource.value, None
        if rtcp_socket is not None and rtcp_socket is not owner.bound_socket:
            rtcp_socket.close()
    owner.set_reporting_allowed(True)
    await owner.start(sock=sock)
    return owner


def remote_audio_rtcp_fields(remote_sdp) -> dict:
    host, port = audio_rtcp_target(remote_sdp)
    from .core.sdp_parser import audio_rtcp_allowed
    return {"remote_audio_rtcp_host": host, "remote_audio_rtcp_port": port,
            "audio_rtcp_allowed": audio_rtcp_allowed(remote_sdp)}


async def async_prepare_browser_rtcp(hass, owner_session, contract, *, client=None, descriptor=None):
    """Start accepted early/final media control even while the card is detached."""
    if contract.audio_rtcp_min_interval <= 0:
        return None
    if client is not None:
        source = client.audio_rtp_source
        if source is None:
            source = rtp.AudioRtpSenderState.create()
            client.audio_rtp_source = source
        reservation, local_port = client.media_reservation, client.local_rtp_port
        sock = client.audio_rtcp_socket
    else:
        source = descriptor.get("audio_rtp_source")
        if source is None:
            source = rtp.AudioRtpSenderState.create()
            descriptor["audio_rtp_source"] = source
        reservation, local_port = descriptor.get("rtp_reservation"), descriptor["local_rtp_port"]
        sock = descriptor.get("audio_rtcp_socket")
    source.use_clock_rate(contract.send_format.rtp_clock_rate)
    owner = await async_get_or_create(hass, owner_session, reservation, local_port,
        remote_host=contract.remote_audio_rtcp_host or contract.remote_rtp_host,
        remote_port=contract.remote_audio_rtcp_port or contract.remote_rtp_port + 1,
        local_ssrc=source.ssrc, clock_rate=contract.send_format.rtp_clock_rate,
        minimum_interval=contract.audio_rtcp_min_interval, rtcp_socket=sock,
        audio_rtcp_allowed=contract.audio_rtcp_allowed)
    if owner is None:
        return None
    if contract.audio_rtcp_allowed and client is not None and client.audio_rtcp_socket is sock:
        client.audio_rtcp_socket = None
    elif contract.audio_rtcp_allowed and descriptor is not None and descriptor.get("audio_rtcp_socket") is sock:
        descriptor.pop("audio_rtcp_socket", None)
    owner.remote_host = contract.remote_audio_rtcp_host or contract.remote_rtp_host
    owner.remote_port = contract.remote_audio_rtcp_port or contract.remote_rtp_port + 1
    owner.minimum_interval = contract.audio_rtcp_min_interval
    owner.update_sender(source.ssrc, contract.send_format.rtp_clock_rate)
    return owner


def observe_validated_event(owner, decoder) -> None:
    """Use the existing DTMF admission/clock result, including wire duplicates."""
    observed = getattr(decoder, "last_wire_packet", None)
    if owner is None or observed is None:
        return
    packet, clock_rate, shared_audio = observed
    if shared_audio:
        owner.observe_receive(packet, clock_rate=clock_rate)
    else:
        owner.observe_auxiliary_receive(packet, clock_rate=clock_rate)
