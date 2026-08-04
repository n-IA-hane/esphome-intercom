"""Browser audio WebSocket for the HA SIP softphone media leg."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
import threading
from typing import Any, Callable
import wave

from aiohttp import WSMsgType, web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from . import rtp
from .audio_ws import decode_audio_frame, encode_audio_frame
from .const import CONF_DEBUG_MODE, CONF_MEDIA_CAPTURE, DOMAIN
from .audio_format import HA_SIP_PCM_FORMATS
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
from .dtmf import RtpDtmfDecoder, telephone_event_code
from .media_debug import merge_media_debug
from .media_call_lifetime import (
    active_media_call,
    listen_for_media_call_end as _listen_for_call_end,
)
from .queue_utils import drain_queue, put_drop_oldest
from .session_cleanup import async_wait_for_cleanup
from .sip_client import RtpPayloadDecoder, RtpPayloadEncoder, SipCallClient
from .phone_endpoint import DEFAULT_ENDPOINT_ID
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
# (3,840 bytes) plus the one-byte framing tag.  Keep a small fixed ceiling so
# aiohttp never buffers multi-megabyte payloads on this real-time endpoint.
_MAX_BROWSER_AUDIO_MESSAGE_BYTES = 4096


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
    on_dtmf: Callable[[str], None] | None = None
    conference_room: str = ""
    conference_queue: asyncio.Queue[bytes] | None = None
    local_ssrc: int = 0
    rtp_source: rtp.AudioRtpSenderState | None = None
    media_generation: int = 0
    update_event: asyncio.Event = field(default_factory=asyncio.Event)


def _rx_queue_frame_limit(frame_ms: int) -> int:
    """Keep roughly 160 ms of receive jitter across negotiated ptimes."""

    value = max(1, int(frame_ms))
    return max(4, min(16, (160 + value - 1) // value))


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

    bucket = hass.data.setdefault(DOMAIN, {})
    tasks: set[asyncio.Future[Any]] = bucket.setdefault(
        "debug_capture_tasks", set()
    )
    tasks.difference_update(task for task in tasks if task.done())
    if len(tasks) >= DEBUG_CAPTURE_MAX_PENDING_WRITES:
        bucket["debug_capture_dropped_writes"] = int(
            bucket.get("debug_capture_dropped_writes", 0)
        ) + 1
        _LOGGER.warning(
            "HA softphone debug capture queue full; dropping capture call_id=%s pending=%d",
            capture.call_id,
            len(tasks),
        )
        return
    if not try_reserve_debug_capture_write():
        bucket["debug_capture_dropped_writes"] = int(
            bucket.get("debug_capture_dropped_writes", 0)
        ) + 1
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
    if hass.data.setdefault(DOMAIN, {}).get("audio_ws_view_registered"):
        return
    hass.http.register_view(VoipAudioWebSocketView)
    hass.data[DOMAIN]["audio_ws_view_registered"] = True
    _LOGGER.info("HA softphone browser audio websocket ready on %s", VoipAudioWebSocketView.url)


def _active_softphone_media_session(
    hass: HomeAssistant,
    endpoint_id: str = DEFAULT_ENDPOINT_ID,
) -> _SoftphoneMediaSession | None:
    active = active_media_call(hass, endpoint_id)
    if active is None:
        return None
    call_id = active.call_id
    registry = active.registry
    inbound = registry.softphone_media

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

    if call_id and call_id in inbound:
        item = inbound[call_id]
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
            from .sdp import offered_dtmf_formats

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
                on_dtmf=_dtmf_callback("left"),
                rtp_source=rtp_source,
            )

    clients: dict[str, SipCallClient] = registry.sip_clients
    if call_id and call_id in clients:
        client = clients[call_id]
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
        }
    )

    async def peer_to_browser() -> None:
        while True:
            pcm = await bridge.receive_audio(
                lease.call_id,
                lease.endpoint_id,
                lease.token,
            )
            await ws.send_bytes(encode_audio_frame(pcm))
            counters["ws_tx"] += 1

    async def browser_to_peer() -> None:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                try:
                    pcm = decode_audio_frame(bytes(msg.data))
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
    endpoint_id: str = DEFAULT_ENDPOINT_ID,
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
    queue: asyncio.Queue[tuple[bytes, tuple[str, int]]] = asyncio.Queue(maxsize=16)
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
        "drop_direction": 0,
        "drop_connection_hold": 0,
        "dtmf_rx_events": 0,
    }
    logged_first_rtp = False
    latched_rtp_source: tuple[str, int] | None = None
    latched_rtp_ssrc: int | None = None
    remote_rtp_host = str(session.remote_rtp_host)
    remote_rtp_port = int(session.remote_rtp_port)
    applied_media_generation = int(session.media_generation)
    last_counter_event = 0.0
    ws_send_lock = asyncio.Lock()
    media_state_lock = asyncio.Lock()
    # Browser frames can arrive in short scheduler-driven bursts. Keep a
    # shallow FIFO jitter buffer and consume exactly one frame per RTP tick.
    tx_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=4)
    debug_capture = (
        _DebugAudioCapture(session.call_id, rx_format=session.recv_format, tx_format=session.send_format)
        if bool(hass.data.get(DOMAIN, {}).get(CONF_MEDIA_CAPTURE, False))
        else None
    )
    rtp_decoder: RtpPayloadDecoder
    rtp_encoder: RtpPayloadEncoder
    dtmf_decoder = (
        RtpDtmfDecoder(session.dtmf_payload_type)
        if session.dtmf_payload_type is not None
        else None
    )
    tx_frame_delay = max(0.001, session.send_format.audio_format.frame_ms / 1000.0)
    tx_silence_pcm = bytes(int(session.send_format.audio_format.nominal_frame_bytes))

    def publish_counters(*, force: bool = False) -> None:
        nonlocal last_counter_event
        now = loop.time()
        debug_mode = bool(hass.data.get(DOMAIN, {}).get(CONF_DEBUG_MODE, False))
        publish_interval = 0.5 if debug_mode else 5.0
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
        if debug_mode:
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
        }
        if message_type:
            payload["type"] = message_type
        return payload

    async def refresh_media_state(generation: int) -> None:
        nonlocal applied_media_generation, remote_rtp_host, remote_rtp_port
        nonlocal latched_rtp_source, latched_rtp_ssrc, logged_first_rtp
        nonlocal rtp_decoder, rtp_encoder, tx_frame_delay, tx_silence_pcm
        nonlocal dtmf_decoder
        nonlocal debug_capture
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
            next_frame_delay = max(
                0.001,
                session.send_format.audio_format.frame_ms / 1000.0,
            )
            next_silence_pcm = bytes(
                int(session.send_format.audio_format.nominal_frame_bytes)
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
            counters["drop_tx_queue"] += drain_queue(tx_queue)
            rtp_decoder = next_decoder
            rtp_encoder = next_encoder
            dtmf_decoder = next_dtmf_decoder
            tx_frame_delay = next_frame_delay
            tx_silence_pcm = next_silence_pcm
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
        observed_generation = int(session.media_generation)
        while not closed.is_set():
            if observed_generation != session.media_generation:
                observed_generation = int(session.media_generation)
                await refresh_media_state(observed_generation)
            data, addr = await queue.get()
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
                if debug_capture is not None:
                    debug_capture.note_rtp_rx(loop.time(), pcm)
                async with ws_send_lock:
                    await ws.send_bytes(encode_audio_frame(pcm))
                if debug_capture is not None:
                    debug_capture.note_ws_send(loop.time())
                counters["ws_tx"] += 1
                publish_counters()
            except (ConnectionError, RuntimeError):
                # A dead browser transport ends this media owner. Treating it
                # as malformed RTP would leave a zombie UDP session spinning.
                raise
            except Exception as err:  # noqa: BLE001 - media path must stay alive on bad packets.
                counters["drop_error"] += 1
                _LOGGER.debug("HA softphone RTP RX drop: %s", err)

    async def ws_to_rtp() -> None:
        nonlocal sequence, timestamp, remote_rtp_host, remote_rtp_port
        frame_delay = tx_frame_delay
        next_send = loop.time()
        silence_pcm = tx_silence_pcm
        observed_generation = int(session.media_generation)
        while not closed.is_set():
            if observed_generation != session.media_generation:
                observed_generation = int(session.media_generation)
                await refresh_media_state(observed_generation)
                frame_delay = tx_frame_delay
                silence_pcm = tx_silence_pcm
                next_send = loop.time()
            sleep_for = next_send - loop.time()
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
                if closed.is_set():
                    break
            if observed_generation != session.media_generation:
                observed_generation = int(session.media_generation)
                await refresh_media_state(observed_generation)
                frame_delay = tx_frame_delay
                silence_pcm = tx_silence_pcm
                next_send = loop.time()
            try:
                pcm = tx_queue.get_nowait()
            except asyncio.QueueEmpty:
                pcm = silence_pcm
                counters["tx_silence_keepalive"] += 1
            if (
                session.remote_audio_connection_held
                or session.local_audio_direction not in {"sendonly", "sendrecv"}
            ):
                counter = (
                    "drop_connection_hold"
                    if session.remote_audio_connection_held
                    else "drop_direction"
                )
                counters[counter] += 1
                timestamp = rtp.next_timestamp(
                    timestamp,
                    session.send_format.rtp_timestamp_step,
                )
                rtp_source.timestamp = timestamp
                publish_counters()
                next_send += frame_delay
                if next_send <= loop.time():
                    next_send = loop.time() + frame_delay
                continue
            try:
                payload = rtp_encoder.encode(pcm)
            except Exception as err:  # noqa: BLE001 - keep the media clock alive.
                counters["tx_error"] += 1
                _LOGGER.debug("HA softphone RTP encode drop: %s", err)
                timestamp = rtp.next_timestamp(
                    timestamp,
                    session.send_format.rtp_timestamp_step,
                )
                rtp_source.timestamp = timestamp
                next_send = max(next_send + frame_delay, loop.time() + frame_delay)
                continue
            if payload:
                packet = rtp.build_packet(
                    rtp.RtpPacket(
                        payload_type=session.send_format.payload_type,
                        sequence=sequence,
                        timestamp=timestamp,
                        ssrc=ssrc,
                        payload=payload,
                    )
                )
                try:
                    transport.sendto(packet, (remote_rtp_host, remote_rtp_port))
                except (OSError, RuntimeError) as err:
                    counters["tx_error"] += 1
                    _LOGGER.debug("HA softphone RTP TX drop: %s", err)
                else:
                    if debug_capture is not None:
                        debug_capture.note_rtp_tx(loop.time())
                    counters["rtp_tx"] += 1
                    counters["rtp_tx_bytes"] += len(packet)
                sequence = rtp.next_sequence(sequence)
                rtp_source.sequence = sequence
            timestamp = rtp.next_timestamp(
                timestamp,
                session.send_format.rtp_timestamp_step,
            )
            rtp_source.timestamp = timestamp
            publish_counters()
            next_send += frame_delay
            if next_send <= loop.time():
                next_send = loop.time() + frame_delay

    rx_task = asyncio.create_task(rtp_to_ws())
    tx_task = asyncio.create_task(ws_to_rtp())
    call_ended, remove_call_listener = _listen_for_call_end(
        hass, session.call_id, endpoint_id
    )
    active_sessions = hass.data.setdefault(DOMAIN, {}).setdefault("active_audio_sessions", {})
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

    async def browser_to_queue() -> None:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                try:
                    counters["ws_rx"] += 1
                    pcm = decode_audio_frame(bytes(msg.data))
                    expected = int(session.send_format.audio_format.nominal_frame_bytes)
                    if len(pcm) != expected:
                        raise ValueError(f"browser PCM frame has {len(pcm)} bytes, expected {expected}")
                    if debug_capture is not None:
                        debug_capture.note_ws_rx(loop.time(), pcm)
                    if tx_queue.full():
                        tx_queue.get_nowait()
                        counters["drop_tx_queue"] += 1
                    tx_queue.put_nowait(pcm)
                except Exception as err:  # noqa: BLE001 - malformed frames cannot stop call control.
                    counters["tx_error"] += 1
                    _LOGGER.debug("HA softphone browser audio TX drop: %s", err)
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                break

    browser_task = asyncio.create_task(browser_to_queue())
    lifetime_task = asyncio.create_task(call_ended.wait())
    update_task = asyncio.create_task(session_updates_to_ws())
    try:
        critical_tasks = {browser_task, lifetime_task, rx_task, tx_task, update_task}
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
        for task in (rx_task, tx_task, browser_task, lifetime_task, update_task):
            task.cancel()
        transport.close()
        caller_cancelled = False
        try:
            await async_wait_for_cleanup(
                asyncio.gather(
                    rx_task,
                    tx_task,
                    browser_task,
                    lifetime_task,
                    update_task,
                    return_exceptions=True,
                )
            )
        except asyncio.CancelledError:
            caller_cancelled = True
        publish_counters(force=True)
        _LOGGER.info(
            "HA softphone audio websocket detached call_id=%s ws_rx=%d rtp_tx=%d rtp_rx=%d ws_tx=%d "
            "drop_addr=%d drop_pt=%d drop_size=%d drop_error=%d drop_rx_queue=%d "
            "drop_tx_queue=%d tx_error=%d",
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
    endpoint_id: str = DEFAULT_ENDPOINT_ID,
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
        }
    )
    _LOGGER.info("HA softphone conference websocket attached call_id=%s room=%s", session.call_id, session.conference_room)

    async def room_to_ws() -> None:
        while not closed.is_set():
            pcm = await conference_queue.get()
            await ws.send_bytes(encode_audio_frame(pcm))
            counters["ws_tx"] += 1

    async def browser_to_room() -> None:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                try:
                    pcm = decode_audio_frame(bytes(msg.data))
                    expected = int(
                        session.send_format.audio_format.nominal_frame_bytes
                    )
                    if len(pcm) != expected:
                        raise ValueError(
                            f"browser PCM frame has {len(pcm)} bytes, expected {expected}"
                        )
                    manager = hass.data.setdefault(DOMAIN, {}).get("conference_manager")
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
