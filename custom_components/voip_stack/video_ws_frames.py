"""Browser video frame admission, codec packetization and RTP output."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
import json
import logging
import math
import struct

from aiohttp import WSMsgType, web

from .core import rtp, sdp
from .core.video_rtp import (
    H264Depacketizer,
    H264RtpError,
    JpegDepacketizer,
    VideoAccessUnit,
    Vp8Depacketizer,
    jpeg_dimensions,
    packetize_annex_b,
    packetize_jpeg,
    packetize_vp8,
    split_annex_b,
)

_LOGGER = logging.getLogger(f"{__package__}.video_ws_view")
_VIDEO_ACCESS_UNIT = 1
_VIDEO_HEADER = struct.Struct("!BBI")
_MAX_BROWSER_ACCESS_UNIT_BYTES = 1024 * 1024
_MAX_BROWSER_RECEIVE_ACCESS_UNIT_BYTES = 2 * 1024 * 1024
_MAX_BROWSER_JPEG_DIMENSION = 1280
_MAX_BROWSER_JPEG_PIXELS = 1280 * 800
_VIDEO_RTP_SEND_BURST_PACKETS = 32
_VIDEO_RTP_RECEIVE_BURST_PACKETS = 32
_VIDEO_PACKETIZE_SEMAPHORE = asyncio.Semaphore(1)


class _VideoFrameCadence:
    """Reserve one frame slot or return its wait, without accumulating bursts."""

    __slots__ = ("period", "next_at", "last_at")

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.period = 0.0
        self.next_at: float | None = None
        self.last_at: float | None = None

    def delay(self, now: float, framerate: float | None) -> float:
        # This downstream meter needs one frame of arrival credit in addition
        # to the current frame: upstream selection and network delivery both
        # add jitter. It is a two-frame token bucket (R*t + 2), using only the
        # server clock. Long stalls cannot accumulate more than that credit.
        period = (
            1.0 / framerate
            if framerate is not None and math.isfinite(framerate) and framerate > 0
            else 0.0
        )
        if period != self.period or (self.last_at is not None and now < self.last_at):
            self.period = period
            self.next_at = None
        self.last_at = now
        if not period:
            return 0.0
        if self.next_at is None or now - self.next_at >= period:
            self.next_at = now
            return 0.0
        if now < self.next_at:
            return self.next_at - now
        self.next_at += period
        return 0.0


def _packetize_browser_access_unit(
    access_unit: bytes,
    *,
    encoding: str,
    payload_type: int,
    sequence: int,
    timestamp: int,
    ssrc: int,
) -> list[tuple[bytes, int]]:
    """Packetize and serialize one browser AU outside Home Assistant's loop."""

    if encoding == "H264":
        packets = packetize_annex_b(
            access_unit,
            payload_type=payload_type,
            sequence=sequence,
            timestamp=timestamp,
            ssrc=ssrc,
        )
    elif encoding == "VP8":
        packets = packetize_vp8(
            access_unit,
            payload_type=payload_type,
            sequence=sequence,
            timestamp=timestamp,
            ssrc=ssrc,
        )
    elif encoding == "JPEG":
        packets = packetize_jpeg(
            access_unit,
            payload_type=payload_type,
            sequence=sequence,
            timestamp=timestamp,
            ssrc=ssrc,
        )
    else:
        raise ValueError(f"browser TX does not support {encoding}")
    return [(rtp.build_packet(packet), len(packet.payload)) for packet in packets]


async def _async_packetize_browser_access_unit(
    access_unit: bytes,
    *,
    encoding: str,
    payload_type: int,
    sequence: int,
    timestamp: int,
    ssrc: int,
    should_continue: Callable[[], bool] | None = None,
) -> list[tuple[bytes, int]] | None:
    """Keep access-unit parsing and RTP serialization off the HA event loop."""

    # Packetizers are Python CPU work and therefore still contend for the GIL
    # when moved to a thread. Serialize them globally: one bounded AU cannot
    # monopolise HA, and stale queued frames are discarded before they ever
    # enter the executor.
    async with _VIDEO_PACKETIZE_SEMAPHORE:
        if should_continue is not None and not should_continue():
            return None
        result = await asyncio.to_thread(
            _packetize_browser_access_unit,
            access_unit,
            encoding=encoding,
            payload_type=payload_type,
            sequence=sequence,
            timestamp=timestamp,
            ssrc=ssrc,
        )
        if should_continue is not None and not should_continue():
            return None
        return result


async def _async_validate_browser_jpeg(access_unit: bytes) -> tuple[int, int]:
    """Validate JPEG geometry off-loop before FFmpeg may allocate a frame."""

    async with _VIDEO_PACKETIZE_SEMAPHORE:
        width, height = await asyncio.to_thread(jpeg_dimensions, access_unit)
    if (
        width > _MAX_BROWSER_JPEG_DIMENSION
        or height > _MAX_BROWSER_JPEG_DIMENSION
        or width * height > _MAX_BROWSER_JPEG_PIXELS
    ):
        raise ValueError("browser JPEG exceeds the normalization budget")
    return width, height


async def _send_packetized_datagrams(
    transport: asyncio.DatagramTransport,
    datagrams: list[tuple[bytes, int]],
    target: tuple[str, int],
    *,
    should_continue: Callable[[], bool],
    on_sent: Callable[[int, int], None],
) -> bool:
    """Send one AU in bounded bursts while call control retains loop fairness."""

    if not should_continue():
        return False
    for index, (raw, payload_bytes) in enumerate(datagrams):
        if index and index % _VIDEO_RTP_SEND_BURST_PACKETS == 0:
            await asyncio.sleep(0)
            if not should_continue():
                return False
        transport.sendto(raw, target)
        on_sent(len(raw), payload_bytes)
    return True


def _sdp_parameter_sets(video_format: sdp.RtpVideoFormat) -> list[bytes]:
    out: list[bytes] = []
    for value in str(video_format.sprop_parameter_sets or "").split(","):
        value = value.strip()
        if not value:
            continue
        try:
            decoded = base64.b64decode(value, validate=True)
        except (ValueError, TypeError):
            continue
        if decoded and len(decoded) <= 65536:
            out.append(decoded)
    return out


def _make_video_depacketizer(
    call_id: str,
    browser_format: sdp.RtpVideoFormat,
    registry: object,
    cached_parameter_sets: tuple[bytes, ...],
):
    if browser_format.encoding == "H264":
        parameter_sets = cached_parameter_sets
        if registry is not None:
            parameter_sets = (
                registry.artifact_for(call_id, "video_parameter_sets") or parameter_sets
            )
        # A current SDP sprop is authoritative over parameter sets learned
        # from the preceding RTP generation.
        return H264Depacketizer([*parameter_sets, *_sdp_parameter_sets(browser_format)])
    if browser_format.encoding == "VP8":
        return Vp8Depacketizer()
    if browser_format.encoding == "JPEG":
        return JpegDepacketizer(
            max_access_unit_bytes=_MAX_BROWSER_RECEIVE_ACCESS_UNIT_BYTES
        )
    return None


def decode_browser_video_packet(
    depacketizer,
    packet,
    browser_format,
    registry,
    call_id,
    now,
    recover_from_video_loss,
):
    """Feed an ordered packet and retain H.264 parameter sets before publishing pictures."""
    before = int(getattr(depacketizer, "dropped_access_units", 0))
    result = depacketizer.push(packet)
    if int(getattr(depacketizer, "dropped_access_units", 0)) > before:
        recover_from_video_loss(now)
    if result is None:
        return
    access_unit = (
        VideoAccessUnit(result.data, result.timestamp, result.key_frame, "H264")
        if browser_format.encoding == "H264"
        else result
    )
    if browser_format.encoding == "H264" and isinstance(depacketizer, H264Depacketizer):
        parameter_sets = depacketizer.parameter_sets
        if len(parameter_sets) == 2 and registry is not None:
            registry.cache_video_parameter_sets(
                call_id,
                parameter_sets,
            )
        # WebCodecs chunks require a coded picture. Preserve standalone
        # SPS/PPS in the depacketizer cache for the next IDR, but do not
        # submit them (or SEI/AUD-only units) as empty browser frames.
        if not any(1 <= (nal[0] & 0x1F) <= 5 for nal in split_annex_b(result.data)):
            return
    return access_unit


async def _video_access_units_to_ws(
    ws: web.WebSocketResponse,
    closed: asyncio.Event,
    access_units: asyncio.Queue[VideoAccessUnit],
    ws_send_lock: asyncio.Lock,
) -> None:
    while not closed.is_set():
        access_unit = await access_units.get()
        if access_unit.encoding == "JPEG":
            # Socket backpressure and the bounded queue already limit work.
            # Keep the newest independent image without imposing a global FPS.
            while True:
                try:
                    access_unit = access_units.get_nowait()
                except asyncio.QueueEmpty:
                    break
        flags = 1 if access_unit.key_frame else 0
        async with ws_send_lock:
            await ws.send_bytes(
                _VIDEO_HEADER.pack(_VIDEO_ACCESS_UNIT, flags, access_unit.timestamp)
                + access_unit.data
            )


class BrowserVideoSender:
    """Send encoded browser frames on the connection's existing RTP source and lock."""

    def __init__(self, source, transport, send_lock, target, loop):
        self.source = source
        self.transport = transport
        self.send_lock = send_lock
        self.target = target
        self.loop = loop
        self.sequence = int(source.sequence)
        self.ssrc = int(source.ssrc)
        self.last_regular_rtp_tx_at = 0.0
        self.jpeg_normalization_required = False

    async def _send_access_unit(
        self,
        access_unit,
        source_timestamp,
        send_format,
        *,
        media_current,
        jpeg_normalizer,
        counters,
        store_counters,
    ):
        if send_format.encoding == "JPEG" and self.jpeg_normalization_required:
            access_unit = await jpeg_normalizer.normalize(
                access_unit,
                media_current=media_current,
            )
            if access_unit is None:
                return
        while True:
            retry_with_normalized_jpeg = False
            async with self.send_lock:
                start_sequence = self.sequence
                packet_target = self.target()
                try:
                    datagrams = await _async_packetize_browser_access_unit(
                        access_unit,
                        encoding=send_format.encoding,
                        payload_type=send_format.payload_type,
                        sequence=start_sequence,
                        timestamp=self.source.clock.map_browser(
                            source_timestamp, self.loop.time()
                        ),
                        ssrc=self.ssrc,
                        should_continue=media_current,
                    )
                except ValueError:
                    if (
                        send_format.encoding != "JPEG"
                        or self.jpeg_normalization_required
                    ):
                        raise
                    retry_with_normalized_jpeg = True
                    datagrams = None
                if datagrams is not None:
                    sent_packets = 0

                    def record_sent(raw_bytes: int, payload_bytes: int) -> None:
                        nonlocal sent_packets
                        sent_packets += 1
                        counters["video_rtp_tx_packets"] += 1
                        counters["video_rtp_tx_bytes"] += raw_bytes
                        counters["video_rtp_tx_payload_bytes"] += payload_bytes

                    complete = False
                    try:
                        complete = await _send_packetized_datagrams(
                            self.transport,
                            datagrams,
                            packet_target,
                            should_continue=media_current,
                            on_sent=record_sent,
                        )
                    finally:
                        if sent_packets:
                            self.sequence = (start_sequence + sent_packets) & 0xFFFF
                            self.source.sequence = self.sequence
                            self.last_regular_rtp_tx_at = self.loop.time()
            if not retry_with_normalized_jpeg:
                break
            access_unit = await jpeg_normalizer.normalize(
                access_unit,
                media_current=media_current,
            )
            if access_unit is None:
                break
            self.jpeg_normalization_required = True
        if datagrams is None:
            return
        if not complete:
            if sent_packets:
                counters["video_drop_error"] += 1
            return
        counters["video_access_units_tx"] += 1
        if counters["video_access_units_tx"] % 30 == 0:
            store_counters()

    async def run(
        self,
        ws,
        session,
        *,
        closed,
        call_ended,
        counters,
        refresh_media_state,
        request_key_frame,
        store_counters,
        jpeg_normalizer,
    ):
        browser_tx_drop_logs = 0
        observed_generation = int(session.media_generation)
        received_in_burst = 0
        cadence = _VideoFrameCadence()
        async for msg in ws:
            received_in_burst += 1
            if received_in_burst >= _VIDEO_RTP_RECEIVE_BURST_PACKETS:
                received_in_burst = 0
                await asyncio.sleep(0)
            if observed_generation != session.media_generation:
                observed_generation = int(session.media_generation)
                cadence.reset()
                if not await refresh_media_state(observed_generation):
                    return
            if msg.type == WSMsgType.BINARY:
                if session.remote_connection_held:
                    counters["video_drop_connection_hold"] += 1
                    continue
                if not session.can_send:
                    counters["video_drop_direction"] += 1
                    continue
                data = bytes(msg.data)
                if (
                    len(data) <= _VIDEO_HEADER.size
                    or len(data) > _MAX_BROWSER_ACCESS_UNIT_BYTES + _VIDEO_HEADER.size
                ):
                    counters["video_drop_error"] += 1
                    continue
                frame_type, _flags, source_timestamp = _VIDEO_HEADER.unpack_from(data)
                if frame_type != _VIDEO_ACCESS_UNIT:
                    counters["video_drop_error"] += 1
                    continue
                try:
                    packet_generation = observed_generation
                    send_format = session.send_video_format

                    def media_current(generation: int = packet_generation) -> bool:
                        return bool(
                            not closed.is_set()
                            and not call_ended.is_set()
                            and not session.removed
                            and generation == session.media_generation
                            and session.can_send
                            and not session.remote_connection_held
                        )

                    max_framerate = send_format.max_framerate
                    wait_seconds = cadence.delay(self.loop.time(), max_framerate)
                    if wait_seconds > 0:
                        counters["video_rate_limited_access_units"] += 1
                        if send_format.encoding == "JPEG":
                            # Independent pictures may be discarded before
                            # normalization; client timestamps grant no credit.
                            continue
                        # H.264/VP8 pictures retain their dependency chain.
                        # Backpressure waits for admission instead of dropping
                        # an arbitrary already-encoded delta frame.
                        while wait_seconds > 0:
                            try:
                                await asyncio.wait_for(
                                    closed.wait(), timeout=wait_seconds
                                )
                                return
                            except TimeoutError:
                                pass
                            if not media_current():
                                break
                            wait_seconds = cadence.delay(
                                self.loop.time(), max_framerate
                            )
                        if not media_current():
                            continue

                    await self._send_access_unit(
                        data[_VIDEO_HEADER.size :],
                        source_timestamp,
                        send_format,
                        media_current=media_current,
                        jpeg_normalizer=jpeg_normalizer,
                        counters=counters,
                        store_counters=store_counters,
                    )
                except (H264RtpError, OSError, RuntimeError, ValueError) as err:
                    counters["video_drop_error"] += 1
                    browser_tx_drop_logs += 1
                    if browser_tx_drop_logs & (browser_tx_drop_logs - 1) == 0:
                        _LOGGER.debug(
                            "HA softphone browser video TX drops=%s latest=%s",
                            browser_tx_drop_logs,
                            err,
                        )
            elif msg.type == WSMsgType.TEXT:
                try:
                    control = json.loads(msg.data) if len(str(msg.data)) <= 256 else {}
                except (TypeError, ValueError):
                    control = {}
                if control.get("type") == "request_key_frame":
                    counters["video_browser_keyframe_requests"] += 1
                    request_key_frame(self.loop.time())
                elif control.get("type") == "tx_epoch":
                    async with self.send_lock:
                        self.source.clock.reset_browser()
            elif msg.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                break
