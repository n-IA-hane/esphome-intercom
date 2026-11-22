"""RTP admission, reordering and access-unit processing below the WS owner."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

from .core import rtp, sdp
from .core.video_rtp import VideoAccessUnit, split_annex_b
from .video_receive_state import VideoDepacketizer, VideoReceiveState
from .video_ws_io import _ByteBudgetQueue

if TYPE_CHECKING:
    from .pbx_runtime import SipEndpointRuntime
    from .video_transcoder import FfmpegVideoTranscoder
    from .video_ws_view import _VideoMediaSession

_LOGGER = logging.getLogger(f"{__package__}.video_ws_view")

@dataclass(frozen=True, slots=True)
class VideoReceiveHooks:
    """Explicit codec seams; they do not own sessions, sockets or workers."""

    decode_packet: Callable[[VideoDepacketizer, rtp.RtpPacket, sdp.RtpVideoFormat,
        SipEndpointRuntime | None, str, float, Callable[[float], None]], VideoAccessUnit | None]
    loss_requires_key_frame: Callable[..., bool]
    sync_reorder: Callable[..., None]


@dataclass(slots=True)
class VideoReceiveWorkers:
    """Process queues borrowed from the existing browser media owner."""

    session: _VideoMediaSession
    state: VideoReceiveState
    queue: _ByteBudgetQueue
    media_queue: _ByteBudgetQueue
    access_units: _ByteBudgetQueue
    closed: asyncio.Event
    registry: SipEndpointRuntime | None
    transcoder: FfmpegVideoTranscoder | None
    refresh_media_state: Callable[[int], Awaitable[bool]]
    request_key_frame: Callable[[float], None]
    store_counters: Callable[..., None]
    hooks: VideoReceiveHooks
    max_access_unit_bytes: int
    receive_burst_packets: int

    def queue_access_unit(self, access_unit: VideoAccessUnit, now: float) -> None:
        if len(access_unit.data) > self.max_access_unit_bytes:
            self.state.counters["video_access_unit_queue_drops"] += 1
            self.state.counters["video_drop_error"] += 1
            self.state.needs_key_frame = self.state.browser_format.encoding != "JPEG"
            self.request_key_frame(now)
            return
        if self.state.browser_format.encoding == "JPEG":
            # Every RTP/JPEG frame is independently decodable. Retain only the
            # newest complete image instead of allowing a slow browser to
            # accumulate stale full-frame work.
            dropped = 0
            while True:
                try:
                    self.access_units.get_nowait()
                    dropped += 1
                except asyncio.QueueEmpty:
                    break
            self.state.counters["video_access_unit_queue_drops"] += dropped
            self.state.counters["video_drop_error"] += dropped
        if self.access_units.full() or not self.access_units.can_fit(access_unit):
            dropped = 0
            while True:
                try:
                    self.access_units.get_nowait()
                    dropped += 1
                except asyncio.QueueEmpty:
                    break
            self.state.counters["video_access_unit_queue_drops"] += dropped
            self.state.counters["video_drop_error"] += dropped
            if not access_unit.key_frame:
                # VP8 decoders can conceal loss from later complete frames.
                # H264 and decoded/transcoded input still require a clean IDR.
                self.state.counters["video_access_unit_queue_drops"] += 1
                self.state.counters["video_drop_error"] += 1
                self.recover_from_video_loss(now)
                return
            self.state.needs_key_frame = False
        if not self.access_units.can_fit(access_unit):
            self.state.counters["video_access_unit_queue_drops"] += 1
            self.state.counters["video_drop_error"] += 1
            self.recover_from_video_loss(now)
            return
        self.access_units.put_nowait(access_unit)
        self.state.counters["video_access_unit_queue_max"] = max(
            self.state.counters["video_access_unit_queue_max"], self.access_units.qsize()
        )

    def recover_from_video_loss(self, now: float) -> None:
        """Request recovery while preserving VP8 decoder error concealment."""

        self.state.needs_key_frame = self.state.needs_key_frame or bool(
            self.hooks.loss_requires_key_frame(
                self.state.browser_format.encoding,
                receive_transcoding=self.session.requires_receive_transcoding,
            )
        )
        self.request_key_frame(now)

    def consume_ordered(self, packet: rtp.RtpPacket, now: float) -> None:
        if not self.session.requires_receive_transcoding:
            self.state.highest_sequence = self.state.extended_sequence.observe(packet.sequence)
        if self.state.depacketizer is None:
            self.state.counters["video_drop_error"] += 1
            return
        access_unit = self.hooks.decode_packet(
            self.state.depacketizer, packet, self.state.browser_format, self.registry, self.session.call_id, now, self.recover_from_video_loss
        )
        if access_unit is None:
            return
        if self.state.needs_key_frame and not access_unit.key_frame:
            self.state.counters["video_drop_error"] += 1
            self.state.counters["video_drop_wait_keyframe"] += 1
            if self.state.counters["video_drop_wait_keyframe"] == 1:
                _LOGGER.info(
                    "HA softphone video waiting for key frame call_id=%s "
                    "codec=%s payload_type=%s key_frame=%s nal_types=%s",
                    self.session.call_id,
                    self.state.browser_format.encoding,
                    packet.payload_type,
                    access_unit.key_frame,
                    tuple(nal[0] & 0x1F for nal in split_annex_b(access_unit.data))
                    if self.state.browser_format.encoding == "H264" else (),
                )
            self.request_key_frame(now)
            return
        if access_unit.key_frame:
            self.state.needs_key_frame = False
        self.queue_access_unit(access_unit, now)
        self.state.counters["video_access_units_rx"] += 1
        if self.state.counters["video_access_units_rx"] % 30 == 0:
            self.store_counters()

    def forward_ordered_to_transcoder(self, item: tuple[bytes, rtp.RtpPacket]) -> None:
        if self.transcoder is None:
            return
        raw, packet = item
        self.state.highest_sequence = self.state.extended_sequence.observe(packet.sequence)
        self.transcoder.send_rtp(raw)

    async def rtp_to_transcoder(self) -> None:
        assert self.transcoder is not None
        local_loop = asyncio.get_running_loop()
        observed_generation = int(self.session.media_generation)
        dequeued_in_burst = 0
        drop_logs = 0
        while not self.closed.is_set():
            if observed_generation != self.session.media_generation:
                observed_generation = int(self.session.media_generation)
                if not await self.refresh_media_state(observed_generation):
                    return
            deadline = self.state.input_reorder.next_deadline
            timeout = (
                None if deadline is None else max(0.0, deadline - local_loop.time())
            )
            try:
                data, addr = await asyncio.wait_for(self.queue.get(), timeout=timeout)
            except TimeoutError:
                lost_before = self.state.input_reorder.lost
                for ordered in self.state.input_reorder.flush(local_loop.time()):
                    self.forward_ordered_to_transcoder(ordered)
                if self.state.input_reorder.lost > lost_before:
                    self.recover_from_video_loss(local_loop.time())
                self.hooks.sync_reorder(self.state.counters, self.session, self.state.reorder, self.state.input_reorder)
                continue
            dequeued_in_burst += 1
            if dequeued_in_burst >= self.receive_burst_packets:
                dequeued_in_burst = 0
                await asyncio.sleep(0)
            if observed_generation != self.session.media_generation:
                observed_generation = int(self.session.media_generation)
                if not await self.refresh_media_state(observed_generation):
                    return
            try:
                if str(addr[0]) not in {
                    self.session.remote_rtp_host,
                    self.session.signaling_host,
                }:
                    self.state.counters["video_drop_addr"] += 1
                    continue
                packet = rtp.parse_packet(data)
                if packet.payload_type != self.session.recv_video_format.payload_type:
                    self.state.counters["video_drop_payload_type"] += 1
                    continue
                source = (str(addr[0]), int(addr[1]))
                if self.state.route.latched_ssrc is not None and packet.ssrc != self.state.route.latched_ssrc:
                    self.state.counters["video_drop_addr"] += 1
                    continue
                if self.state.route.latched_source is None:
                    self.state.route.latched_source = source
                    self.state.route.latched_ssrc = packet.ssrc
                    self.state.route.remote_host, self.state.route.remote_port = source
                    if self.state.route.latched_rtcp_source is None and not self.state.route.remote_rtcp_host_explicit:
                        self.state.route.remote_rtcp_host = source[0]
                        self.state.route.remote_rtcp_port = source[1] + self.state.route.remote_rtcp_offset
                    self.request_key_frame(local_loop.time())
                elif source[0] != self.state.route.latched_source[0]:
                    self.state.counters["video_drop_addr"] += 1
                    continue
                elif source[1] != self.state.route.latched_source[1]:
                    self.state.route.latched_source = source
                    self.state.route.remote_port = source[1]
                    if self.state.route.latched_rtcp_source is None and not self.state.route.remote_rtcp_host_explicit:
                        self.state.route.remote_rtcp_port = source[1] + self.state.route.remote_rtcp_offset
                self.state.counters["video_rtp_rx_packets"] += 1
                self.state.counters["video_rtp_rx_bytes"] += len(data)
                lost_before = self.state.input_reorder.lost
                for ordered in self.state.input_reorder.push(
                    packet.sequence, (data, packet), local_loop.time()
                ):
                    self.forward_ordered_to_transcoder(ordered)
                if self.state.input_reorder.lost > lost_before:
                    self.recover_from_video_loss(local_loop.time())
                self.hooks.sync_reorder(self.state.counters, self.session, self.state.reorder, self.state.input_reorder)
            except (OSError, RuntimeError, ValueError) as err:
                self.state.counters["video_drop_error"] += 1
                drop_logs += 1
                if drop_logs & (drop_logs - 1) == 0:
                    _LOGGER.debug(
                        "HA softphone video transcode input drops=%s latest=%s",
                        drop_logs,
                        err,
                    )

    async def rtp_to_access_units(self) -> None:
        loop = asyncio.get_running_loop()
        observed_generation = int(self.session.media_generation)
        dequeued_in_burst = 0
        drop_logs = 0
        while not self.closed.is_set():
            if observed_generation != self.session.media_generation:
                observed_generation = int(self.session.media_generation)
                if not await self.refresh_media_state(observed_generation):
                    return
            deadline = self.state.reorder.next_deadline
            timeout = None if deadline is None else max(0.0, deadline - loop.time())
            try:
                data, addr = await asyncio.wait_for(self.media_queue.get(), timeout=timeout)
            except TimeoutError:
                lost_before = self.state.reorder.lost
                for ordered in self.state.reorder.flush(loop.time()):
                    self.consume_ordered(ordered, loop.time())
                if self.state.reorder.lost > lost_before:
                    self.recover_from_video_loss(loop.time())
                self.hooks.sync_reorder(self.state.counters, self.session, self.state.reorder, self.state.input_reorder)
                continue
            dequeued_in_burst += 1
            if dequeued_in_burst >= self.receive_burst_packets:
                dequeued_in_burst = 0
                await asyncio.sleep(0)
            if observed_generation != self.session.media_generation:
                observed_generation = int(self.session.media_generation)
                if not await self.refresh_media_state(observed_generation):
                    return
            if not self.session.can_receive:
                self.state.counters["video_drop_direction"] += 1
                continue
            try:
                packet = rtp.parse_packet(data)
                if packet.payload_type != self.state.browser_format.payload_type:
                    self.state.counters["video_drop_payload_type"] += 1
                    continue
                if self.session.requires_receive_transcoding:
                    lost_before = self.state.reorder.lost
                    for ordered in self.state.reorder.push(packet.sequence, packet, loop.time()):
                        self.consume_ordered(ordered, loop.time())
                    if self.state.reorder.lost > lost_before:
                        self.recover_from_video_loss(loop.time())
                    continue
                if str(addr[0]) not in {
                    self.session.remote_rtp_host,
                    self.session.signaling_host,
                }:
                    self.state.counters["video_drop_addr"] += 1
                    continue
                source = (str(addr[0]), int(addr[1]))
                if self.state.route.latched_ssrc is not None and packet.ssrc != self.state.route.latched_ssrc:
                    self.state.counters["video_drop_addr"] += 1
                    continue
                if self.state.route.latched_source is None:
                    self.state.route.latched_source = source
                    self.state.route.latched_ssrc = packet.ssrc
                    # RFC 3264 does not require an RTP packet's source tuple
                    # to equal the c=/m= destination advertised in SDP.  Latch
                    # the first valid negotiated payload/SSRC and send media
                    # back to that symmetric RTP tuple, which is essential for
                    # ordinary SIP phones behind NAT.
                    self.state.route.remote_host = source[0]
                    self.state.route.remote_port = source[1]
                    if self.state.route.latched_rtcp_source is None and not self.state.route.remote_rtcp_host_explicit:
                        self.state.route.remote_rtcp_host = source[0]
                        self.state.route.remote_rtcp_port = source[1] + self.state.route.remote_rtcp_offset
                    self.request_key_frame(loop.time())
                elif source[0] != self.state.route.latched_source[0]:
                    self.state.counters["video_drop_addr"] += 1
                    continue
                elif source[1] != self.state.route.latched_source[1]:
                    self.state.route.latched_source = source
                    self.state.route.remote_port = source[1]
                    if self.state.route.latched_rtcp_source is None and not self.state.route.remote_rtcp_host_explicit:
                        self.state.route.remote_rtcp_port = source[1] + self.state.route.remote_rtcp_offset
                self.state.counters["video_rtp_rx_packets"] += 1
                self.state.counters["video_rtp_rx_bytes"] += len(data)
                lost_before = self.state.reorder.lost
                for ordered in self.state.reorder.push(packet.sequence, packet, loop.time()):
                    self.consume_ordered(ordered, loop.time())
                if self.state.reorder.lost > lost_before:
                    self.recover_from_video_loss(loop.time())
                self.hooks.sync_reorder(self.state.counters, self.session, self.state.reorder, self.state.input_reorder)
            except Exception as err:  # noqa: BLE001 - a bad frame must not stop audio/call control.
                self.state.counters["video_drop_error"] += 1
                drop_logs += 1
                if drop_logs & (drop_logs - 1) == 0:
                    _LOGGER.debug(
                        "HA softphone video RTP RX drops=%s latest=%s",
                        drop_logs,
                        err,
                    )
