"""Browser audio WebSocket for the HA SIP softphone media leg."""

from __future__ import annotations

import asyncio
from collections import deque
import contextlib
from dataclasses import dataclass, field
import json
import logging
import math
from pathlib import Path
import threading
from typing import Any, Callable
import wave

from aiohttp import WSMsgType, web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .core import rtp
from .audio_ws import AUDIO_FRAME_HEADER_BYTES, AUDIO_FRAME_VERSION, decode_audio_frame, encode_audio_frame
from .config import debug_mode, media_capture_enabled
from .core.audio_format import HA_SIP_PCM_FORMATS
from .debug_capture import (
    DEBUG_CAPTURE_DIR,
    DEBUG_CAPTURE_MAX_PENDING_WRITES,
    capture_temp_path,
    capture_session_name,
    commit_capture_file,
    debug_capture_transaction,
    prune_debug_captures,
    release_debug_capture_write,
    try_reserve_debug_capture_write,
    wav_pcm_payload,
)
from .dtmf import (
    RtpDtmfDecoder,
    build_telephone_event_payload,
    send_rtp_dtmf_event,
    telephone_event_code,
)
from .media_debug import merge_media_debug
from .media_call_lifetime import (
    active_media_call,
    listen_for_media_call_end as _listen_for_call_end,
)
from .queue_utils import drain_queue, put_drop_oldest
from .runtime_data import conference_component, registration_data, require_runtime_data
from .session_cleanup import async_wait_for_cleanup
from .sip_client import RtpPayloadDecoder, RtpPayloadEncoder
from .media_ws_session import (
    async_claimed_media_websocket,
    async_prepare_media_websocket_request,
)
from .websocket_api import (
    _ha_softphone_store,
    _publish_ha_softphone_state,
)
from .websocket_owner import (
    WebSocketOwnerBusyError,
)

_LOGGER = logging.getLogger(__name__)
_DEBUG_CAPTURE_SECONDS = 15
_DEBUG_TIMING_MAX_SAMPLES = 4096
_AUDIO_OWNER_HANDOFF_TIMEOUT = 5.0
# The largest supported browser PCM frame is stereo 48 kHz/s16le/20 ms
# (3,840 bytes) plus the versioned frame header. Keep a small fixed ceiling so
# aiohttp never buffers multi-megabyte payloads on this real-time endpoint.
_MAX_BROWSER_AUDIO_MESSAGE_BYTES = 4096 + AUDIO_FRAME_HEADER_BYTES
_BROWSER_PLAYOUT_MAX_MS = 200


def _playout_target_frames(frame_ms: int) -> int:
    """Start after three complete source frames, bounded by capacity."""

    value = max(1, int(frame_ms))
    max_frames = max(1, _BROWSER_PLAYOUT_MAX_MS // value)
    return min(3, max_frames)


def _timestamp_delta(value: int, reference: int) -> int:
    """Return a signed 32-bit media timestamp delta."""

    return ((int(value) - int(reference) + 0x80000000) & 0xFFFFFFFF) - 0x80000000


def _rtp_to_pcm_timestamp(
    value: int,
    origin: int,
    *,
    pcm_rate: int,
    rtp_rate: int,
) -> int:
    """Map an RTP clock onto the decoded PCM sample timeline."""

    if pcm_rate <= 0 or rtp_rate <= 0:
        raise ValueError("media clock rates must be positive")
    return round(_timestamp_delta(value, origin) * pcm_rate / rtp_rate) & 0xFFFFFFFF


async def _pace_playout(
    loop: asyncio.AbstractEventLoop,
    next_deadline: float,
    frame_ms: int,
) -> float:
    """Advance one media tick without accumulating event-loop drift."""

    frame_delay = max(1, int(frame_ms)) / 1000
    next_deadline += frame_delay
    now = loop.time()
    if now - next_deadline > frame_delay:
        next_deadline = now
    await asyncio.sleep(max(0.0, next_deadline - now))
    return next_deadline


def _conceal_pcm_frame(last_pcm: bytes, frame_bytes: int) -> bytes:
    """Return one click-free s16le PLC frame which decays to silence."""
    if frame_bytes < 2 or not last_pcm:
        return bytes(frame_bytes)
    last_sample = int.from_bytes(last_pcm[-2:], "little", signed=True)
    samples = frame_bytes // 2
    output = bytearray(frame_bytes)
    for index in range(samples):
        sample = round(last_sample * (samples - index - 1) / samples)
        output[index * 2 : index * 2 + 2] = int(sample).to_bytes(
            2, "little", signed=True
        )
    return bytes(output)


def _fade_in_pcm_frame(pcm: bytes, fade_samples: int) -> bytes:
    """Fade in recovered s16le PCM so PLC recovery cannot click."""
    samples = min(len(pcm) // 2, max(0, fade_samples))
    if samples == 0:
        return pcm
    output = bytearray(pcm)
    for index in range(samples):
        offset = index * 2
        sample = int.from_bytes(pcm[offset : offset + 2], "little", signed=True)
        sample = round(sample * (index + 1) / samples)
        output[offset : offset + 2] = int(sample).to_bytes(2, "little", signed=True)
    return bytes(output)


@dataclass(slots=True)
class _SoftphoneMediaSession:
    call_id: str
    local_rtp_port: int
    remote_rtp_host: str
    remote_rtp_port: int
    send_format: Any
    recv_format: Any
    signaling_host: str = ""
    local_audio_direction: str = "sendrecv"
    remote_audio_connection_held: bool = False
    dtmf_payload_type: int | None = None
    dtmf_events: frozenset[int] = frozenset()
    send_dtmf_payload_type: int | None = None
    send_dtmf_clock_rate: int = 8000
    send_dtmf_events: frozenset[int] = frozenset()
    send_dtmf_info: Callable[[str], Any] | None = None
    send_dtmf: Callable[[str, int], bool] | None = None
    on_dtmf: Callable[[str], None] | None = None
    conference_room: str = ""
    conference_queue: asyncio.Queue[bytes] | None = None
    local_ssrc: int = 0
    rtp_source: rtp.AudioRtpSenderState | None = None
    media_generation: int = 0
    update_event: asyncio.Event = field(default_factory=asyncio.Event)


def _rx_queue_frame_limit(frame_ms: int) -> int:
    """Bound packets while browser playback is attaching."""

    value = max(1, int(frame_ms))
    return max(4, (_BROWSER_PLAYOUT_MAX_MS + value - 1) // value)


class _RtpAudioProtocol(asyncio.DatagramProtocol):
    def __init__(
        self,
        queue: asyncio.Queue[tuple[bytes, tuple[str, int]]],
        *,
        frame_ms: int,
    ) -> None:
        self.queue = queue
        self.frame_limit = _rx_queue_frame_limit(frame_ms)
        self.dropped_packets = 0

    def datagram_received(self, data: bytes, addr) -> None:
        while self.queue.qsize() >= self.frame_limit:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self.dropped_packets += 1
        if put_drop_oldest(self.queue, (data, addr)):
            self.dropped_packets += 1

    def reconfigure_frame_ms(self, frame_ms: int) -> None:
        """Apply a new duration bound without swapping a queue waiter."""

        self.frame_limit = _rx_queue_frame_limit(frame_ms)
        while self.queue.qsize() > self.frame_limit:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self.dropped_packets += 1


class _DebugAudioCapture:
    def __init__(self, call_id: str, *, rx_format: Any, tx_format: Any) -> None:
        self.call_id = str(call_id or "call")
        self.capture_name = capture_session_name(self.call_id)
        self.rx_format = rx_format.audio_format
        self.tx_format = tx_format.audio_format
        self.rtp_to_ws = bytearray()
        self.ws_to_rtp = bytearray()
        self.rtp_to_ws_deltas_ms: list[float] = []
        self.ws_rx_deltas_ms: list[float] = []
        self.ws_send_deltas_ms: list[float] = []
        self.rtp_tx_deltas_ms: list[float] = []
        self._last_rtp_rx: float | None = None
        self._last_ws_rx: float | None = None
        self._last_ws_send: float | None = None
        self._last_rtp_tx: float | None = None

    def note_rtp_rx(self, now: float, pcm: bytes) -> None:
        self._append_delta(self.rtp_to_ws_deltas_ms, "_last_rtp_rx", now)
        self._append_pcm(self.rtp_to_ws, pcm, self.rx_format)

    def note_ws_rx(self, now: float, pcm: bytes) -> None:
        self._append_delta(self.ws_rx_deltas_ms, "_last_ws_rx", now)
        self._append_pcm(self.ws_to_rtp, pcm, self.tx_format)

    def note_ws_send(self, now: float) -> None:
        self._append_delta(self.ws_send_deltas_ms, "_last_ws_send", now)

    def note_rtp_tx(self, now: float) -> None:
        self._append_delta(self.rtp_tx_deltas_ms, "_last_rtp_tx", now)

    def _append_delta(self, target: list[float], attr: str, now: float) -> None:
        previous = getattr(self, attr)
        if previous is not None and len(target) < _DEBUG_TIMING_MAX_SAMPLES:
            target.append((now - previous) * 1000.0)
        setattr(self, attr, now)

    @staticmethod
    def _append_pcm(target: bytearray, pcm: bytes, fmt: Any) -> None:
        max_bytes = (
            int(fmt.sample_rate)
            * int(fmt.channels)
            * int(fmt.container_bytes_per_sample)
            * _DEBUG_CAPTURE_SECONDS
        )
        remaining = max(0, max_bytes - len(target))
        if remaining:
            target.extend(pcm[:remaining])

    def write(self, counters: dict[str, int]) -> None:
        safe_name = self.capture_name
        rx_path = DEBUG_CAPTURE_DIR / f"{safe_name}_ha_ws_rtp_to_browser.wav"
        tx_path = DEBUG_CAPTURE_DIR / f"{safe_name}_ha_ws_browser_to_rtp.wav"
        meta_path = DEBUG_CAPTURE_DIR / f"{safe_name}_ha_ws_timing.json"
        rx_payload = bytes(self.rtp_to_ws)
        tx_payload = bytes(self.ws_to_rtp)
        meta = {
            "call_id": self.call_id,
            "rx_format": self.rx_format.wire_token(),
            "tx_format": self.tx_format.wire_token(),
            "rtp_to_browser_wav": str(rx_path),
            "browser_to_rtp_wav": str(tx_path),
            "counters": dict(counters),
            "rtp_to_ws_deltas_ms": self.rtp_to_ws_deltas_ms,
            "ws_rx_deltas_ms": self.ws_rx_deltas_ms,
            "ws_send_deltas_ms": self.ws_send_deltas_ms,
            "rtp_tx_deltas_ms": self.rtp_tx_deltas_ms,
        }
        with debug_capture_transaction():
            rx_temp = capture_temp_path(rx_path)
            tx_temp = capture_temp_path(tx_path)
            meta_temp = capture_temp_path(meta_path)
            published: list[Path] = []
            try:
                self._write_wav(rx_temp, self.rx_format, rx_payload)
                self._write_wav(tx_temp, self.tx_format, tx_payload)
                meta_temp.write_text(
                    json.dumps(meta, indent=2, sort_keys=True),
                    encoding="utf-8",
                )
                commit_capture_file(rx_temp, rx_path)
                published.append(rx_path)
                commit_capture_file(tx_temp, tx_path)
                published.append(tx_path)
                commit_capture_file(meta_temp, meta_path)
                published.append(meta_path)
                prune_debug_captures()
            except BaseException:
                # Session names are unique, so these paths cannot refer to a
                # previous capture. Roll back a partially published group: a
                # timing manifest without both WAV legs (or vice versa) is a
                # misleading diagnostic artifact.
                for destination in published:
                    with contextlib.suppress(OSError):
                        destination.unlink()
                raise
            finally:
                for temporary in (rx_temp, tx_temp, meta_temp):
                    with contextlib.suppress(OSError):
                        temporary.unlink()
        _LOGGER.info(
            "HA softphone audio debug capture wrote call_id=%s rtp_to_browser=%s bytes=%d "
            "browser_to_rtp=%s bytes=%d timing=%s",
            self.call_id,
            rx_path,
            len(rx_payload),
            tx_path,
            len(tx_payload),
            meta_path,
        )

    def _write_wav(self, path: Path, fmt: Any, payload: bytes | bytearray) -> None:
        sample_width, wav_payload = wav_pcm_payload(fmt, payload)
        with wave.open(str(path), "wb") as wav_file:
            wav_file.setnchannels(int(fmt.channels))
            wav_file.setsampwidth(int(sample_width))
            wav_file.setframerate(int(fmt.sample_rate))
            wav_file.writeframes(wav_payload)


def _schedule_debug_capture_write(
    hass: HomeAssistant,
    capture: _DebugAudioCapture,
    counters: dict[str, int],
) -> None:
    """Persist diagnostics without delaying WebSocket ownership handoff."""

    media = require_runtime_data(hass).media
    tasks = media.debug_capture_tasks
    tasks.difference_update(task for task in tasks if task.done())
    if len(tasks) >= DEBUG_CAPTURE_MAX_PENDING_WRITES:
        media.debug_capture_dropped_writes += 1
        _LOGGER.warning(
            "HA softphone debug capture queue full; dropping capture call_id=%s pending=%d",
            capture.call_id,
            len(tasks),
        )
        return
    if not try_reserve_debug_capture_write():
        media.debug_capture_dropped_writes += 1
        _LOGGER.warning(
            "VoIP debug capture writer pool full; dropping audio capture call_id=%s",
            capture.call_id,
        )
        return
    frozen_counters = dict(counters)
    state_lock = threading.Lock()
    state = {"phase": "pending"}

    def write_reserved_capture() -> None:
        with state_lock:
            if state["phase"] != "pending":
                return
            state["phase"] = "running"
        try:
            capture.write(frozen_counters)
        finally:
            with state_lock:
                state["phase"] = "released"
            release_debug_capture_write()

    # Home Assistant returns an asyncio Future for executor work, not a
    # coroutine. ``ensure_future`` accepts either shape and preserves the
    # Future as the unload barrier instead of raising TypeError after the
    # worker has already been scheduled.
    try:
        task = asyncio.ensure_future(
            hass.async_add_executor_job(write_reserved_capture)
        )
    except BaseException:
        with state_lock:
            state["phase"] = "released"
        release_debug_capture_write()
        raise
    tasks.add(task)

    def done(completed: asyncio.Future[Any]) -> None:
        tasks.discard(completed)
        release_pending = False
        with state_lock:
            if state["phase"] == "pending":
                state["phase"] = "released"
                release_pending = True
        if release_pending:
            release_debug_capture_write()
        if completed.cancelled():
            return
        try:
            error = completed.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            _LOGGER.error(
                "HA softphone audio debug capture write failed call_id=%s: %s",
                capture.call_id,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )

    task.add_done_callback(done)


class VoipAudioWebSocketView(HomeAssistantView):
    """Expose browser audio to the current HA softphone SIP/RTP dialog."""

    url = "/api/voip_stack/ws"
    name = "api:voip_stack:ws"
    requires_auth = True

    async def get(self, request: web.Request) -> web.WebSocketResponse:
        if str(request.query.get("audio_protocol") or "") != str(AUDIO_FRAME_VERSION):
            raise web.HTTPBadRequest(
                text="Browser audio protocol mismatch, reload the Home Assistant frontend"
            )
        prepared = await async_prepare_media_websocket_request(
            request,
            active_session_resolver=_active_softphone_media_session,
            missing_dialog_text="HA softphone has no matching SIP/RTP dialog",
        )
        context = prepared.context
        hass: HomeAssistant = context.hass
        endpoint_id = context.endpoint_id
        local_bridge = context.local_bridge

        try:
            async with async_claimed_media_websocket(
                request,
                context,
                prepared.registry,
                channel="audio",
                max_msg_size=_MAX_BROWSER_AUDIO_MESSAGE_BYTES,
                timeout=_AUDIO_OWNER_HANDOFF_TIMEOUT,
                local_call=prepared.local_call,
                publish_state=lambda: _publish_ha_softphone_state(
                    hass, endpoint_id=endpoint_id
                ),
            ) as claimed:
                ws = claimed.websocket
                owner = claimed.owner
                media_owner = claimed.media_session
                local_call, session = prepared.resolve_current()
                if local_call is not None:
                    lease = prepared.acquire_local_lease(media_owner)
                    await ws.prepare(request)
                    await _run_local_audio_session(
                        hass,
                        ws,
                        local_bridge,
                        lease,
                    )
                else:
                    assert session is not None
                    await ws.prepare(request)
                    await _run_audio_session(
                        hass,
                        ws,
                        session,
                        request.transport,
                        handoff_requested=owner.handoff_requested,
                        endpoint_id=endpoint_id,
                    )
        except WebSocketOwnerBusyError as err:
            raise web.HTTPConflict(text="HA softphone media is already attached") from err
        return claimed.websocket


def async_register_audio_ws_view(hass: HomeAssistant) -> None:
    registration = registration_data(hass)
    if registration.audio_view:
        return
    hass.http.register_view(VoipAudioWebSocketView)
    registration.audio_view = True
    _LOGGER.info("HA softphone browser audio websocket ready on %s", VoipAudioWebSocketView.url)


def _active_softphone_media_session(
    hass: HomeAssistant,
    endpoint_id: str,
) -> _SoftphoneMediaSession | None:
    active = active_media_call(hass, endpoint_id)
    if active is None:
        return None
    call_id = active.call_id
    registry = active.registry
    item = registry.resource_for(call_id, "softphone_media") if call_id else None

    def _dtmf_callback(side: str) -> Callable[[str], None]:
        def _emit(digit: str) -> None:
            active = registry.sessions.get(registry.resolve_session_id(call_id))
            if active is None:
                return
            from .dtmf_events import publish_dtmf_event

            publish_dtmf_event(
                hass,
                call_id=call_id,
                dest_call_id="",
                caller=active.caller,
                callee=active.callee,
                side=side,
                digit=digit,
                transport="rtp_event",
            )

        return _emit

    if item is not None:
        if item.get("rtp_loopback"):
            rtp_source = item.get("audio_rtp_source")
            if not isinstance(rtp_source, rtp.AudioRtpSenderState):
                rtp_source = rtp.AudioRtpSenderState.create(
                    ssrc=int(item.get("local_ssrc") or 0)
                )
                item["audio_rtp_source"] = rtp_source
            return _SoftphoneMediaSession(
                call_id=call_id,
                local_rtp_port=0,
                remote_rtp_host=str(item["remote_rtp_host"]),
                remote_rtp_port=int(item["remote_rtp_port"]),
                send_format=item["send_format"],
                recv_format=item["recv_format"],
                local_ssrc=int(item.get("local_ssrc") or 0),
                rtp_source=rtp_source,
            )
        conference_room = str(item.get("conference_room") or "")
        conference_queue = item.get("conference_queue")
        if conference_room and conference_queue is not None:
            from .conference import CONFERENCE_RTP_FORMAT

            return _SoftphoneMediaSession(
                call_id=call_id,
                local_rtp_port=0,
                remote_rtp_host="",
                remote_rtp_port=0,
                send_format=CONFERENCE_RTP_FORMAT,
                recv_format=CONFERENCE_RTP_FORMAT,
                conference_room=conference_room,
                conference_queue=conference_queue,
            )
        invite = item.get("invite")
        local_rtp_port = int(item.get("local_rtp_port") or 0)
        if invite is not None and local_rtp_port:
            rtp_source = item.get("audio_rtp_source")
            if not isinstance(rtp_source, rtp.AudioRtpSenderState):
                rtp_source = rtp.AudioRtpSenderState.create()
                item["audio_rtp_source"] = rtp_source
            from .core.sdp import offered_dtmf_formats

            dtmf_formats = offered_dtmf_formats(invite.remote_sdp)
            dtmf_format = dtmf_formats[0] if dtmf_formats else None
            return _SoftphoneMediaSession(
                call_id=invite.call_id,
                local_rtp_port=local_rtp_port,
                remote_rtp_host=invite.remote_rtp_host,
                remote_rtp_port=int(invite.remote_rtp_port),
                send_format=invite.send_format,
                recv_format=invite.recv_format,
                signaling_host=invite.source_host,
                local_audio_direction=str(invite.local_audio_direction),
                remote_audio_connection_held=bool(
                    invite.remote_audio_connection_held
                ),
                dtmf_payload_type=(
                    dtmf_format.payload_type if dtmf_format is not None else None
                ),
                dtmf_events=(
                    dtmf_format.events if dtmf_format is not None else frozenset()
                ),
                send_dtmf_payload_type=(
                    dtmf_format.payload_type if dtmf_format is not None else None
                ),
                send_dtmf_clock_rate=(
                    dtmf_format.sample_rate if dtmf_format is not None else 8000
                ),
                send_dtmf_events=(
                    dtmf_format.events if dtmf_format is not None else frozenset()
                ),
                on_dtmf=_dtmf_callback("left"),
                rtp_source=rtp_source,
            )

    client = registry.sip_client_for(call_id) if call_id else None
    if client is not None:
        if client.dialog is not None:
            dialog = client.dialog
            rtp_source = getattr(client, "audio_rtp_source", None)
            if not isinstance(rtp_source, rtp.AudioRtpSenderState):
                rtp_source = rtp.AudioRtpSenderState.create()
                client.audio_rtp_source = rtp_source
            return _SoftphoneMediaSession(
                call_id=dialog.call_id,
                local_rtp_port=int(dialog.local_rtp_port),
                remote_rtp_host=dialog.remote_rtp_host,
                remote_rtp_port=int(dialog.remote_rtp_port),
                send_format=dialog.send_format,
                recv_format=dialog.recv_format,
                signaling_host=dialog.remote_host,
                local_audio_direction=str(dialog.local_audio_direction),
                remote_audio_connection_held=bool(
                    dialog.remote_audio_connection_held
                ),
                dtmf_payload_type=dialog.dtmf_payload_type,
                dtmf_events=dialog.dtmf_events,
                send_dtmf_payload_type=dialog.send_dtmf_payload_type,
                send_dtmf_clock_rate=int(dialog.send_dtmf_clock_rate or 8000),
                send_dtmf_events=dialog.send_dtmf_events or frozenset(),
                send_dtmf_info=client.send_dtmf_info,
                on_dtmf=_dtmf_callback("right"),
                rtp_source=rtp_source,
            )
    return None


async def _run_local_audio_session(
    hass: HomeAssistant,
    ws: web.WebSocketResponse,
    bridge,
    lease,
) -> None:
    """Relay browser PCM between two logical phones without an RTP hop."""
    audio_format = HA_SIP_PCM_FORMATS[0]
    expected_bytes = int(audio_format.nominal_frame_bytes)
    counters = {
        "ws_rx": 0,
        "ws_tx": 0,
        "drop_payload_size": 0,
        "drop_tx_queue": 0,
        "tx_error": 0,
    }
    media_generation = 1
    send_sequence = 0
    send_timestamp = 0
    await ws.send_json(
        {
            "state": "in_call",
            "call_id": lease.call_id,
            "tx_format": audio_format.wire_token(),
            "rx_format": audio_format.wire_token(),
            "selected_tx_format": audio_format.wire_token(),
            "selected_rx_format": audio_format.wire_token(),
            "selected_tx_rtp_format": "local/websocket",
            "selected_rx_rtp_format": "local/websocket",
            "audio_direction": "sendrecv",
            "remote_connection_held": False,
            "media_transport": "local_websocket",
            "audio_protocol": AUDIO_FRAME_VERSION,
            "media_generation": media_generation,
        }
    )

    async def peer_to_browser() -> None:
        nonlocal send_sequence, send_timestamp
        while True:
            pcm = await bridge.receive_audio(
                lease.call_id,
                lease.endpoint_id,
                lease.token,
            )
            await ws.send_bytes(
                encode_audio_frame(
                    pcm,
                    generation=media_generation,
                    sequence=send_sequence,
                    timestamp=send_timestamp,
                )
            )
            send_sequence = rtp.next_sequence(send_sequence)
            send_timestamp = rtp.next_timestamp(
                send_timestamp, audio_format.nominal_frame_samples
            )
            counters["ws_tx"] += 1

    async def browser_to_peer() -> None:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                try:
                    frame = decode_audio_frame(bytes(msg.data))
                    if frame.generation != media_generation:
                        counters["tx_error"] += 1
                        continue
                    pcm = frame.payload
                    if len(pcm) != expected_bytes:
                        counters["drop_payload_size"] += 1
                        continue
                    if bridge.send_audio(
                        lease.call_id,
                        lease.endpoint_id,
                        lease.token,
                        pcm,
                    ):
                        counters["drop_tx_queue"] += 1
                    counters["ws_rx"] += 1
                except Exception:  # noqa: BLE001 - isolate malformed media.
                    counters["tx_error"] += 1
                    _LOGGER.debug(
                        "Local softphone browser audio frame rejected call_id=%s endpoint=%s",
                        lease.call_id,
                        lease.endpoint_id,
                        exc_info=True,
                    )
            elif msg.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                return

    peer_task = asyncio.create_task(peer_to_browser())
    browser_task = asyncio.create_task(browser_to_peer())
    lifetime_task = asyncio.create_task(bridge.wait_closed(lease.call_id))
    tasks = (peer_task, browser_task, lifetime_task)
    try:
        done, _pending = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if lifetime_task in done:
            ws.force_close()
        for task in done:
            if task is lifetime_task or task.cancelled():
                continue
            error = task.exception()
            if error is not None:
                _LOGGER.debug(
                    "Local softphone audio session ended call_id=%s endpoint=%s: %s",
                    lease.call_id,
                    lease.endpoint_id,
                    error,
                )
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        store = _ha_softphone_store(hass, lease.endpoint_id)
        if str(store.get("call_id") or store.get("last_terminal_call_id") or "") == lease.call_id:
            store.update(counters)
            store["last_sip_event"] = "local_audio_detached"
            _publish_ha_softphone_state(hass, endpoint_id=lease.endpoint_id)


async def _run_audio_session(
    hass: HomeAssistant,
    ws: web.WebSocketResponse,
    session: _SoftphoneMediaSession,
    websocket_transport: asyncio.BaseTransport | None = None,
    *,
    handoff_requested: asyncio.Event | None = None,
    endpoint_id: str,
) -> None:
    if session.conference_queue is not None:
        await _run_conference_audio_session(
            hass,
            ws,
            session,
            handoff_requested=handoff_requested,
            endpoint_id=endpoint_id,
        )
        return
    frame_ms = max(1, int(session.recv_format.audio_format.frame_ms))
    queue: asyncio.Queue[tuple[bytes, tuple[str, int]]] = asyncio.Queue(
        maxsize=_rx_queue_frame_limit(frame_ms)
    )
    protocol = _RtpAudioProtocol(queue, frame_ms=frame_ms)
    loop = asyncio.get_running_loop()
    try:
        transport, _ = await loop.create_datagram_endpoint(
            lambda: protocol,
            local_addr=("0.0.0.0", int(session.local_rtp_port)),
        )
    except OSError as err:
        _LOGGER.warning(
            "HA softphone audio websocket rejected call_id=%s local_rtp=%s: %s",
            session.call_id,
            session.local_rtp_port,
            err,
        )
        await ws.close(code=1013, message=b"RTP port already in use")
        return
    rtp_source = session.rtp_source
    if rtp_source is None:
        rtp_source = rtp.AudioRtpSenderState.create(ssrc=session.local_ssrc)
        session.rtp_source = rtp_source
    sequence = int(rtp_source.sequence)
    timestamp = int(rtp_source.timestamp)
    ssrc = int(rtp_source.ssrc)
    closed = asyncio.Event()
    counters = {
        "ws_rx": 0,
        "ws_tx": 0,
        "rtp_rx": 0,
        "rtp_tx": 0,
        "rtp_rx_bytes": 0,
        "rtp_tx_bytes": 0,
        "drop_addr": 0,
        "drop_payload_type": 0,
        "drop_payload_size": 0,
        "drop_error": 0,
        "drop_rx_queue": 0,
        "drop_tx_queue": 0,
        "tx_error": 0,
        "tx_silence_keepalive": 0,
        "tx_playout_depth": 0,
        "tx_playout_peak": 0,
        "tx_playout_late_discard": 0,
        "tx_playout_plc": 0,
        "tx_playout_rebuffer": 0,
        "rx_playout_depth": 0,
        "rx_playout_peak": 0,
        "rx_playout_late_discard": 0,
        "rx_playout_plc": 0,
        "rx_playout_rebuffer": 0,
        "rx_rtp_max_gap_ms": 0,
        "rx_rtp_gaps_over_40ms": 0,
        "rx_rtp_arrival_span_ms": 0,
        "rx_rtp_media_span_ms": 0,
        "rx_rtp_clock_drift_ms": 0,
        "drop_direction": 0,
        "drop_connection_hold": 0,
        "dtmf_rx_events": 0,
        "tx_ws_max_gap_ms": 0,
        "tx_ws_gaps_over_40ms": 0,
        "tx_playout_target_frames": 0,
        "tx_playout_target_ms": 0,
    }
    logged_first_rtp = False
    latched_rtp_source: tuple[str, int] | None = None
    latched_rtp_ssrc: int | None = None
    remote_rtp_host = str(session.remote_rtp_host)
    remote_rtp_port = int(session.remote_rtp_port)
    applied_media_generation = int(session.media_generation)
    browser_rtp_timestamp_origin: int | None = None
    browser_pcm_timestamp_origin = 0
    last_browser_audio_at = 0.0
    last_rtp_audio_at = 0.0
    first_rtp_audio_at = 0.0
    last_counter_event = 0.0
    ws_send_lock = asyncio.Lock()
    media_state_lock = asyncio.Lock()
    rtp_send_lock = asyncio.Lock()
    dtmf_send_lock = asyncio.Lock()
    dtmf_tasks: set[asyncio.Task[None]] = set()
    browser_playback_ready = asyncio.Event()
    browser_preroll: deque[bytes] = deque()
    browser_preroll_max_frames = max(
        2,
        math.ceil(80 / max(1, int(session.recv_format.audio_format.frame_ms))),
    )
    debug_capture = (
        _DebugAudioCapture(session.call_id, rx_format=session.recv_format, tx_format=session.send_format)
        if media_capture_enabled(hass)
        else None
    )
    rtp_decoder: RtpPayloadDecoder
    rtp_encoder: RtpPayloadEncoder
    dtmf_decoder = (
        RtpDtmfDecoder(session.dtmf_payload_type)
        if session.dtmf_payload_type is not None
        else None
    )

    def publish_counters(*, force: bool = False) -> None:
        nonlocal last_counter_event
        now = loop.time()
        debug_enabled = debug_mode(hass)
        publish_interval = 0.5 if debug_enabled else 5.0
        if not force and now - last_counter_event < publish_interval:
            return
        last_counter_event = now
        store = _ha_softphone_store(hass, endpoint_id)
        current_call_id = str(store.get("call_id") or "")
        if current_call_id:
            if current_call_id != session.call_id:
                return
        elif str(store.get("last_terminal_call_id") or "") != session.call_id:
            return
        counters["drop_rx_queue"] = protocol.dropped_packets
        update = {
            "rtp_tx_packets": counters["rtp_tx"],
            "rtp_rx_packets": counters["rtp_rx"],
            "rtp_tx_bytes": counters["rtp_tx_bytes"],
            "rtp_rx_bytes": counters["rtp_rx_bytes"],
        }
        # Final media counters may arrive after BYE/hangup published the
        # terminal snapshot. Preserve that authoritative signaling event.
        if current_call_id:
            update["last_sip_event"] = "rtp_media"
        if debug_enabled:
            merge_media_debug(
                store,
                call_id=session.call_id,
                channel="audio",
                values={
                    "local_rtp_port": session.local_rtp_port,
                    "remote_rtp_host": remote_rtp_host,
                    "remote_rtp_port": remote_rtp_port,
                    "tx_format": session.send_format.audio_format.wire_token(),
                    "rx_format": session.recv_format.audio_format.wire_token(),
                    "tx_rtp_format": session.send_format.wire_token(),
                    "rx_rtp_format": session.recv_format.wire_token(),
                    "audio_direction": session.local_audio_direction,
                    "remote_connection_held": session.remote_audio_connection_held,
                    "expected_browser_tx_frame_bytes": session.send_format.audio_format.nominal_frame_bytes,
                    "expected_browser_rx_frame_bytes": session.recv_format.audio_format.nominal_frame_bytes,
                    **counters,
                },
            )
        store.update(update)
        # Media telemetry belongs to the high-frequency softphone snapshot.
        # Re-emitting the SIP lifecycle event here would turn every counter
        # tick into another synthetic ``answered`` automation occurrence.
        _publish_ha_softphone_state(hass, endpoint_id=endpoint_id)

    def negotiation_payload(*, message_type: str = "") -> dict[str, Any]:
        payload: dict[str, Any] = {
            "state": "in_call",
            "call_id": session.call_id,
            "tx_format": session.send_format.audio_format.wire_token(),
            "rx_format": session.recv_format.audio_format.wire_token(),
            "selected_tx_format": session.send_format.audio_format.wire_token(),
            "selected_rx_format": session.recv_format.audio_format.wire_token(),
            "selected_tx_rtp_format": session.send_format.wire_token(),
            "selected_rx_rtp_format": session.recv_format.wire_token(),
            "audio_direction": session.local_audio_direction,
            "remote_connection_held": session.remote_audio_connection_held,
            "audio_protocol": AUDIO_FRAME_VERSION,
            "media_generation": int(session.media_generation),
        }
        if message_type:
            payload["type"] = message_type
        return payload

    async def refresh_media_state(generation: int) -> None:
        nonlocal applied_media_generation, remote_rtp_host, remote_rtp_port
        nonlocal latched_rtp_source, latched_rtp_ssrc, logged_first_rtp
        nonlocal rtp_decoder, rtp_encoder
        nonlocal dtmf_decoder
        nonlocal debug_capture
        nonlocal browser_preroll_max_frames
        nonlocal browser_rtp_timestamp_origin, browser_pcm_timestamp_origin
        if generation == applied_media_generation:
            return
        async with media_state_lock:
            if generation == applied_media_generation:
                return
            # Build the complete codec/timing generation before publishing any
            # part of it.  A re-INVITE may change PT, codec, rate or ptime; using
            # the previous encoder with the new RTP metadata would put invalid
            # media on the wire.
            next_decoder = RtpPayloadDecoder(session.recv_format)
            next_encoder = RtpPayloadEncoder(session.send_format)
            next_dtmf_decoder = (
                RtpDtmfDecoder(session.dtmf_payload_type)
                if session.dtmf_payload_type is not None
                else None
            )
            protocol.reconfigure_frame_ms(
                int(session.recv_format.audio_format.frame_ms)
            )
            remote_rtp_host = str(session.remote_rtp_host)
            remote_rtp_port = int(session.remote_rtp_port)
            latched_rtp_source = None
            latched_rtp_ssrc = None
            logged_first_rtp = False
            protocol.dropped_packets += drain_queue(queue)
            browser_preroll.clear()
            browser_rtp_timestamp_origin = None
            browser_pcm_timestamp_origin = 0
            browser_preroll_max_frames = max(
                2,
                math.ceil(
                    80 / max(1, int(session.recv_format.audio_format.frame_ms))
                ),
            )
            counters["rx_playout_rebuffer"] += 1
            rtp_decoder = next_decoder
            rtp_encoder = next_encoder
            dtmf_decoder = next_dtmf_decoder
            if debug_capture is not None:
                _schedule_debug_capture_write(hass, debug_capture, counters)
                debug_capture = _DebugAudioCapture(
                    session.call_id,
                    rx_format=session.recv_format,
                    tx_format=session.send_format,
                )
            applied_media_generation = generation

    try:
        await ws.send_json(negotiation_payload())
        rtp_decoder = RtpPayloadDecoder(session.recv_format)
        rtp_encoder = RtpPayloadEncoder(session.send_format)
    except asyncio.CancelledError:
        transport.close()
        raise
    except (ConnectionError, RuntimeError):
        transport.close()
        return
    except BaseException:
        transport.close()
        raise
    _LOGGER.info(
        "HA softphone audio websocket attached call_id=%s local_rtp=%s remote=%s:%s tx=%s (%s) rx=%s (%s)",
        session.call_id,
        session.local_rtp_port,
        session.remote_rtp_host,
        session.remote_rtp_port,
        session.send_format.audio_format.wire_token(),
        session.send_format.wire_token(),
        session.recv_format.audio_format.wire_token(),
        session.recv_format.wire_token(),
    )

    async def rtp_to_ws() -> None:
        nonlocal latched_rtp_source, latched_rtp_ssrc, logged_first_rtp
        nonlocal remote_rtp_host, remote_rtp_port
        nonlocal last_rtp_audio_at
        nonlocal first_rtp_audio_at
        nonlocal browser_rtp_timestamp_origin, browser_pcm_timestamp_origin
        observed_generation = int(session.media_generation)
        while not closed.is_set():
            if observed_generation != session.media_generation:
                observed_generation = int(session.media_generation)
                await refresh_media_state(observed_generation)
            data, addr = await queue.get()
            if closed.is_set():
                break
            if observed_generation != session.media_generation:
                observed_generation = int(session.media_generation)
                await refresh_media_state(observed_generation)
            if str(addr[0]) not in {session.remote_rtp_host, session.signaling_host}:
                counters["drop_addr"] += 1
                continue
            try:
                packet = rtp.parse_packet(data)
                if (
                    dtmf_decoder is not None
                    and packet.payload_type == dtmf_decoder.payload_type
                ):
                    digit = dtmf_decoder.decode(data)
                    event = telephone_event_code(digit)
                    if (
                        digit
                        and event is not None
                        and event in session.dtmf_events
                        and session.on_dtmf is not None
                    ):
                        session.on_dtmf(digit)
                        counters["dtmf_rx_events"] += 1
                    continue
                if not logged_first_rtp:
                    _LOGGER.info(
                        "HA softphone RTP RX first packet call_id=%s from=%s:%s payload_type=%s expected=%s bytes=%d",
                        session.call_id,
                        addr[0],
                        addr[1],
                        packet.payload_type,
                        session.recv_format.payload_type,
                        len(data),
                    )
                    logged_first_rtp = True
                if packet.payload_type != session.recv_format.payload_type:
                    counters["drop_payload_type"] += 1
                    continue
                if session.local_audio_direction not in {"recvonly", "sendrecv"}:
                    counters["drop_direction"] += 1
                    continue
                if latched_rtp_ssrc is not None and packet.ssrc != latched_rtp_ssrc:
                    counters["drop_addr"] += 1
                    continue
                try:
                    rtp.validate_audio_payload_size(
                        packet.payload,
                        session.recv_format,
                    )
                except rtp.RtpError as err:
                    counters["drop_payload_size"] += 1
                    _LOGGER.debug("HA softphone RTP RX oversized audio drop: %s", err)
                    continue
                pcm = rtp_decoder.decode(packet.payload)
                if not pcm:
                    continue
                source = (str(addr[0]), int(addr[1]))
                if latched_rtp_source is None:
                    latched_rtp_source = source
                    latched_rtp_ssrc = packet.ssrc
                    remote_rtp_host = source[0]
                    remote_rtp_port = source[1]
                elif source[0] != latched_rtp_source[0]:
                    counters["drop_addr"] += 1
                    continue
                elif source[1] != latched_rtp_source[1]:
                    # Preserve the SSRC latch while allowing a NAT mapping to
                    # change its source port during a long-lived call.
                    latched_rtp_source = source
                    remote_rtp_port = source[1]
                counters["rtp_rx"] += 1
                counters["rtp_rx_bytes"] += len(data)
                now = loop.time()
                if first_rtp_audio_at == 0:
                    first_rtp_audio_at = now
                if last_rtp_audio_at > 0:
                    gap_ms = (now - last_rtp_audio_at) * 1000
                    counters["rx_rtp_max_gap_ms"] = max(
                        counters["rx_rtp_max_gap_ms"], round(gap_ms, 1)
                    )
                    if gap_ms >= 40:
                        counters["rx_rtp_gaps_over_40ms"] += 1
                last_rtp_audio_at = now
                arrival_span_ms = max(0.0, (now - first_rtp_audio_at) * 1000)
                media_span_ms = max(0, counters["rtp_rx"] - 1) * max(
                    1, int(session.recv_format.audio_format.frame_ms)
                )
                counters["rx_rtp_arrival_span_ms"] = round(arrival_span_ms, 1)
                counters["rx_rtp_media_span_ms"] = media_span_ms
                counters["rx_rtp_clock_drift_ms"] = round(
                    arrival_span_ms - media_span_ms, 1
                )
                if debug_capture is not None:
                    debug_capture.note_rtp_rx(loop.time(), pcm)
                if browser_rtp_timestamp_origin is None:
                    browser_rtp_timestamp_origin = packet.timestamp
                    browser_pcm_timestamp_origin = 0
                browser_timestamp = (
                    browser_pcm_timestamp_origin
                    + _rtp_to_pcm_timestamp(
                        packet.timestamp,
                        browser_rtp_timestamp_origin,
                        pcm_rate=session.recv_format.audio_format.sample_rate,
                        rtp_rate=session.recv_format.rtp_clock_rate,
                    )
                ) & 0xFFFFFFFF
                encoded = encode_audio_frame(
                    pcm,
                    generation=int(session.media_generation),
                    sequence=packet.sequence,
                    timestamp=browser_timestamp,
                )
                if not browser_playback_ready.is_set():
                    if len(browser_preroll) >= browser_preroll_max_frames:
                        browser_preroll.popleft()
                        counters["rx_playout_late_discard"] += 1
                    browser_preroll.append(encoded)
                    counters["rx_playout_depth"] = len(browser_preroll)
                    counters["rx_playout_peak"] = max(
                        counters["rx_playout_peak"], len(browser_preroll)
                    )
                    publish_counters()
                    continue
                pending_frames = tuple(browser_preroll)
                browser_preroll.clear()
                async with ws_send_lock:
                    for pending in (*pending_frames, encoded):
                        await ws.send_bytes(pending)
                        if debug_capture is not None:
                            debug_capture.note_ws_send(loop.time())
                        counters["ws_tx"] += 1
                counters["rx_playout_depth"] = 0
                counters["rx_playout_peak"] = max(
                    counters["rx_playout_peak"], len(pending_frames) + 1
                )
                publish_counters()
            except (ConnectionError, RuntimeError):
                # A dead browser transport ends this media owner. Treating it
                # as malformed RTP would leave a zombie UDP session spinning.
                raise
            except Exception as err:  # noqa: BLE001 - media path must stay alive on bad packets.
                counters["drop_error"] += 1
                _LOGGER.debug("HA softphone RTP RX drop: %s", err)

    rx_task = asyncio.create_task(rtp_to_ws())
    call_ended, remove_call_listener = _listen_for_call_end(
        hass, session.call_id, endpoint_id
    )
    active_sessions = require_runtime_data(hass).media.sessions_for("audio")
    active_sessions[session.call_id] = session

    async def session_updates_to_ws() -> None:
        """Apply a committed re-INVITE and notify the attached browser."""

        observed_generation = int(session.media_generation)
        while not closed.is_set():
            await session.update_event.wait()
            session.update_event.clear()
            generation = int(session.media_generation)
            if generation == observed_generation:
                continue
            await refresh_media_state(generation)
            observed_generation = generation
            async with ws_send_lock:
                await ws.send_json(negotiation_payload(message_type="media_update"))

    async def browser_to_rtp() -> None:
        nonlocal sequence, timestamp, remote_rtp_host, remote_rtp_port
        nonlocal last_browser_audio_at
        observed_generation = int(session.media_generation)
        tx_frames = deque()
        tx_ready = asyncio.Event()
        frame_ms = int(session.send_format.audio_format.frame_ms)
        target_frames = _playout_target_frames(frame_ms)
        max_frames = max(target_frames, _BROWSER_PLAYOUT_MAX_MS // frame_ms)
        counters["tx_playout_target_frames"] = target_frames
        counters["tx_playout_target_ms"] = target_frames * frame_ms
        last_pcm = b""
        plc_active = False
        silence_pcm = bytes(int(session.send_format.audio_format.nominal_frame_bytes))
        expected_browser_timestamp: int | None = None

        async def send_dtmf(digit: str, duration_ms: int) -> None:
            """Send one browser digit using RFC 4733, then SIP INFO fallback."""

            nonlocal sequence
            event = telephone_event_code(digit)
            if (
                event is None
                or session.send_dtmf_payload_type is None
                or event not in session.send_dtmf_events
            ):
                fallback = session.send_dtmf_info
                if fallback is not None:
                    result = fallback(digit)
                    if asyncio.iscoroutine(result):
                        await result
                return
            async with dtmf_send_lock:
                event_rate = max(1, int(session.send_dtmf_clock_rate))
                event_timestamp = timestamp

                async def emit(duration: int, marker: bool, end: bool) -> bool:
                    nonlocal sequence
                    async with rtp_send_lock:
                        packet = rtp.build_packet(
                            rtp.RtpPacket(
                                payload_type=int(session.send_dtmf_payload_type),
                                sequence=sequence,
                                timestamp=event_timestamp,
                                ssrc=ssrc,
                                payload=build_telephone_event_payload(
                                    digit,
                                    duration=duration,
                                    end=end,
                                ),
                                marker=marker,
                            )
                        )
                        transport.sendto(packet, (remote_rtp_host, remote_rtp_port))
                        sequence = rtp.next_sequence(sequence)
                        rtp_source.sequence = sequence
                    return True

                await send_rtp_dtmf_event(
                    digit,
                    clock_rate=event_rate,
                    duration_ms=duration_ms,
                    emit=emit,
                )

        def request_dtmf(digit: str, duration_ms: int = 160) -> bool:
            value = str(digit or "").strip().upper()
            if telephone_event_code(value) is None or closed.is_set():
                return False
            task = asyncio.create_task(
                send_dtmf(value, duration_ms),
                name=f"voip-browser-dtmf-{session.call_id}-{value}",
            )
            dtmf_tasks.add(task)
            task.add_done_callback(dtmf_tasks.discard)
            return True

        session.send_dtmf = request_dtmf

        async def playout() -> None:
            nonlocal sequence, timestamp, last_pcm, plc_active
            nonlocal expected_browser_timestamp
            started = False
            next_deadline = loop.time()
            while not closed.is_set():
                if not started:
                    while len(tx_frames) < target_frames and not closed.is_set():
                        tx_ready.clear()
                        await tx_ready.wait()
                    if closed.is_set():
                        return
                    started = True
                    expected_browser_timestamp = tx_frames[0].timestamp
                    next_deadline = loop.time()

                while tx_frames and expected_browser_timestamp is not None:
                    if _timestamp_delta(
                        tx_frames[0].timestamp, expected_browser_timestamp
                    ) >= 0:
                        break
                    tx_frames.popleft()
                    counters["tx_playout_late_discard"] += 1

                current_frame = None
                if tx_frames and expected_browser_timestamp is not None:
                    if tx_frames[0].timestamp == expected_browser_timestamp:
                        current_frame = tx_frames.popleft()

                if current_frame is not None:
                    pcm = current_frame.payload
                    if plc_active:
                        pcm = _fade_in_pcm_frame(
                            pcm,
                            int(session.send_format.audio_format.sample_rate) * 2 // 1000,
                        )
                    last_pcm = pcm
                    plc_active = False
                else:
                    pcm = (
                        silence_pcm
                        if plc_active
                        else _conceal_pcm_frame(last_pcm, len(silence_pcm))
                    )
                    last_pcm = pcm
                    plc_active = True
                    counters["tx_playout_plc"] += 1

                if not (
                    session.remote_audio_connection_held
                    or session.local_audio_direction not in {"sendonly", "sendrecv"}
                ):
                    payload = rtp_encoder.encode(pcm)
                    if payload:
                        async with rtp_send_lock:
                            packet = rtp.build_packet(
                                rtp.RtpPacket(
                                    payload_type=session.send_format.payload_type,
                                    sequence=sequence,
                                    timestamp=timestamp,
                                    ssrc=ssrc,
                                    payload=payload,
                                )
                            )
                            transport.sendto(packet, (remote_rtp_host, remote_rtp_port))
                            sequence = rtp.next_sequence(sequence)
                            rtp_source.sequence = sequence
                        if debug_capture is not None:
                            debug_capture.note_rtp_tx(loop.time())
                        counters["rtp_tx"] += 1
                        counters["rtp_tx_bytes"] += len(packet)
                else:
                    counter = (
                        "drop_connection_hold"
                        if session.remote_audio_connection_held
                        else "drop_direction"
                    )
                    counters[counter] += 1
                timestamp = rtp.next_timestamp(
                    timestamp, session.send_format.rtp_timestamp_step
                )
                if expected_browser_timestamp is not None:
                    expected_browser_timestamp = rtp.next_timestamp(
                        expected_browser_timestamp,
                        int(session.send_format.audio_format.nominal_frame_samples),
                    )
                rtp_source.timestamp = timestamp
                counters["tx_playout_depth"] = len(tx_frames)
                publish_counters()
                next_deadline = await _pace_playout(loop, next_deadline, frame_ms)

        playout_task = asyncio.create_task(playout())
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        control = json.loads(str(msg.data))
                        if control.get("type") == "playback_ready":
                            browser_playback_ready.set()
                            continue
                        if control.get("type") == "playback_timing":
                            _LOGGER.warning(
                                "HA softphone browser playback underrun call_id=%s "
                                "count=%d buffered=%d target=%d ws_gap=%.1fms "
                                "worklet_arrival_gap=%.1fms worklet_delivery_gap=%.1fms "
                                "clock=%dppm",
                                session.call_id,
                                max(0, int(control.get("underruns") or 0)),
                                max(0, int(control.get("buffered_frames") or 0)),
                                max(0, int(control.get("jitter_target_frames") or 0)),
                                max(0.0, float(control.get("max_ws_arrival_gap_ms") or 0.0)),
                                max(0.0, float(control.get("max_worklet_arrival_gap_ms") or 0.0)),
                                max(0.0, float(control.get("max_worklet_delivery_gap_ms") or 0.0)),
                                int(control.get("clock_recovery_ppm") or 0),
                            )
                            continue
                        if control.get("type") != "dtmf":
                            continue
                        digit = str(control.get("digit") or "").strip().upper()
                        if telephone_event_code(digit) is None:
                            raise ValueError("unsupported DTMF digit")
                        if not request_dtmf(
                            digit,
                            int(control.get("duration_ms") or 160),
                        ):
                            raise ValueError("DTMF media owner is unavailable")
                    except (TypeError, ValueError, json.JSONDecodeError) as err:
                        _LOGGER.debug("HA softphone DTMF control rejected: %s", err)
                    continue
                if msg.type != WSMsgType.BINARY:
                    if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                        break
                    continue
                try:
                    if observed_generation != session.media_generation:
                        observed_generation = int(session.media_generation)
                        await refresh_media_state(observed_generation)
                        tx_frames.clear()
                        expected_browser_timestamp = None
                        frame_ms = int(session.send_format.audio_format.frame_ms)
                        target_frames = _playout_target_frames(frame_ms)
                        max_frames = max(
                            target_frames, _BROWSER_PLAYOUT_MAX_MS // frame_ms
                        )
                        counters["tx_playout_target_frames"] = target_frames
                        counters["tx_playout_target_ms"] = target_frames * frame_ms
                        silence_pcm = bytes(
                            int(session.send_format.audio_format.nominal_frame_bytes)
                        )
                        counters["tx_playout_rebuffer"] += 1
                    counters["ws_rx"] += 1
                    now = loop.time()
                    if last_browser_audio_at > 0:
                        gap_ms = (now - last_browser_audio_at) * 1000
                        counters["tx_ws_max_gap_ms"] = max(
                            counters["tx_ws_max_gap_ms"], round(gap_ms, 1)
                        )
                        if gap_ms >= 40:
                            counters["tx_ws_gaps_over_40ms"] += 1
                    last_browser_audio_at = now
                    frame = decode_audio_frame(bytes(msg.data))
                    if frame.generation != observed_generation:
                        counters["tx_playout_late_discard"] += 1
                        continue
                    pcm = frame.payload
                    expected = int(session.send_format.audio_format.nominal_frame_bytes)
                    if len(pcm) != expected:
                        raise ValueError(f"browser PCM frame has {len(pcm)} bytes, expected {expected}")
                    if debug_capture is not None:
                        debug_capture.note_ws_rx(loop.time(), pcm)
                    if expected_browser_timestamp is not None and _timestamp_delta(
                        frame.timestamp, expected_browser_timestamp
                    ) < 0:
                        counters["tx_playout_late_discard"] += 1
                        continue
                    if tx_frames and _timestamp_delta(
                        frame.timestamp, tx_frames[-1].timestamp
                    ) <= 0:
                        counters["tx_playout_late_discard"] += 1
                        continue
                    tx_frames.append(frame)
                    while len(tx_frames) > max_frames:
                        tx_frames.popleft()
                        counters["tx_playout_late_discard"] += 1
                    counters["tx_playout_depth"] = len(tx_frames)
                    counters["tx_playout_peak"] = max(
                        counters["tx_playout_peak"], len(tx_frames)
                    )
                    tx_ready.set()
                except Exception as err:  # noqa: BLE001 - malformed frames cannot stop call control.
                    counters["tx_error"] += 1
                    _LOGGER.debug("HA softphone browser audio TX drop: %s", err)
        finally:
            playout_task.cancel()
            await asyncio.gather(playout_task, return_exceptions=True)

    browser_task = asyncio.create_task(browser_to_rtp())
    lifetime_task = asyncio.create_task(call_ended.wait())
    update_task = asyncio.create_task(session_updates_to_ws())
    try:
        critical_tasks = {browser_task, lifetime_task, rx_task, update_task}
        done, _pending = await asyncio.wait(
            critical_tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        media_task_ended = bool(done - {browser_task, lifetime_task})
        if media_task_ended:
            for task in done - {browser_task, lifetime_task}:
                if task.cancelled():
                    continue
                error = task.exception()
                if error is not None:
                    _LOGGER.warning(
                        "HA softphone audio media task failed call_id=%s task=%s error=%s",
                        session.call_id,
                        task.get_name(),
                        error,
                    )
        if lifetime_task in done or media_task_ended:
            ws.force_close()
            if websocket_transport is not None:
                websocket_transport.close()
            browser_task.cancel()
            await asyncio.gather(browser_task, return_exceptions=True)
        if browser_task in done and not browser_task.cancelled():
            browser_task.result()
    finally:
        closed.set()
        remove_call_listener()
        if active_sessions.get(session.call_id) is session:
            active_sessions.pop(session.call_id, None)
        session.send_dtmf = None
        for task in (rx_task, browser_task, lifetime_task, update_task):
            task.cancel()
        for task in tuple(dtmf_tasks):
            task.cancel()
        transport.close()
        caller_cancelled = False
        try:
            await async_wait_for_cleanup(
                asyncio.gather(
                    rx_task,
                    browser_task,
                    lifetime_task,
                    update_task,
                    *tuple(dtmf_tasks),
                    return_exceptions=True,
                )
            )
        except asyncio.CancelledError:
            caller_cancelled = True
        publish_counters(force=True)
        _LOGGER.info(
            "HA softphone audio websocket detached call_id=%s ws_rx=%d rtp_tx=%d rtp_rx=%d ws_tx=%d "
            "drop_addr=%d drop_pt=%d drop_size=%d drop_error=%d drop_rx_queue=%d "
            "drop_tx_queue=%d tx_error=%d tx_playout_peak=%d tx_late_discard=%d "
            "tx_plc=%d tx_rebuffer=%d rx_playout_peak=%d rx_late_discard=%d "
            "rx_plc=%d rx_rebuffer=%d",
            session.call_id,
            counters["ws_rx"],
            counters["rtp_tx"],
            counters["rtp_rx"],
            counters["ws_tx"],
            counters["drop_addr"],
            counters["drop_payload_type"],
            counters["drop_payload_size"],
            counters["drop_error"],
            protocol.dropped_packets,
            counters["drop_tx_queue"],
            counters["tx_error"],
            counters["tx_playout_peak"],
            counters["tx_playout_late_discard"],
            counters["tx_playout_plc"],
            counters["tx_playout_rebuffer"],
            counters["rx_playout_peak"],
            counters["rx_playout_late_discard"],
            counters["rx_playout_plc"],
            counters["rx_playout_rebuffer"],
        )
        if debug_capture is not None:
            _schedule_debug_capture_write(hass, debug_capture, counters)
        if caller_cancelled:
            raise asyncio.CancelledError


async def _run_conference_audio_session(
    hass: HomeAssistant,
    ws: web.WebSocketResponse,
    session: _SoftphoneMediaSession,
    *,
    handoff_requested: asyncio.Event | None = None,
    endpoint_id: str,
) -> None:
    conference_queue = session.conference_queue
    if conference_queue is None:
        _LOGGER.error("HA softphone conference session has no media queue call_id=%s", session.call_id)
        await ws.close(code=1011, message=b"Conference media queue unavailable")
        return
    closed = asyncio.Event()
    counters = {
        "ws_rx": 0,
        "ws_tx": 0,
        "rtp_rx": 0,
        "rtp_tx": 0,
        "rtp_rx_bytes": 0,
        "rtp_tx_bytes": 0,
        "drop_addr": 0,
        "drop_payload_type": 0,
        "drop_error": 0,
        "drop_tx_queue": 0,
        "tx_error": 0,
        "tx_silence_keepalive": 0,
    }
    await ws.send_json(
        {
            "state": "in_call",
            "call_id": session.call_id,
            "tx_format": session.send_format.audio_format.wire_token(),
            "rx_format": session.recv_format.audio_format.wire_token(),
            "selected_tx_format": session.send_format.audio_format.wire_token(),
            "selected_rx_format": session.recv_format.audio_format.wire_token(),
            "selected_tx_rtp_format": session.send_format.wire_token(),
            "selected_rx_rtp_format": session.recv_format.wire_token(),
            "audio_protocol": AUDIO_FRAME_VERSION,
            "media_generation": int(session.media_generation),
        }
    )
    _LOGGER.info("HA softphone conference websocket attached call_id=%s room=%s", session.call_id, session.conference_room)

    async def room_to_ws() -> None:
        send_sequence = 0
        send_timestamp = 0
        while not closed.is_set():
            pcm = await conference_queue.get()
            await ws.send_bytes(
                encode_audio_frame(
                    pcm,
                    generation=int(session.media_generation),
                    sequence=send_sequence,
                    timestamp=send_timestamp,
                )
            )
            send_sequence = rtp.next_sequence(send_sequence)
            send_timestamp = rtp.next_timestamp(
                send_timestamp,
                int(session.recv_format.audio_format.nominal_frame_samples),
            )
            counters["ws_tx"] += 1

    async def browser_to_room() -> None:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                try:
                    frame = decode_audio_frame(bytes(msg.data))
                    if frame.generation != int(session.media_generation):
                        counters["tx_error"] += 1
                        continue
                    pcm = frame.payload
                    expected = int(
                        session.send_format.audio_format.nominal_frame_bytes
                    )
                    if len(pcm) != expected:
                        raise ValueError(
                            f"browser PCM frame has {len(pcm)} bytes, expected {expected}"
                        )
                    manager = conference_component(hass)
                    if manager is not None:
                        manager.push_ha_audio(session.call_id, pcm)
                    counters["ws_rx"] += 1
                except Exception as err:  # noqa: BLE001 - keep media path alive.
                    counters["tx_error"] += 1
                    _LOGGER.debug("HA softphone conference audio TX drop: %s", err)
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                break

    rx_task = asyncio.create_task(room_to_ws())
    browser_task = asyncio.create_task(browser_to_room())
    call_ended, remove_call_listener = _listen_for_call_end(
        hass, session.call_id, endpoint_id
    )
    lifetime_task = asyncio.create_task(call_ended.wait())
    try:
        done, _pending = await asyncio.wait(
            {rx_task, browser_task, lifetime_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if rx_task in done and not rx_task.cancelled():
            error = rx_task.exception()
            if error is not None:
                _LOGGER.warning(
                    "HA softphone conference media sender failed call_id=%s error=%s",
                    session.call_id,
                    error,
                )
            ws.force_close()
        if lifetime_task in done:
            ws.force_close()
            browser_task.cancel()
            await asyncio.gather(browser_task, return_exceptions=True)
        if browser_task in done and not browser_task.cancelled():
            browser_task.result()
    finally:
        closed.set()
        remove_call_listener()
        rx_task.cancel()
        browser_task.cancel()
        lifetime_task.cancel()
        caller_cancelled = False
        try:
            await async_wait_for_cleanup(
                asyncio.gather(
                    rx_task,
                    browser_task,
                    lifetime_task,
                    return_exceptions=True,
                )
            )
        except asyncio.CancelledError:
            caller_cancelled = True
        # A browser media WebSocket is not the conference call controller.
        # Reloading a dashboard (or handing ownership to a new card instance)
        # must only detach this consumer; explicit hangup owns room teardown.
        store = _ha_softphone_store(hass, endpoint_id)
        if str(store.get("call_id") or "") == session.call_id:
            store.update(
                {
                    "last_sip_event": (
                        "conference_media_handoff"
                        if handoff_requested is not None and handoff_requested.is_set()
                        else "conference_media_detached"
                    ),
                    **counters,
                }
            )
            _publish_ha_softphone_state(hass, endpoint_id=endpoint_id)
        if caller_cancelled:
            raise asyncio.CancelledError
