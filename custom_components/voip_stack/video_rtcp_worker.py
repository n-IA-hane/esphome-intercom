"""RTCP reports and keyframe feedback borrow the current receive state."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
import logging
import struct
import time
from typing import TYPE_CHECKING

from aiohttp import web

from .core.video_rtcp import RtcpError, RtcpPacket
from .core.video_rtp import RtpTimestampClock
from .video_receive_state import VideoReceiveState
from .video_ws_io import _ByteBudgetQueue

if TYPE_CHECKING:
    from .video_ws_view import _VideoMediaSession

_LOGGER = logging.getLogger(f"{__package__}.video_ws_view")

@dataclass(frozen=True, slots=True)
class VideoRtcpHooks:
    build_pli: Callable[[int, int], bytes]
    build_fir: Callable[[int, int, int], bytes]
    build_receiver_compound: Callable[..., bytes]
    build_sender_compound: Callable[..., bytes]
    parse_compound: Callable[[bytes], list[RtcpPacket]]


@dataclass(slots=True)
class VideoRtcpWorkers:
    """The parent creates, cancels and joins every task and transport."""

    session: _VideoMediaSession
    state: VideoReceiveState
    ssrc: int
    outbound_clock: RtpTimestampClock
    ws: web.WebSocketResponse
    ws_send_lock: asyncio.Lock
    closed: asyncio.Event
    rtcp_queue: _ByteBudgetQueue
    store_counters: Callable[..., None]
    hooks: VideoRtcpHooks
    loop: asyncio.AbstractEventLoop
    report_interval: float
    keyframe_feedback_interval: float
    receive_burst_packets: int
    transport: asyncio.DatagramTransport | None = None
    keyframe_task: asyncio.Task[bool] | None = field(default=None, init=False)

    def record_rtcp_send_error(self, context: str, err: BaseException) -> None:
        """Count persistent RTCP send failures without flooding HA logs."""

        self.state.counters["video_rtcp_send_errors"] += 1
        failures = self.state.counters["video_rtcp_send_errors"]
        # Log the first failure and powers of two thereafter. Diagnostics retain
        # the exact count while a broken route cannot emit a warning every RTP
        # loss or five-second report interval indefinitely.
        if failures & (failures - 1) == 0:
            _LOGGER.warning(
                "HA softphone video RTCP %s failed call_id=%s failures=%d error=%s",
                context,
                self.session.call_id,
                failures,
                err,
            )
        self.store_counters()

    def request_key_frame(self, now: float) -> None:
        if (
            self.session.remote_connection_held
            or self.state.route.latched_ssrc is None
            or now - self.state.last_keyframe_feedback < self.keyframe_feedback_interval
        ):
            return
        feedback = set(self.session.send_video_format.rtcp_feedback)
        feedback_packet = None
        if "nack pli" in feedback:
            feedback_packet = self.hooks.build_pli(self.ssrc, self.state.route.latched_ssrc)
        elif "ccm fir" in feedback:
            feedback_packet = self.hooks.build_fir(
                self.ssrc,
                self.state.route.latched_ssrc,
                self.state.counters["video_keyframe_requests"] + 1,
            )
        if feedback_packet is None:
            request = self.session.request_remote_keyframe
            if request is None or (
                self.keyframe_task is not None and not self.keyframe_task.done()
            ):
                return
            self.state.last_keyframe_feedback = now
            self.state.counters["video_keyframe_requests"] += 1

            async def request_intra_frame() -> bool:
                self.state.counters["video_sip_keyframe_requests"] += 1
                try:
                    accepted = await request()
                except asyncio.CancelledError:
                    raise
                except (ConnectionError, OSError, RuntimeError, ValueError) as err:
                    _LOGGER.info(
                        "SIP video keyframe request failed call_id=%s error=%s",
                        self.session.call_id,
                        err,
                    )
                    self.store_counters()
                    return False
                if accepted:
                    self.state.counters["video_sip_keyframe_accepted"] += 1
                self.store_counters()
                return accepted

            self.keyframe_task = asyncio.create_task(
                request_intra_frame(),
                name=f"voip-video-keyframe-{self.session.call_id}",
            )
            return
        if self.transport is None:
            return
        raw = self.hooks.build_receiver_compound(
            self.ssrc,
            self.state.route.latched_ssrc,
            cumulative_lost=(
                self.state.input_reorder.lost
                if self.session.requires_receive_transcoding
                else self.state.reorder.lost
            ),
            highest_sequence=self.state.highest_sequence,
            feedback=feedback_packet,
        )
        try:
            self.transport.sendto(raw, (self.state.route.remote_rtcp_host, self.state.route.remote_rtcp_port))
        except (OSError, RuntimeError, ValueError) as err:
            # Feedback is advisory. A closed/unreachable RTCP transport must
            # not tear down the video receive task or the audio SIP dialog.
            self.state.last_keyframe_feedback = now
            self.record_rtcp_send_error("keyframe feedback", err)
            return
        self.state.counters["video_rtcp_tx_packets"] += 1
        self.state.counters["video_rtcp_tx_bytes"] += len(raw)
        self.state.last_keyframe_feedback = now
        self.state.counters["video_keyframe_requests"] += 1

    async def rtcp_reports(self) -> None:
        while not self.closed.is_set():
            try:
                await asyncio.wait_for(self.closed.wait(), timeout=self.report_interval)
            except TimeoutError:
                pass
            if (
                self.closed.is_set()
                or self.session.remote_connection_held
                or self.transport is None
            ):
                continue
            report_kwargs = {
                "cumulative_lost": (
                    self.state.input_reorder.lost
                    if self.session.requires_receive_transcoding
                    else self.state.reorder.lost
                ),
                "highest_sequence": self.state.highest_sequence,
            }
            if self.state.counters["video_rtp_tx_packets"]:
                # A send-only source has no inbound SSRC to report on. RFC
                # 3550 still requires periodic SR/SDES, with RC=0 in that case.
                monotonic_now = self.loop.time()
                unix_now = time.time()
                unix_seconds = int(unix_now)
                report = self.hooks.build_sender_compound(
                    self.ssrc,
                    self.state.route.latched_ssrc,
                    ntp_seconds=unix_seconds + 2_208_988_800,
                    ntp_fraction=int((unix_now - unix_seconds) * (1 << 32)),
                    rtp_timestamp=self.outbound_clock.current(monotonic_now),
                    packet_count=self.state.counters["video_rtp_tx_packets"],
                    octet_count=self.state.counters["video_rtp_tx_payload_bytes"],
                    **report_kwargs,
                )
            elif self.state.route.latched_ssrc is not None:
                report = self.hooks.build_receiver_compound(
                    self.ssrc,
                    self.state.route.latched_ssrc,
                    **report_kwargs,
                )
            else:
                continue
            try:
                self.transport.sendto(report, (self.state.route.remote_rtcp_host, self.state.route.remote_rtcp_port))
            except (OSError, RuntimeError, ValueError) as err:
                self.record_rtcp_send_error("report", err)
                continue
            self.state.counters["video_rtcp_tx_packets"] += 1
            self.state.counters["video_rtcp_tx_bytes"] += len(report)

    async def rtcp_to_browser_feedback(self) -> None:
        """Forward valid remote PLI/FIR feedback to the browser encoder."""

        dequeued_in_burst = 0
        while not self.closed.is_set():
            data, addr = await self.rtcp_queue.get()
            dequeued_in_burst += 1
            if dequeued_in_burst >= self.receive_burst_packets:
                dequeued_in_burst = 0
                await asyncio.sleep(0)
            source = (str(addr[0]), int(addr[1]))
            allowed_hosts = {
                str(self.session.remote_rtp_host),
                str(self.session.remote_rtcp_host),
                str(self.session.signaling_host),
                str(self.state.route.remote_host),
                str(self.state.route.remote_rtcp_host),
            }
            if source[0] not in allowed_hosts:
                self.state.counters["video_rtcp_drop_addr"] += 1
                continue
            if self.state.route.latched_rtcp_source is not None and source[0] != self.state.route.latched_rtcp_source[0]:
                self.state.counters["video_rtcp_drop_addr"] += 1
                continue
            try:
                packets = self.hooks.parse_compound(data)
            except RtcpError:
                self.state.counters["video_rtcp_drop_error"] += 1
                continue

            feedback = ""
            invalid_target = False
            for packet in packets:
                if packet.packet_type != 206:
                    continue
                if packet.fmt == 1:
                    if len(packet.payload) != 8:
                        invalid_target = True
                        continue
                    _sender_ssrc, media_ssrc = struct.unpack_from("!II", packet.payload)
                    if media_ssrc != self.ssrc:
                        invalid_target = True
                        continue
                    self.state.counters["video_rtcp_pli_rx"] += 1
                    feedback = "pli"
                elif packet.fmt == 4:
                    if len(packet.payload) < 16 or (len(packet.payload) - 8) % 8:
                        invalid_target = True
                        continue
                    fir_targets = {
                        struct.unpack_from("!I", packet.payload, offset)[0]
                        for offset in range(8, len(packet.payload) - 7, 8)
                    }
                    if self.ssrc not in fir_targets:
                        invalid_target = True
                        continue
                    self.state.counters["video_rtcp_fir_rx"] += 1
                    feedback = "fir"
            if invalid_target and not feedback:
                self.state.counters["video_rtcp_drop_error"] += 1
                continue

            self.state.route.latched_rtcp_source = source
            self.state.route.remote_rtcp_host, self.state.route.remote_rtcp_port = source
            self.state.counters["video_rtcp_rx_packets"] += 1
            self.state.counters["video_rtcp_rx_bytes"] += len(data)
            now = self.loop.time()
            if feedback and now - self.state.last_browser_keyframe_feedback >= 0.2:
                async with self.ws_send_lock:
                    await self.ws.send_json(
                        {"type": "force_key_frame", "feedback": feedback}
                    )
                self.state.last_browser_keyframe_feedback = now
                self.state.counters["video_rtcp_keyframe_requests_to_browser"] += 1
                self.store_counters()
