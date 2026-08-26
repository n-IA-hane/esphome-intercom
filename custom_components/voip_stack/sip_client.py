"""Outbound SIP/RTP primitives for the phase-1 VoIP Stack profile."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, replace
import logging
import re
import secrets
import socket
import ssl
from typing import Any, AsyncIterator, Awaitable, Callable

from .core.audio_format import AudioFormat, HA_SIP_PCM_FORMATS, PcmFormat
from .core import g711
from .core.codec_capabilities import supports_dahua_pcm
from .core.g722_codec import G722Decoder, G722Encoder
from .core.opus_codec import OpusDecoder, OpusEncoder
from .session_cleanup import async_wait_for_cleanup
from .core import sdp, sip, sip_transfer
from .core.sip_transport import default_tls_context
from .core.sip_auth import (
    DigestChallengeTracker,
    build_digest_authorization,
)
from .core.sip_resolution import SipServerResolver
from .core.sip_dialog import (
    DialogSignalingState,
    apply_remote_offer_media,
    build_dialog_request,
)
from .sip_tcp_io import (
    SipTcpWriter,
    read_sip_stream_message as _read_sip_stream_message,
)
from .sip_udp_io import SipDatagramQueueProtocol
from .core.sip_transaction import (
    SIP_T1,
    SIP_T2,
    SIP_TIMER_B,
    SipClientTransaction,
    SipInvite2xxTransaction,
    SessionTimerDriver,
    async_run_dialog_request_transaction,
    matches_response,
    same_request_transaction,
)

_LOGGER = logging.getLogger(__name__)

_S24_SIGN_EXTENSION = bytes(0xFF if value & 0x80 else 0x00 for value in range(256))
_SIP_UDP_SAFE_REQUEST_BYTES = 1300


def _uri_with_transport(uri: sip.SipUri, transport: str) -> sip.SipUri:
    """Return one URI with a single authoritative transport parameter."""

    params = tuple(
        (key, value)
        for key, value in uri.params
        if key.strip().lower() != "transport"
    )
    return replace(uri, params=(*params, ("transport", transport.lower())))


def _rtp_encoding(fmt: AudioFormat | sdp.RtpPcmFormat) -> str:
    return getattr(fmt, "encoding", "")


def _audio_format(fmt: AudioFormat | sdp.RtpPcmFormat) -> AudioFormat:
    return fmt.audio_format if isinstance(fmt, sdp.RtpPcmFormat) else fmt


def pcm_to_rtp_payload(data: bytes, fmt: AudioFormat | sdp.RtpPcmFormat) -> bytes:
    encoding = _rtp_encoding(fmt)
    if encoding == "PCM":
        if len(data) % 2:
            raise ValueError("Dahua PCM frame length is not sample-aligned")
        return data
    if encoding == "PCMA":
        return g711.s16le_to_alaw(data)
    if encoding == "PCMU":
        return g711.s16le_to_ulaw(data)
    if encoding == "OPUS":
        return OpusEncoder(fmt.sample_rate, fmt.channels).encode(data)
    if encoding == "G722":
        return G722Encoder().encode(data)
    fmt = _audio_format(fmt)
    if fmt.pcm_format == PcmFormat.S16LE:
        if len(data) % 2:
            raise ValueError("s16le frame length is not sample-aligned")
        out = bytearray(len(data))
        out[0::2] = data[1::2]
        out[1::2] = data[0::2]
        return bytes(out)
    if fmt.pcm_format == PcmFormat.S24LE:
        if len(data) % 3:
            raise ValueError("s24le frame length is not sample-aligned")
        out = bytearray(len(data))
        out[0::3] = data[2::3]
        out[1::3] = data[1::3]
        out[2::3] = data[0::3]
        return bytes(out)
    if fmt.pcm_format == PcmFormat.S24LE_IN_S32:
        if len(data) % 4:
            raise ValueError("s24le_in_s32 frame length is not sample-aligned")
        samples = len(data) // 4
        out = bytearray(samples * 3)
        out[0::3] = data[2::4]
        out[1::3] = data[1::4]
        out[2::3] = data[0::4]
        return bytes(out)
    raise ValueError(f"{fmt.pcm_format.value} has no phase-1 RTP mapping")


def rtp_payload_to_pcm(payload: bytes, fmt: AudioFormat | sdp.RtpPcmFormat) -> bytes:
    encoding = _rtp_encoding(fmt)
    if encoding == "PCM":
        if len(payload) % 2:
            raise ValueError("Dahua PCM payload length is not sample-aligned")
        return payload
    if encoding == "PCMA":
        return g711.alaw_to_s16le(payload)
    if encoding == "PCMU":
        return g711.ulaw_to_s16le(payload)
    if encoding == "OPUS":
        return OpusDecoder(fmt.sample_rate, fmt.channels).decode(payload)
    if encoding == "G722":
        return G722Decoder().decode(payload)
    fmt = _audio_format(fmt)
    if fmt.pcm_format == PcmFormat.S16LE:
        if len(payload) % 2:
            raise ValueError("L16 payload length is not sample-aligned")
        out = bytearray(len(payload))
        out[0::2] = payload[1::2]
        out[1::2] = payload[0::2]
        return bytes(out)
    if fmt.pcm_format == PcmFormat.S24LE:
        if len(payload) % 3:
            raise ValueError("L24 payload length is not sample-aligned")
        out = bytearray(len(payload))
        out[0::3] = payload[2::3]
        out[1::3] = payload[1::3]
        out[2::3] = payload[0::3]
        return bytes(out)
    if fmt.pcm_format == PcmFormat.S24LE_IN_S32:
        if len(payload) % 3:
            raise ValueError("L24 payload length is not sample-aligned")
        samples = len(payload) // 3
        out = bytearray(samples * 4)
        out[0::4] = payload[2::3]
        out[1::4] = payload[1::3]
        out[2::4] = payload[0::3]
        out[3::4] = payload[0::3].translate(_S24_SIGN_EXTENSION)
        return bytes(out)
    raise ValueError(f"{fmt.pcm_format.value} has no phase-1 RTP mapping")


class RtpPayloadDecoder:
    def __init__(self, fmt: sdp.RtpPcmFormat) -> None:
        self.fmt = fmt
        pcm_format = fmt.audio_format
        self._codec = (
            OpusDecoder(pcm_format.sample_rate, pcm_format.channels, pcm_format.frame_ms)
            if fmt.encoding == "OPUS"
            else G722Decoder()
            if fmt.encoding == "G722"
            else None
        )

    def decode(self, payload: bytes) -> bytes:
        if self._codec is not None:
            return self._codec.decode(payload)
        return rtp_payload_to_pcm(payload, self.fmt)


class RtpPayloadEncoder:
    def __init__(self, fmt: sdp.RtpPcmFormat) -> None:
        self.fmt = fmt
        pcm_format = fmt.audio_format
        self._codec = (
            OpusEncoder(pcm_format.sample_rate, pcm_format.channels, pcm_format.frame_ms)
            if fmt.encoding == "OPUS"
            else G722Encoder()
            if fmt.encoding == "G722"
            else None
        )

    def encode(self, pcm: bytes) -> bytes:
        if self._codec is not None:
            return self._codec.encode(pcm)
        return pcm_to_rtp_payload(pcm, self.fmt)


def _sip_decline_reason(msg: sip.SipMessage) -> str:
    direct = (msg.header("X-Voip-Stack-Decline-Reason") or "").strip()
    if direct:
        return direct
    reason = msg.header("Reason")
    marker = "text="
    idx = reason.find(marker)
    if idx < 0:
        return ""
    value = reason[idx + len(marker) :].strip()
    if not value:
        return ""
    if value[0] != '"':
        return value.split(";", 1)[0].strip()
    out: list[str] = []
    escaped = False
    for ch in value[1:]:
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            break
        out.append(ch)
    return "".join(out).strip()


def _is_invite_progress_response(status_code: int | None) -> bool:
    return status_code is not None and 100 < int(status_code) < 200


def _sip_header_token(value: str) -> str:
    return "".join(
        ch
        for ch in str(value or "").strip()
        if ch.isalnum() or ch in " _-."
    ).strip()


@dataclass(slots=True)
class SipDialog(DialogSignalingState):
    target: str
    remote_host: str
    remote_sip_port: int
    remote_rtp_host: str
    remote_rtp_port: int
    local_rtp_port: int
    call_id: str
    local_uri: str
    remote_uri: str
    send_format: sdp.RtpPcmFormat
    recv_format: sdp.RtpPcmFormat
    remote_tag: str = ""
    dtmf_payload_type: int | None = None
    dtmf_clock_rate: int = 8000
    dtmf_events: frozenset[int] = frozenset(range(16))
    send_dtmf_payload_type: int | None = None
    send_dtmf_clock_rate: int | None = None
    send_dtmf_events: frozenset[int] | None = None
    remote_audio_direction: str = "sendrecv"
    local_audio_direction: str = "sendrecv"
    remote_audio_connection_held: bool = False
    video_format: sdp.RtpVideoFormat | None = None
    # ``video_format`` remains the backward-compatible local-TX contract
    # selected by the remote answer.  ``local_video_format`` is the distinct
    # local-RX contract retained from our offer (RFC 6184 level asymmetry and
    # VP8 receiver limits).
    local_video_format: sdp.RtpVideoFormat | None = None
    remote_video_rtp_host: str = ""
    remote_video_rtp_port: int = 0
    remote_video_rtcp_host: str = ""
    remote_video_rtcp_port: int = 0
    remote_video_rtcp_mux: bool = False
    remote_video_payload_types: tuple[int, ...] = ()
    remote_video_connection_held: bool = False
    local_video_rtp_port: int = 0
    local_video_direction: str = "inactive"

    @property
    def selected_format(self) -> sdp.RtpPcmFormat:
        return self.send_format

    @property
    def send_video_format(self) -> sdp.RtpVideoFormat | None:
        return self.video_format

    @property
    def recv_video_format(self) -> sdp.RtpVideoFormat | None:
        return self.local_video_format or self.video_format


@dataclass(frozen=True, slots=True)
class _DelayedRemoteOffer:
    request: sip.SipMessage
    offer_sdp: str
    remote_target_uri: str
    local_sdp_session_version: int


DialogMediaCommit = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class PreparedDialogMediaUpdate:
    """Two-phase media update owned until its SIP response is committed."""

    commit: DialogMediaCommit
    rollback: DialogMediaCommit | None = None
    answer_video_format: sdp.RtpVideoFormat | None = None
    answer_video_rtp_port: int | None = None


DialogMediaUpdateHandler = Callable[
    [SipDialog, SipDialog, str],
    Awaitable[DialogMediaCommit | PreparedDialogMediaUpdate | None],
]
ReferHandler = Callable[[sip_transfer.SipReferTarget], Awaitable[int]]


@dataclass(frozen=True, slots=True)
class SipTransferResult:
    """Final outcome reported by the REFER subscription."""

    accepted: bool
    status: int
    state: str


@dataclass(slots=True, frozen=True)
class _InDialogResponse:
    request: sip.SipMessage
    status: int
    reason: str
    extra_headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b""


_SipClientProtocol = SipDatagramQueueProtocol
_MIN_INVITE_RETRY_AFTER = 0.2
_MAX_INVITE_RETRY_AFTER = 2.0


class SipCallClient:
    """One outbound SIP dialog.

    This is intentionally small and standards-shaped. It can call an ESP or HA
    SIP URI and expose the negotiated RTP parameters to a relay/session owner.
    """

    def __init__(
        self,
        *,
        local_ip: str,
        local_name: str,
        local_uri_user: str = "",
        local_sip_port: int,
        local_rtp_port: int,
        supported_formats: list[AudioFormat] | None = None,
        supported_send_formats: list[AudioFormat] | None = None,
        supported_recv_formats: list[AudioFormat] | None = None,
        signaling_transport: str = "UDP",
        auth_username: str = "",
        username: str = "",
        password: str = "",
        outbound_proxy: str = "",
        include_common_codecs: bool = False,
        allow_directional_audio_payloads: bool = False,
        peer_user_agent: str = "",
        local_video_rtp_port: int = 0,
        video_format: sdp.RtpVideoFormat | None = None,
        video_formats: tuple[sdp.RtpVideoFormat, ...] | list[sdp.RtpVideoFormat] | None = None,
        video_direction: str = "sendrecv",
        generic_video_relay: bool = False,
        allow_video_transcoding: bool = False,
        media_reservation=None,
        video_rtp_socket: socket.socket | None = None,
        video_rtcp_socket: socket.socket | None = None,
        target_resolver: SipServerResolver | None = None,
        tls_context: ssl.SSLContext | None = None,
    ) -> None:
        self.local_ip = local_ip
        self.local_name = local_name
        self.local_uri_user = str(local_uri_user or username or local_name).strip()
        self.local_sip_port = int(local_sip_port)
        self.local_rtp_port = int(local_rtp_port)
        # ``None`` means that the caller did not constrain the profile.  An
        # empty list is materially different: directional capability
        # negotiation ran and found no usable format.  Never turn that result
        # back into the broad HA default offer.
        base_formats = (
            list(HA_SIP_PCM_FORMATS)
            if supported_formats is None
            else list(supported_formats)
        )
        self.supported_send_formats = (
            list(base_formats)
            if supported_send_formats is None
            else list(supported_send_formats)
        )
        self.supported_recv_formats = (
            list(base_formats)
            if supported_recv_formats is None
            else list(supported_recv_formats)
        )
        self.signaling_transport = (signaling_transport or "UDP").upper()
        self.auth_username = auth_username
        self.username = username or local_name
        self.password = password
        self.outbound_proxy = outbound_proxy
        self.include_common_codecs = bool(include_common_codecs)
        self.allow_directional_audio_payloads = bool(
            allow_directional_audio_payloads
        )
        self.peer_user_agent = str(peer_user_agent or "").strip()
        self.include_dahua_pcm = supports_dahua_pcm(self.peer_user_agent)
        self.local_video_rtp_port = int(local_video_rtp_port or 0)
        requested_video = tuple(video_formats or (() if video_format is None else (video_format,)))
        self.video_formats = requested_video if self.local_video_rtp_port > 0 else ()
        self.video_format = self.video_formats[0] if self.video_formats else None
        self.video_direction = str(video_direction or "sendrecv")
        self.generic_video_relay = bool(generic_video_relay)
        self.allow_video_transcoding = bool(allow_video_transcoding)
        self.media_reservation = media_reservation
        self.video_rtp_socket = video_rtp_socket
        self.video_rtcp_socket = video_rtcp_socket
        self.target_resolver = target_resolver or SipServerResolver()
        self.tls_context = tls_context
        self._tls_server_name = ""
        # RTP source identity belongs to this SIP call and survives a
        # dashboard media-owner handoff. The WebSocket views initialize the
        # codec-specific state lazily to avoid coupling SIP signaling to them.
        self.audio_rtp_source = None
        self.video_rtp_source = None
        self._sdp_session_id = secrets.randbits(63) or 1
        self._local_sdp_body = ""
        self.transport: asyncio.DatagramTransport | None = None
        self.protocol: SipDatagramQueueProtocol | None = None
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._tcp_writer: SipTcpWriter | None = None
        self._tcp_reuse_send: Callable[[bytes], bool | None] | None = None
        self._tcp_reuse_responses: asyncio.Queue[bytes] | None = None
        self._tcp_reuse_close: Callable[[], None] | None = None
        self.queue: asyncio.Queue[tuple[bytes, tuple[str, int]]] = asyncio.Queue(maxsize=128)
        self._deferred_signaling: list[tuple[sip.SipMessage, tuple[str, int]]] = []
        self._reliable_rseq: dict[tuple[str, int], int] = {}
        self.dialog_ids = sip.SipDialogIds(call_id=sip.make_call_id("ha"), local_tag=sip.make_tag())
        self.dialog: SipDialog | None = None
        self.early_dialogs: dict[str, SipDialog] = {}
        self._terminated_invite_branches: set[str] = set()
        self.on_info_dtmf: Callable[[str], None] | None = None
        self.on_media_update: DialogMediaUpdateHandler | None = None
        self.on_connected_identity: Callable[[str, str], None] | None = None
        self.on_refer: ReferHandler | None = None
        self._invite_cseq = self.dialog_ids.cseq
        self._pending_target = ""
        self._pending_target_display = ""
        self._dialog_remote_display_name = ""
        self._pending_remote_host = ""
        self._pending_remote_sip_port = 5060
        self._pending_request_uri = ""
        self._pending_local_uri = ""
        self._pending_remote_uri = ""
        self._signaling_nominal: tuple[str, int] | None = None
        self._resolved_signaling_target: tuple[str, int] | None = None
        self._udp_family_host = ""
        self._pending_invite_body = b""
        self._pending_invite_auth: dict[str, tuple[str, str, int]] = {}
        self._initial_delayed_offer = False
        self._retry_after_used = False
        self._invite_abort_event = asyncio.Event()
        self._invite_transaction_active = False
        self._cancel_requested = False
        self._cancel_sent = False
        self._received_provisional = False
        self._invite_task: asyncio.Task[str] | None = None
        self._invite_progress: asyncio.Future[str] | None = None
        self._invite_transaction: SipClientTransaction[
            tuple[sip.SipMessage, tuple[str, int]]
        ] | None = None
        self._start_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._deferred_close_task: asyncio.Task[None] | None = None
        self._dialog_termination_task: asyncio.Task[str] | None = None
        self._exceptional_bye_tasks: set[asyncio.Task[None]] = set()
        self._closing = False
        self._closed = False
        self._local_dialog_cseq = self._invite_cseq
        self._remote_cseq = 0
        self._in_dialog_responses: list[_InDialogResponse] = []
        self._refer_notifications: asyncio.Queue[tuple[int, bool]] | None = None
        self._incoming_refer_task: asyncio.Task[None] | None = None
        self._dialog_read_lock = asyncio.Lock()
        self._local_offer_lock = asyncio.Lock()
        self._dialog_writer_requested = asyncio.Event()
        self._dialog_writer_count = 0
        self._prepared_reinvite: tuple[
            SipDialog,
            SipDialog,
            tuple[sdp.RtpVideoFormat, ...],
            str,
        ] | None = None
        self._uas_invite_2xx = SipInvite2xxTransaction()
        self._uas_invite_ack_timeout = asyncio.Event()
        self._uas_delayed_offer: _DelayedRemoteOffer | None = None
        self.last_sip_event = ""
        self.last_sip_status_code = 0
        self.last_sip_reason = ""
        self.connected_party = ""

    async def start(self) -> None:
        if self.signaling_transport in {"TCP", "TLS"}:
            return
        async with self._start_lock:
            if self.transport is not None:
                return
            if self._closing or self._closed:
                raise RuntimeError("SIP client is already closed")
            loop = asyncio.get_running_loop()
            protocol = SipDatagramQueueProtocol(self.queue)
            family = socket.AF_INET6 if ":" in self._udp_family_host else socket.AF_INET
            transport, _ = await loop.create_datagram_endpoint(
                lambda: protocol,
                local_addr=("::" if family == socket.AF_INET6 else "0.0.0.0", 0),
                family=family,
            )
            if self._closing or self._closed:
                transport.close()
                raise RuntimeError("SIP client closed while starting")
            self.protocol = protocol
            self.transport = transport  # type: ignore[assignment]
            sockname = transport.get_extra_info("sockname")
            if sockname and len(sockname) >= 2 and int(sockname[1]) > 0:
                self.local_sip_port = int(sockname[1])

    async def close(self) -> None:
        self._closing = True
        self._invite_abort_event.set()
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._close(),
                name=f"voip-sip-client-close-{self.dialog_ids.call_id}",
            )
        await async_wait_for_cleanup(self._close_task)

    async def _close(self) -> None:
        self._uas_invite_2xx.cancel()
        self._uas_delayed_offer = None
        if self._invite_transaction_active:
            with contextlib.suppress(Exception):
                self.cancel()
        # Detach and release media before the first cancellation point.  SIP
        # TCP shutdown can wait on a congested writer, but it must never hold
        # video sockets or an allocator reservation hostage.
        reservation = self.media_reservation
        self.media_reservation = None
        video_socket = self.video_rtp_socket
        self.video_rtp_socket = None
        if video_socket is not None:
            video_socket.close()
        video_rtcp_socket = self.video_rtcp_socket
        self.video_rtcp_socket = None
        if video_rtcp_socket is not None:
            video_rtcp_socket.close()
        if reservation is not None and hasattr(reservation, "release"):
            reservation.release()

        # ``invite()`` and ``wait_for_final()`` shield the SIP transaction
        # from a cancelled UI/service waiter.  Closing therefore first gives
        # the current owner a bounded opportunity to complete the standard
        # CANCEL/487 or ACK/BYE exchange while signaling is still available.
        # Only an unresponsive transaction is force-cancelled afterwards.
        current_task = asyncio.current_task()
        owned_signaling_task = next(
            (
                task
                for task in (self._dialog_termination_task, self._invite_task)
                if task is not None
                and task is not current_task
                and not task.done()
            ),
            None,
        )
        if owned_signaling_task is None and (
            self.dialog is not None or self._invite_transaction_active
        ):
            owned_signaling_task = asyncio.create_task(
                self.terminate(timeout=1.5),
                name=f"voip-sip-client-terminate-{self.dialog_ids.call_id}",
            )
        if owned_signaling_task is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(owned_signaling_task),
                    timeout=1.5,
                )
            except asyncio.TimeoutError:
                _LOGGER.debug(
                    "SIP signaling teardown reached its bounded fallback call_id=%s",
                    self.dialog_ids.call_id,
                )
            except asyncio.CancelledError:
                _LOGGER.debug(
                    "SIP signaling owner was already cancelled call_id=%s",
                    self.dialog_ids.call_id,
                )
            except Exception:
                _LOGGER.debug(
                    "SIP signaling teardown failed call_id=%s",
                    self.dialog_ids.call_id,
                    exc_info=True,
                )

        lingering_tasks = tuple(
            dict.fromkeys(
                task
                for task in (
                    owned_signaling_task,
                    self._dialog_termination_task,
                    self._invite_task,
                    self._incoming_refer_task,
                    *self._exceptional_bye_tasks,
                )
                if task is not None
                and task is not current_task
                and not task.done()
            )
        )
        for task in lingering_tasks:
            task.cancel()
        if lingering_tasks:
            await asyncio.gather(*lingering_tasks, return_exceptions=True)
        self._invite_transaction_active = False
        self._invite_transaction = None
        self._invite_progress = None
        self._dialog_termination_task = None
        self._exceptional_bye_tasks.clear()
        self.dialog = None
        self.early_dialogs.clear()
        self._terminated_invite_branches.clear()
        self._reliable_rseq.clear()
        self._deferred_signaling.clear()
        self._refer_notifications = None
        self._incoming_refer_task = None

        if self.transport is not None:
            self.transport.close()
            self.transport = None

        tcp_writer = self._tcp_writer
        self._tcp_writer = None
        writer = self.writer
        self.writer = None
        self.reader = None
        if self._tcp_reuse_close is not None:
            self._tcp_reuse_close()
            self._tcp_reuse_close = None
        self._tcp_reuse_send = None
        self._tcp_reuse_responses = None
        try:
            try:
                if tcp_writer is not None:
                    await tcp_writer.close()
            finally:
                # StreamWriter.close() is synchronous and must still run if
                # the queued writer task is cancelled while draining.
                if writer is not None:
                    writer.close()
            if writer is not None:
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
        finally:
            self._closed = True

    def use_reused_tcp_connection(
        self,
        *,
        send: Callable[[bytes], bool | None],
        responses: asyncio.Queue[bytes],
        close: Callable[[], None],
    ) -> None:
        if self._closing or self._closed:
            close()
            raise RuntimeError("SIP client is already closed")
        self._tcp_reuse_send = send
        self._tcp_reuse_responses = responses
        self._tcp_reuse_close = close

    async def _connect_tcp(self, remote_host: str, remote_sip_port: int) -> None:
        async with self._start_lock:
            if self._closing or self._closed:
                raise RuntimeError("SIP client is already closed")
            if self._tcp_reuse_send is not None:
                return
            if self.writer is not None and not self.writer.is_closing():
                return
            if self._tcp_writer is not None:
                await self._tcp_writer.close()
                self._tcp_writer = None
            if self.writer is not None:
                self.writer.close()
                with contextlib.suppress(Exception):
                    await self.writer.wait_closed()
                self.writer = None
                self.reader = None
            if self._closing or self._closed:
                raise RuntimeError("SIP client closed while connecting")
            host, port = self._signaling_target(remote_host, int(remote_sip_port))
            tls = self.signaling_transport == "TLS"
            if tls:
                if self.tls_context is None:
                    self.tls_context = await default_tls_context()
                reader, writer = await asyncio.open_connection(
                    host,
                    port,
                    ssl=self.tls_context,
                    server_hostname=self._tls_server_name,
                )
            else:
                reader, writer = await asyncio.open_connection(host, port)
            if self._closing or self._closed:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
                raise RuntimeError("SIP client closed while connecting")
            self.reader = reader
            self.writer = writer
            self._tcp_writer = SipTcpWriter(writer, label=f"client {host}:{port}")
            sock = writer.get_extra_info("socket")
            if sock is not None:
                sockname = sock.getsockname()
                if sockname and len(sockname) >= 2 and int(sockname[1]) > 0:
                    self.local_sip_port = int(sockname[1])

    async def _select_initial_signaling_target(
        self,
        host: str,
        port: int,
    ) -> None:
        """Own one pre-dialog network target and discard the prior attempt."""
        if self._tcp_reuse_send is not None:
            self._resolved_signaling_target = (host, int(port))
            return
        selected = (host, int(port))
        replacing = (
            self._resolved_signaling_target is not None
            and self._resolved_signaling_target != selected
        )
        if replacing and self.transport is not None:
            self.transport.close()
            self.transport = None
            self.protocol = None
        if replacing:
            tcp_writer = self._tcp_writer
            self._tcp_writer = None
            writer = self.writer
            self.writer = None
            self.reader = None
            if tcp_writer is not None:
                await tcp_writer.close()
            if writer is not None:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
            while not self.queue.empty():
                with contextlib.suppress(asyncio.QueueEmpty):
                    self.queue.get_nowait()
        self._resolved_signaling_target = selected
        self._udp_family_host = host
        if self.signaling_transport in {"TCP", "TLS"}:
            assert self._signaling_nominal is not None
            await self._connect_tcp(*self._signaling_nominal)
        else:
            await self.start()

    def _signaling_target(self, remote_host: str, remote_sip_port: int) -> tuple[str, int]:
        if self._resolved_signaling_target is not None and (
            self.outbound_proxy
            or self._signaling_nominal == (remote_host, int(remote_sip_port))
        ):
            return self._resolved_signaling_target
        proxy = str(self.outbound_proxy or "").strip()
        if not proxy:
            return remote_host, int(remote_sip_port)
        if proxy.lower().startswith(("sip:", "sips:")):
            proxy = proxy.split(":", 1)[1]
        proxy = proxy.split(";", 1)[0].strip()
        if "@" in proxy:
            proxy = proxy.rsplit("@", 1)[1]
        if ":" in proxy and proxy.count(":") == 1:
            host, port = proxy.rsplit(":", 1)
            try:
                return host.strip(), int(port)
            except ValueError:
                return host.strip(), int(remote_sip_port)
        return proxy, int(remote_sip_port)

    async def _send_raw(self, raw: bytes, remote_host: str, remote_sip_port: int) -> None:
        if self.signaling_transport in {"TCP", "TLS"}:
            if self._tcp_reuse_send is not None:
                if self._tcp_reuse_send(raw) is False:
                    raise ConnectionError("reused SIP TCP connection is not writable")
                return
            await self._connect_tcp(remote_host, remote_sip_port)
            if self._tcp_writer is None:
                raise ConnectionError("SIP TCP writer is not available")
            if not await self._tcp_writer.send(raw):
                raise ConnectionError("SIP TCP connection is not writable")
            return
        if self.transport is None:
            raise ConnectionError("SIP UDP transport is not available")
        host, port = self._signaling_target(remote_host, int(remote_sip_port))
        self.transport.sendto(raw, (host, port))

    def _has_signaling_path(self) -> bool:
        if self.signaling_transport in {"TCP", "TLS"}:
            return self.writer is not None or self._tcp_reuse_send is not None
        return self.transport is not None

    def _send_dialog_request(self, raw: bytes, host: str, port: int) -> bool:
        try:
            if self.signaling_transport in {"TCP", "TLS"}:
                if self._tcp_reuse_send is not None:
                    return self._tcp_reuse_send(raw) is not False
                if self.writer is None:
                    return False
                if self._tcp_writer is not None:
                    return self._tcp_writer.send_nowait(raw)
                return False
            if self.transport is not None:
                host, port = self._signaling_target(host, int(port))
                self.transport.sendto(raw, (host, port))
                return True
        except (ConnectionError, OSError, RuntimeError) as err:
            _LOGGER.debug("SIP dialog send failed for %s:%s: %s", host, port, err)
        return False

    def _dialog_next_hop(self, request_uri: str, fallback_host: str, fallback_port: int) -> tuple[str, int]:
        """Resolve an in-dialog target while preserving an explicit proxy."""
        if self.outbound_proxy:
            return fallback_host, int(fallback_port)
        try:
            target = sip.parse_sip_uri(request_uri)
        except (TypeError, ValueError, sip.SipError):
            return fallback_host, int(fallback_port)
        return target.host, int(target.port or 5060)

    def _next_dialog_cseq(self) -> int:
        self._local_dialog_cseq = max(
            self._local_dialog_cseq,
            self._invite_cseq,
        ) + 1
        return self._local_dialog_cseq

    def _ack_retransmitted_invite_2xx(self, message: sip.SipMessage) -> bool:
        dialog = self.dialog
        if (
            dialog is None
            or message.status_code is None
            or not 200 <= message.status_code < 300
            or not matches_response(
                message,
                method="INVITE",
                cseq=self._invite_cseq,
                branch=self.dialog_ids.branch,
            )
        ):
            return False
        remote_tag = sip.extract_tag(message.header("To"))
        if not remote_tag:
            return False
        if remote_tag != self.dialog_ids.remote_tag:
            return self._settle_losing_invite_branch(message)
        self._send_ack(
            dialog.remote_host,
            dialog.remote_sip_port,
            dialog.remote_target_uri or dialog.remote_uri,
            dialog.local_uri,
            dialog.remote_uri,
            route_set=dialog.route_set,
            remote_tag=remote_tag,
        )
        return True

    def _settle_losing_invite_branch(self, response: sip.SipMessage) -> bool:
        """ACK one forked 2xx and terminate that non-winning dialog once."""

        remote_tag = sip.extract_tag(response.header("To"))
        if not remote_tag:
            return False
        try:
            remote_target = (
                sip.contact_target_uri(response) or self._pending_remote_uri
            )
            route_set = sip.record_route_set(response, reverse=True)
        except (TypeError, ValueError, sip.SipError):
            return False
        host, port = self._dialog_next_hop(
            remote_target,
            self._pending_remote_host,
            self._pending_remote_sip_port,
        )
        acknowledged = self._send_ack(
            host,
            port,
            remote_target,
            self._pending_local_uri,
            self._pending_remote_uri,
            route_set=route_set,
            remote_tag=remote_tag,
        )
        if remote_tag not in self._terminated_invite_branches:
            self._terminated_invite_branches.add(remote_tag)
            self._start_bye_request_transaction(
                host,
                port,
                remote_target,
                self._pending_local_uri,
                self._pending_remote_uri,
                route_set=route_set,
                remote_tag=remote_tag,
            )
        return acknowledged

    def _send_response_to_request(
        self,
        request: sip.SipMessage,
        host: str,
        port: int,
        status: int,
        reason: str,
        *,
        extra_headers: tuple[tuple[str, str], ...] = (),
        body: bytes = b"",
    ) -> bool:
        raw = sip.build_uas_response(
            request,
            status,
            reason,
            contact_uri=(
                getattr(self.dialog, "local_uri", "") or self._pending_local_uri
            ),
            body=body,
            extra_headers=extra_headers,
        )
        if not self._send_dialog_request(raw, host, int(port)):
            _LOGGER.warning("SIP TX %s %s dropped: signaling path unavailable", status, reason)
            return False
        sip.mark_sip_event(self, "SIP_RESPONSE", int(status), reason)
        _LOGGER.info("SIP TX %s %s to %s:%s", status, reason, host, port)
        return True

    def _arm_uas_invite_2xx(
        self,
        request: sip.SipMessage,
        host: str,
        port: int,
        status: int,
        reason: str,
        *,
        extra_headers: tuple[tuple[str, str], ...] = (),
        body: bytes = b"",
    ) -> None:
        """Retransmit an INVITE 2xx until its end-to-end ACK arrives.

        RFC 3261 makes this UAS-core responsibility independent of the
        underlying transport, so the timer intentionally also runs over TCP.
        """
        if not 200 <= status < 300:
            return
        self._uas_invite_ack_timeout.clear()
        async def ack_timeout() -> None:
            _LOGGER.warning(
                "SIP remote re-INVITE ACK timed out call_id=%s cseq=%s; terminating dialog",
                self.dialog_ids.call_id,
                self._uas_invite_2xx.cseq,
            )
            dialog = self.dialog
            self._uas_invite_2xx.cancel()
            self._uas_delayed_offer = None
            if dialog is not None:
                self._start_bye_request_transaction(
                    dialog.remote_host,
                    dialog.remote_sip_port,
                    dialog.remote_target_uri or dialog.remote_uri,
                    dialog.local_uri,
                    dialog.remote_uri,
                    route_set=dialog.route_set,
                )
            self.dialog = None
            self._uas_invite_ack_timeout.set()

        self._uas_invite_2xx.start(
            request,
            transport=self.signaling_transport,
            send=lambda: self._send_response_to_request(
                request,
                host,
                port,
                status,
                reason,
                extra_headers=extra_headers,
                body=body,
            ),
            on_timeout=ack_timeout,
            timeout=SIP_TIMER_B,
            t1=SIP_T1,
            t2=SIP_T2,
            task_name=f"voip-sip-client-2xx-{self.dialog_ids.call_id}",
        )

    def _acknowledges_uas_invite_2xx(self, request: sip.SipMessage, host: str) -> bool:
        acknowledged = self._uas_invite_2xx.acknowledge(
            request,
            lambda ack: self._request_matches_dialog(ack, host, "ACK"),
        )
        if acknowledged:
            self._uas_invite_ack_timeout.clear()
        return acknowledged

    async def _commit_delayed_offer_ack(
        self, request: sip.SipMessage
    ) -> str | None:
        delayed = self._uas_delayed_offer
        self._uas_delayed_offer = None
        if delayed is None:
            return None
        content_type = request.header("Content-Type").split(";", 1)[0].lower()
        prepared = (
            self._answer_remote_offer(
                request,
                local_offer_sdp=delayed.offer_sdp,
            )
            if request.body and content_type == "application/sdp"
            else None
        )
        if prepared is None or self.dialog is None:
            await self._terminate_confirmed_dialog()
            return "media_incompatible"
        current = self.dialog
        updated, _offer = prepared
        if delayed.remote_target_uri:
            updated = replace(
                updated,
                remote_target_uri=delayed.remote_target_uri,
            )
        unchanged = self._same_dialog_media(current, updated)
        commit = None
        if self.on_media_update is not None:
            try:
                commit = await self.on_media_update(current, updated, "INVITE")
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception(
                    "SIP delayed offer preparation failed call_id=%s",
                    self.dialog_ids.call_id,
                )
                await self._terminate_confirmed_dialog()
                return "media_update_failed"
        if commit is None and not unchanged:
            await self._terminate_confirmed_dialog()
            return "media_incompatible"
        if not await apply_remote_offer_media(commit):
            _LOGGER.error(
                "SIP delayed offer commit failed call_id=%s", self.dialog_ids.call_id
            )
            await self._terminate_confirmed_dialog()
            return "media_update_failed"
        requested_timer = sip.negotiate_uas_session_timer(delayed.request)
        if requested_timer is not None:
            updated.session_timer.configure(
                requested_timer,
                local_role="uas",
                now=asyncio.get_running_loop().time(),
            )
        self.dialog = updated
        return None

    def _request_matches_dialog(self, request: sip.SipMessage, _host: str, method: str) -> bool:
        """Match an in-dialog request using the RFC 3261 dialog identifiers.

        A dialog is identified by its Call-ID and local/remote tags, not by the
        source IP address.  The request has already been selected by Call-ID in
        ``_read_response``.  Requiring the source to match the original target
        breaks valid dialogs traversing a proxy or SBC whose sequential
        requests can be emitted by a different signaling node.
        """
        dialog = self.dialog
        if dialog is None:
            return False
        try:
            cseq = sip.parse_cseq(request.header("CSeq"))
        except (TypeError, ValueError, sip.SipError):
            return False
        return (
            request.header("Call-ID") == self.dialog_ids.call_id
            and
            cseq.method == method.upper()
            and cseq.number > 0
            and sip.extract_tag(request.header("From")) == self.dialog_ids.remote_tag
            and sip.extract_tag(request.header("To")) == self.dialog_ids.local_tag
        )

    _same_in_dialog_transaction = staticmethod(same_request_transaction)

    def _find_in_dialog_response(self, request: sip.SipMessage) -> _InDialogResponse | None:
        for cached in reversed(self._in_dialog_responses):
            if self._same_in_dialog_transaction(request, cached.request):
                return cached
        return None

    def _remember_in_dialog_response(
        self,
        request: sip.SipMessage,
        status: int,
        reason: str,
        *,
        extra_headers: tuple[tuple[str, str], ...] = (),
        body: bytes = b"",
    ) -> None:
        self._in_dialog_responses = [
            cached
            for cached in self._in_dialog_responses
            if not self._same_in_dialog_transaction(request, cached.request)
        ]
        self._in_dialog_responses.append(
            _InDialogResponse(request, int(status), str(reason), extra_headers, body)
        )
        del self._in_dialog_responses[:-16]

    @staticmethod
    def _same_dialog_media(previous: SipDialog, updated: SipDialog) -> bool:
        return bool(
            previous.send_format.wire_token() == updated.send_format.wire_token()
            and previous.recv_format.wire_token() == updated.recv_format.wire_token()
            and previous.remote_rtp_host == updated.remote_rtp_host
            and previous.remote_rtp_port == updated.remote_rtp_port
            and previous.remote_audio_direction == updated.remote_audio_direction
            and previous.remote_audio_connection_held
            == updated.remote_audio_connection_held
            and previous.video_format == updated.video_format
            and previous.local_video_format == updated.local_video_format
            and previous.remote_video_rtp_host == updated.remote_video_rtp_host
            and previous.remote_video_rtp_port == updated.remote_video_rtp_port
            and previous.remote_video_rtcp_host == updated.remote_video_rtcp_host
            and previous.remote_video_rtcp_port == updated.remote_video_rtcp_port
            and previous.remote_video_rtcp_mux == updated.remote_video_rtcp_mux
            and previous.remote_video_connection_held
            == updated.remote_video_connection_held
        )

    def _answer_remote_offer(
        self,
        request: sip.SipMessage,
        *,
        local_offer_sdp: str = "",
        current: SipDialog | None = None,
    ) -> tuple[SipDialog, str] | None:
        """Build the media replacement for a remote offer or delayed answer."""

        current = current or self.dialog
        if current is None:
            return None
        try:
            selected = (
                sdp.negotiate_answer_directional(
                    request.body,
                    self.supported_send_formats,
                    self.supported_recv_formats,
                    local_offer_sdp=local_offer_sdp,
                    allow_dahua_pcm=self.include_dahua_pcm,
                )
                if local_offer_sdp
                else sdp.negotiate_directional(
                    request.body,
                    self.supported_send_formats,
                    self.supported_recv_formats,
                    allow_dahua_pcm=self.include_dahua_pcm,
                )
            )
            if selected is None:
                return None
            parsed = sdp.parse_sdp(request.body)
            accepted_video = tuple(
                dict.fromkeys(
                    (
                        *(fmt.encoding for fmt in self.video_formats),
                        *(("H264", "VP8", "JPEG") if self.allow_video_transcoding else ()),
                    )
                )
            )
            video_directional = (
                (
                    sdp.negotiate_video_answer_directional(
                        request.body,
                        tuple(sdp.offered_video_formats(local_offer_sdp)),
                    )
                    if local_offer_sdp
                    else sdp.negotiate_video_offer_directional(
                        request.body,
                        local_formats=self.video_formats,
                        accepted_encodings=accepted_video,
                        prefer_browser_send=self.video_direction
                        in {"sendonly", "sendrecv"},
                        allow_passthrough_fallback=self.allow_video_transcoding,
                    )
                )
                if accepted_video and self.local_video_rtp_port
                else None
            )
            video = video_directional.send if video_directional is not None else None
            local_video = (
                video_directional.recv if video_directional is not None else None
            )
            video_answer = (
                video_directional.answer_format
                if video_directional is not None
                else None
            )
            remote_video = sdp.parse_video_sdp(request.body) if video is not None else None
            remote_video_target = sdp.RemoteMediaTarget.from_section(
                remote_video,
                rtcp_mux=False,
            )
            local_video_direction = (
                sdp.constrained_video_direction(
                    video.direction,
                    allow_send=(
                        self.video_direction in {"sendonly", "sendrecv"}
                        and (self.generic_video_relay or sdp.browser_video_send_supported(video))
                        and not bool(
                            remote_video and remote_video["connection_held"]
                        )
                    ),
                    allow_receive=self.video_direction in {"recvonly", "sendrecv"},
                )
                if video is not None
                else "inactive"
            )
            dtmf_formats = sdp.offered_dtmf_formats(request.body)
            dtmf_direction = (
                sdp.negotiate_dtmf_answer_directional(
                    request.body, local_offer_sdp
                )
                if local_offer_sdp
                else None
            )
            answer = local_offer_sdp or sdp.build_answer_directional(
                self.local_ip,
                self.local_ip,
                self.local_rtp_port,
                selected.send,
                selected.recv,
                dtmf=dtmf_formats[0] if dtmf_formats else None,
                remote_sdp=request.body,
                video_port=(self.local_video_rtp_port if video is not None else 0),
                video_format=video_answer,
                video_direction=local_video_direction,
            )
            session_id = int(current.local_sdp_session_id or self._sdp_session_id)
            session_version = int(current.local_sdp_session_version)
            answer = sdp.rewrite_sdp_origin(answer, session_id, session_version)
            if not local_offer_sdp and sdp.sdp_description_changed(
                current.local_sdp_body, answer
            ):
                session_version += 1
                answer = sdp.rewrite_sdp_origin(
                    answer, session_id, session_version
                )
            contact = request.header("Contact")
            remote_target = current.remote_target_uri
            if contact:
                remote_target = str(sip.parse_sip_uri(contact))
            updated = replace(
                current,
                remote_rtp_host=str(parsed["connection_ip"]),
                remote_rtp_port=int(parsed["media_port"]),
                send_format=selected.send,
                recv_format=selected.recv,
                remote_target_uri=remote_target,
                dtmf_payload_type=(
                    dtmf_direction.recv.payload_type
                    if dtmf_direction is not None
                    else dtmf_formats[0].payload_type
                    if dtmf_formats
                    else None
                ),
                dtmf_clock_rate=(
                    dtmf_direction.recv.sample_rate
                    if dtmf_direction is not None
                    else dtmf_formats[0].sample_rate
                    if dtmf_formats
                    else 8000
                ),
                dtmf_events=(
                    dtmf_direction.recv.events
                    if dtmf_direction is not None
                    else dtmf_formats[0].events
                    if dtmf_formats
                    else frozenset()
                ),
                send_dtmf_payload_type=(
                    dtmf_direction.send.payload_type
                    if dtmf_direction is not None
                    else dtmf_formats[0].payload_type
                    if dtmf_formats
                    else None
                ),
                send_dtmf_clock_rate=(
                    dtmf_direction.send.sample_rate
                    if dtmf_direction is not None
                    else dtmf_formats[0].sample_rate
                    if dtmf_formats
                    else None
                ),
                send_dtmf_events=(
                    dtmf_direction.send.events
                    if dtmf_direction is not None
                    else dtmf_formats[0].events
                    if dtmf_formats
                    else None
                ),
                remote_audio_direction=str(parsed["direction"]),
                local_audio_direction=sdp.local_direction_for_offer(
                    parsed["direction"],
                    remote_connection_held=bool(parsed["connection_held"]),
                ),
                remote_audio_connection_held=bool(parsed["connection_held"]),
                video_format=video,
                local_video_format=local_video,
                local_video_rtp_port=(self.local_video_rtp_port if video is not None else 0),
                local_video_direction=local_video_direction,
                local_sdp_session_id=session_id,
                local_sdp_session_version=session_version,
                local_sdp_body=answer,
                **remote_video_target.as_remote_video_fields(),
            )
        except (TypeError, ValueError, sdp.SdpError, sip.SipError):
            return None
        return updated, answer

    def _transport_failure(self, err: BaseException, target: str, remote_host: str, remote_sip_port: int) -> str:
        self._invite_transaction_active = False
        sip.mark_sip_event(self, "TRANSPORT_ERROR", 0, str(err))
        _LOGGER.info(
            "SIP transport unreachable target=%s host=%s:%s transport=%s error=%s",
            target,
            remote_host,
            remote_sip_port,
            self.signaling_transport,
            err,
        )
        return "transport_unreachable"

    @staticmethod
    def _retry_after_delay(message: sip.SipMessage) -> float | None:
        """Return one bounded Retry-After delay accepted for an INVITE retry."""

        if message.status_code not in {500, 503}:
            return None
        match = re.match(r"\s*(\d+)", message.header("Retry-After"))
        if match is None:
            return None
        return min(
            max(float(match.group(1)), _MIN_INVITE_RETRY_AFTER),
            _MAX_INVITE_RETRY_AFTER,
        )

    def _build_pending_invite(self) -> bytes:
        """Build the current initial-dialog INVITE transaction request."""

        headers = sip.dialog_headers(
            request_uri=self._pending_request_uri,
            local_uri=self._pending_local_uri,
            remote_uri=self._pending_remote_uri,
            dialog=self.dialog_ids,
            method="INVITE",
            contact_uri=self._pending_local_uri,
            content_type=("application/sdp" if self._pending_invite_body else None),
            transport=self.signaling_transport,
            local_display_name=self.local_name,
            remote_display_name=self._pending_target_display,
        )
        caller_name = _sip_header_token(self.local_name)
        dest_name = _sip_header_token(self._pending_target_display)
        caller_route = _sip_header_token(
            sip.parse_sip_uri(self._pending_local_uri).user
        )
        dest_route = _sip_header_token(
            sip.parse_sip_uri(self._pending_request_uri).user
        )
        if caller_name:
            headers.append(("X-Voip-Stack-Caller-Name", caller_name))
        if caller_route:
            headers.append(("X-Voip-Stack-Caller-Route", caller_route))
        if dest_name:
            headers.append(("X-Voip-Stack-Dest-Name", dest_name))
        if dest_route:
            headers.append(("X-Voip-Stack-Dest-Route", dest_route))
        headers.extend(
            (header, value)
            for header, (_challenge, value, _count) in self._pending_invite_auth.items()
        )
        return sip.build_request(
            "INVITE",
            self._pending_request_uri,
            headers,
            self._pending_invite_body,
        )

    def _refresh_pending_invite_authorization(self) -> None:
        """Regenerate qop state when an authenticated INVITE gets a new CSeq."""

        for header, (challenge, _value, count) in tuple(
            self._pending_invite_auth.items()
        ):
            count += 1
            self._pending_invite_auth[header] = (
                challenge,
                build_digest_authorization(
                    challenge_header=challenge,
                    username=self.username,
                    auth_username=self.auth_username,
                    password=self.password,
                    method="INVITE",
                    uri=self._pending_request_uri,
                    nonce_count=count,
                    body=self._pending_invite_body,
                ),
                count,
            )

    async def _retry_invite_after(
        self,
        message: sip.SipMessage,
        *,
        remote_host: str,
        remote_sip_port: int,
    ) -> bytes | None:
        """Start at most one RFC-shaped replacement INVITE transaction."""

        delay = self._retry_after_delay(message)
        if (
            delay is None
            or self._retry_after_used
            or self._cancel_requested
            or self._closing
            or self._closed
        ):
            return None
        self._retry_after_used = True
        _LOGGER.info(
            "SIP %s Retry-After=%s, retrying INVITE once",
            message.status_code,
            message.header("Retry-After"),
        )
        try:
            await asyncio.wait_for(self._invite_abort_event.wait(), timeout=delay)
        except TimeoutError:
            pass
        if (
            self._invite_abort_event.is_set()
            or self._cancel_requested
            or self._closing
            or self._closed
        ):
            return None
        # This is a new client transaction for the same logical dialog attempt:
        # preserve Call-ID/From tag, advance CSeq and replace the Via branch.
        self.dialog_ids.cseq += 1
        self.dialog_ids.branch = sip.make_branch()
        self._invite_cseq = self.dialog_ids.cseq
        self._received_provisional = False
        self._cancel_sent = False
        self._refresh_pending_invite_authorization()
        raw = self._build_pending_invite()
        sip.mark_sip_event(self, "INVITE")
        await self._send_raw(raw, remote_host, int(remote_sip_port))
        return raw

    async def _read_response(
        self,
        timeout: float,
        *,
        network_only: bool = False,
    ) -> tuple[sip.SipMessage, tuple[str, int]] | None:
        if self._deferred_signaling and not network_only:
            return self._deferred_signaling.pop(0)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, float(timeout))
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                if self.signaling_transport in {"TCP", "TLS"}:
                    if self._tcp_reuse_responses is not None:
                        raw = await asyncio.wait_for(self._tcp_reuse_responses.get(), timeout=remaining)
                    else:
                        if self.reader is None:
                            return None
                        raw = await asyncio.wait_for(_read_sip_stream_message(self.reader), timeout=remaining)
                        if raw is None:
                            return None
                    addr = (self._pending_remote_host, self._pending_remote_sip_port)
                else:
                    raw, addr = await asyncio.wait_for(self.queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return None

            if raw == b"\r\n":
                continue
            message = sip.parse_message(raw)
            response_call_id = message.header("Call-ID")
            if response_call_id != self.dialog_ids.call_id:
                _LOGGER.debug(
                    "SIP message ignored for stale call_id=%s current=%s",
                    response_call_id or "(empty)",
                    self.dialog_ids.call_id,
                )
                continue
            return message, addr

    async def _ack_reliable_provisional(
        self,
        response: sip.SipMessage,
        addr: tuple[str, int],
        *,
        process_early_media: bool = True,
    ) -> bool:
        """Acknowledge one in-order RFC 3262 provisional response."""

        if not (
            101 <= int(response.status_code or 0) < 200
            and "100rel" in sip.option_tags(response, "Require")
        ):
            return True
        try:
            rseq = int(response.header("RSeq"))
            invite_cseq = sip.parse_cseq(response.header("CSeq"))
            remote_tag = sip.extract_tag(response.header("To"))
            if not 1 <= rseq <= 0xFFFFFFFF or not remote_tag:
                return False
            sequence_key = (remote_tag, invite_cseq.number)
            previous = self._reliable_rseq.get(sequence_key)
            if previous is not None:
                if rseq == previous:
                    return True
                if rseq != previous + 1:
                    return False
            current = self.dialog or self.early_dialogs.get(remote_tag)
            local_uri = (
                current.local_uri if current is not None else self._pending_local_uri
            )
            remote_uri = (
                current.remote_uri if current is not None else self._pending_remote_uri
            )
            remote_target = sip.contact_target_uri(response) or (
                current.remote_target_uri if current is not None else remote_uri
            )
            route_set = sip.record_route_set(response, reverse=True)
            request = build_dialog_request(
                "PRACK",
                call_id=self.dialog_ids.call_id,
                local_tag=self.dialog_ids.local_tag,
                remote_tag=remote_tag,
                cseq=self._next_dialog_cseq(),
                local_uri=local_uri,
                remote_uri=remote_uri,
                remote_target_uri=remote_target,
                route_set=route_set,
                contact_uri=local_uri,
                transport=self.signaling_transport,
                local_display_name=self.local_name,
                remote_display_name=self._pending_target_display,
                extra_headers=(
                    (
                        "RAck",
                        f"{rseq} {invite_cseq.number} {invite_cseq.method}",
                    ),
                ),
            )
            next_host, next_port = self._dialog_next_hop(
                request.routing.next_hop_uri,
                addr[0],
                addr[1],
            )
        except (TypeError, ValueError, sip.SipError):
            return False
        if process_early_media and response.body and not self._commit_200_ok(
            response,
            self._pending_target,
            self._pending_remote_host or addr[0],
            self._pending_remote_sip_port,
            self._pending_request_uri,
            self._pending_local_uri,
            self._pending_remote_uri,
            provisional=True,
        ):
            return False
        try:
            prack_response = await async_run_dialog_request_transaction(
                send=lambda: self._send_dialog_request(
                    request.raw, next_host, next_port
                ),
                read=lambda timeout: self._read_response(
                    timeout, network_only=True
                ),
                matches=lambda message: sip.response_matches_dialog_transaction(
                    message, request.ids, "PRACK"
                ),
                active=lambda: not self._closing and not self._closed,
                transport=self.signaling_transport,
                timeout=SIP_TIMER_B,
                on_unmatched=lambda message, source: self._deferred_signaling.append(
                    (message, source)
                ),
                on_sent=lambda: sip.mark_sip_event(self, "PRACK"),
            )
        except (ConnectionError, OSError):
            return False
        status = int(prack_response.status_code or 0) if prack_response else 0
        if not 200 <= status < 300:
            return False
        self._reliable_rseq[sequence_key] = rseq
        return True

    async def invite(
        self,
        *,
        target: str,
        remote_host: str,
        remote_sip_port: int,
        request_uri: str = "",
        target_display_name: str = "",
        timeout: float = 8.0,
        delayed_offer: bool = False,
    ) -> str:
        """Run one owned INVITE transaction that survives caller-task cancellation."""
        if self._closing or self._closed:
            raise RuntimeError("SIP client is already closed")
        if self._invite_task is not None and not self._invite_task.done():
            raise RuntimeError("INVITE transaction already active")
        progress = asyncio.get_running_loop().create_future()
        self._invite_progress = progress
        task = asyncio.create_task(
            self._run_invite(
                target=target,
                remote_host=remote_host,
                remote_sip_port=remote_sip_port,
                request_uri=request_uri,
                target_display_name=target_display_name,
                timeout=timeout,
                delayed_offer=delayed_offer,
            ),
            name=f"voip-sip-client-invite-{self.dialog_ids.call_id}",
        )
        self._invite_task = task

        def completed(done: asyncio.Task[str]) -> None:
            if self._invite_task is done:
                self._invite_transaction = None
            if progress.done():
                return
            if done.cancelled():
                progress.cancel()
                return
            error = done.exception()
            if error is not None:
                progress.set_exception(error)
            else:
                progress.set_result(done.result())

        task.add_done_callback(completed)
        try:
            return await asyncio.shield(progress)
        except asyncio.CancelledError:
            self.request_cancel()
            raise

    async def _run_invite(
        self,
        *,
        target: str,
        remote_host: str,
        remote_sip_port: int,
        request_uri: str = "",
        target_display_name: str = "",
        timeout: float = 8.0,
        delayed_offer: bool = False,
    ) -> str:
        transport_param = (("transport", self.signaling_transport.lower()),)
        request_uri = request_uri or str(sip.SipUri(target, remote_host, int(remote_sip_port), params=transport_param))
        logical_uri = sip.parse_sip_uri(request_uri)
        route_uri = logical_uri
        if self.outbound_proxy:
            proxy = str(self.outbound_proxy).strip()
            route_uri = sip.parse_sip_uri(
                proxy
                if proxy.lower().startswith(("sip:", "sips:"))
                else f"sip:{proxy}"
            )
        elif remote_host != logical_uri.host:
            # The caller supplied an already selected next hop while keeping
            # the logical Request-URI intact, as required for trunks/proxies.
            route_uri = sip.SipUri("", remote_host, int(remote_sip_port))
        self._tls_server_name = route_uri.host
        self._signaling_nominal = (remote_host, int(remote_sip_port))
        try:
            endpoints = (
                ((remote_host, int(remote_sip_port), self.signaling_transport),)
                if self._tcp_reuse_send is not None
                else tuple(
                    endpoint
                    for candidate in await self.target_resolver.resolve(
                        route_uri,
                        transport=self.signaling_transport,
                    )
                    for endpoint in candidate.endpoints()
                )
            )
        except (OSError, RuntimeError, sip.SipError) as err:
            return self._transport_failure(err, target, remote_host, remote_sip_port)
        endpoint_index = -1

        async def select_next_endpoint() -> bool:
            nonlocal endpoint_index
            while endpoint_index + 1 < len(endpoints):
                endpoint_index += 1
                selected_host, selected_port, selected_transport = endpoints[endpoint_index]
                if selected_transport != self.signaling_transport:
                    continue
                try:
                    await self._select_initial_signaling_target(
                        selected_host,
                        selected_port,
                    )
                    return True
                except (ConnectionError, OSError, RuntimeError) as err:
                    _LOGGER.info(
                        "SIP initial target unavailable %s:%s transport=%s error=%s",
                        selected_host,
                        selected_port,
                        selected_transport,
                        err,
                    )
            return False

        if not await select_next_endpoint():
            return self._transport_failure(
                OSError("every resolved SIP target is unreachable"),
                target,
                remote_host,
                remote_sip_port,
            )
        local_uri = str(
            sip.SipUri(
                self.local_uri_user,
                self.local_ip,
                self.local_sip_port,
                params=transport_param,
            )
        )
        remote_uri = request_uri
        self._pending_target = target
        self._pending_target_display = str(target_display_name or target).strip()
        self._pending_remote_host = remote_host
        self._pending_remote_sip_port = int(remote_sip_port)
        self._pending_request_uri = request_uri
        self._pending_local_uri = local_uri
        self._pending_remote_uri = remote_uri
        self._pending_invite_auth.clear()
        self._retry_after_used = False
        self._invite_abort_event.clear()
        self._invite_transaction_active = True
        self._cancel_requested = False
        self._cancel_sent = False
        self._received_provisional = False
        self._reliable_rseq.clear()
        self._deferred_signaling.clear()
        offer = ""
        if not delayed_offer:
            offer = sdp.build_offer_directional(
                self.local_ip,
                self.local_ip,
                self.local_rtp_port,
                self.supported_send_formats,
                self.supported_recv_formats,
                include_common_codecs=self.include_common_codecs,
                include_dahua_pcm=self.include_dahua_pcm,
                allow_directional_payloads=self.allow_directional_audio_payloads,
                video_port=self.local_video_rtp_port,
                video_format=self.video_format,
                video_formats=self.video_formats,
                video_direction=self.video_direction,
            )
            offer = sdp.rewrite_sdp_origin(offer, self._sdp_session_id, 0)
        self._initial_delayed_offer = bool(delayed_offer)
        self._local_sdp_body = offer
        body = offer.encode()
        self._pending_invite_body = body
        self._invite_cseq = self.dialog_ids.cseq
        raw = self._build_pending_invite()

        if (
            self.signaling_transport == "UDP"
            and len(raw) > _SIP_UDP_SAFE_REQUEST_BYTES
            and self._tcp_reuse_send is None
        ):
            # RFC 3261 section 18.1.1 requires requests larger than 1300
            # bytes to use a congestion-controlled transport when the path
            # MTU is unknown. Rebuild every transport-bearing field before
            # connecting so the Request-URI, Via and Contact all describe the
            # TCP transaction that is actually sent.
            if self.transport is not None:
                self.transport.close()
                self.transport = None
                self.protocol = None
            self._resolved_signaling_target = None
            self.signaling_transport = "TCP"
            transport_param = (("transport", "tcp"),)
            logical_uri = _uri_with_transport(logical_uri, "tcp")
            request_uri = str(logical_uri)
            route_uri = logical_uri
            if self.outbound_proxy:
                proxy = str(self.outbound_proxy).strip()
                route_uri = _uri_with_transport(
                    sip.parse_sip_uri(
                        proxy
                        if proxy.lower().startswith(("sip:", "sips:"))
                        else f"sip:{proxy}"
                    ),
                    "tcp",
                )
            elif remote_host != logical_uri.host:
                route_uri = sip.SipUri(
                    "",
                    remote_host,
                    int(remote_sip_port),
                    params=transport_param,
                )
            self._tls_server_name = route_uri.host
            try:
                endpoints = tuple(
                    endpoint
                    for candidate in await self.target_resolver.resolve(
                        route_uri,
                        transport="TCP",
                    )
                    for endpoint in candidate.endpoints()
                )
            except (OSError, RuntimeError, sip.SipError) as err:
                return self._transport_failure(
                    err,
                    target,
                    remote_host,
                    remote_sip_port,
                )
            endpoint_index = -1
            if not await select_next_endpoint():
                return self._transport_failure(
                    OSError("every resolved SIP TCP target is unreachable"),
                    target,
                    remote_host,
                    remote_sip_port,
                )
            local_uri = str(
                sip.SipUri(
                    self.local_uri_user,
                    self.local_ip,
                    self.local_sip_port,
                    params=transport_param,
                )
            )
            remote_uri = request_uri
            self._pending_request_uri = request_uri
            self._pending_local_uri = local_uri
            self._pending_remote_uri = remote_uri
            raw = self._build_pending_invite()
            _LOGGER.info(
                "SIP initial request is %s bytes; using TCP per RFC 3261 section 18.1.1",
                len(raw),
            )

        def refresh_pending_local_uri() -> None:
            nonlocal local_uri, raw
            local_uri = str(
                sip.SipUri(
                    self.local_uri_user,
                    self.local_ip,
                    self.local_sip_port,
                    params=transport_param,
                )
            )
            self._pending_local_uri = local_uri
            raw = self._build_pending_invite()

        sip.mark_sip_event(self, "INVITE")
        while True:
            try:
                await self._send_raw(raw, remote_host, int(remote_sip_port))
                break
            except (ConnectionError, OSError, RuntimeError) as err:
                if not await select_next_endpoint():
                    self._invite_transaction_active = False
                    return self._transport_failure(
                        err,
                        target,
                        remote_host,
                        remote_sip_port,
                    )
                self.dialog_ids.branch = sip.make_branch()
                refresh_pending_local_uri()
        _LOGGER.info(
            "SIP TX INVITE %s@%s:%s offered=[%s]",
            target,
            remote_host,
            remote_sip_port,
            (
                ", ".join(sdp.offered_media_descriptions(body))
                if body
                else "delayed"
            ),
        )
        transaction = SipClientTransaction[
            tuple[sip.SipMessage, tuple[str, int]]
        ](
            transport=self.signaling_transport,
            timeout=timeout,
            t1=SIP_T1,
            t2=SIP_T2,
        )
        self._invite_transaction = transaction
        auth_challenges = DigestChallengeTracker()
        received_provisional = False

        async def failover_transaction() -> bool:
            nonlocal raw, transaction
            if received_provisional:
                return False
            while await select_next_endpoint():
                self.dialog_ids.branch = sip.make_branch()
                refresh_pending_local_uri()
                try:
                    await self._send_raw(raw, remote_host, int(remote_sip_port))
                except (ConnectionError, OSError, RuntimeError):
                    continue
                transaction = SipClientTransaction(
                    transport=self.signaling_transport,
                    timeout=timeout,
                    t1=SIP_T1,
                    t2=SIP_T2,
                )
                self._invite_transaction = transaction
                return True
            return False

        async def _retransmit_invite() -> None:
            await self._send_raw(raw, remote_host, int(remote_sip_port))
            _LOGGER.debug(
                "SIP UDP retransmit INVITE #%d %s@%s:%s",
                transaction.retransmissions + 1,
                target,
                remote_host,
                remote_sip_port,
            )

        while True:
            if self._cancel_requested and received_provisional and not self._cancel_sent:
                self._send_cancel()
            try:
                received = await transaction.receive(
                    self._read_response,
                    _retransmit_invite,
                    retransmit_enabled=not received_provisional,
                )
                if received is None:
                    if not await failover_transaction():
                        return "timeout"
                    continue
                msg, addr = received
            except (ConnectionError, OSError, RuntimeError) as err:
                if not await failover_transaction():
                    return self._transport_failure(
                        err,
                        target,
                        remote_host,
                        remote_sip_port,
                    )
                continue
            except Exception as err:
                _LOGGER.info("SIP RX malformed: %s", err)
                continue
            if not msg.is_response:
                continue
            if not matches_response(
                msg,
                method="INVITE",
                cseq=self._invite_cseq,
                branch=self.dialog_ids.branch,
            ):
                _LOGGER.debug("SIP response ignored for non-active INVITE transaction")
                continue
            sip.mark_sip_event(self, "SIP_RESPONSE", int(msg.status_code or 0), msg.reason)
            _LOGGER.info("SIP RX %s %s from %s:%s", msg.status_code, msg.reason, addr[0], addr[1])
            if msg.status_code is not None and 100 <= msg.status_code < 200:
                received_provisional = True
                self._received_provisional = True
                if not await self._ack_reliable_provisional(msg, addr):
                    self.request_cancel()
                    return "prack_failed"
                if self._cancel_requested and not self._cancel_sent:
                    self._send_cancel()
                if _is_invite_progress_response(msg.status_code):
                    if self._cancel_requested:
                        continue
                    transaction.reset_deadline(60.0)
                    progress = self._invite_progress
                    if progress is not None and not progress.done():
                        progress.set_result("ringing")
                # A peer or controlled test transport can deliver a burst of
                # provisional responses without blocking. Yield so the caller
                # can observe progress or request cancellation promptly.
                await asyncio.sleep(0)
                continue
            if msg.status_code and 200 <= msg.status_code < 300:
                if not self._commit_200_ok(msg, target, remote_host, int(remote_sip_port), request_uri, local_uri, remote_uri):
                    return (
                        "cancelled"
                        if self._closing or self._closed
                        else "media_incompatible"
                    )
                if self._cancel_requested:
                    await self._terminate_confirmed_dialog()
                    return "cancelled"
                return "in_call"
            if msg.status_code in {401, 407} and self.password:
                self._send_invite_error_ack(msg, addr[0], addr[1])
                if self._cancel_requested:
                    self._invite_transaction_active = False
                    return "cancelled"
                try:
                    auth_header, challenge, auth_value = auth_challenges.authorize(
                        msg,
                        username=self.username,
                        auth_username=self.auth_username,
                        password=self.password,
                        method="INVITE",
                        uri=request_uri,
                        nonce_count=1,
                        body=self._pending_invite_body,
                    )
                except Exception as err:
                    _LOGGER.info("SIP digest auth failed to build INVITE response: %s", err)
                    return sip.sip_failure_reason(msg.status_code)
                self.dialog_ids.cseq += 1
                self.dialog_ids.branch = sip.make_branch()
                self._invite_cseq = self.dialog_ids.cseq
                self._pending_invite_auth[auth_header] = (
                    challenge,
                    auth_value,
                    1,
                )
                raw = self._build_pending_invite()
                sip.mark_sip_event(self, "INVITE")
                try:
                    await self._send_raw(raw, remote_host, int(remote_sip_port))
                except (ConnectionError, OSError, RuntimeError) as err:
                    return self._transport_failure(err, target, remote_host, remote_sip_port)
                transaction.restart_retransmissions()
                received_provisional = False
                self._reliable_rseq.clear()
                continue
            if msg.status_code and msg.status_code >= 300:
                self._send_invite_error_ack(msg, addr[0], addr[1])
                try:
                    retry_raw = await self._retry_invite_after(
                        msg,
                        remote_host=remote_host,
                        remote_sip_port=int(remote_sip_port),
                    )
                except (ConnectionError, OSError, RuntimeError) as err:
                    return self._transport_failure(
                        err, target, remote_host, remote_sip_port
                    )
                if retry_raw is not None:
                    raw = retry_raw
                    transaction.restart_retransmissions()
                    received_provisional = False
                    self._reliable_rseq.clear()
                    continue
                self._invite_transaction_active = False
                if self._cancel_requested or self._closing or self._closed:
                    return "cancelled"
                return _sip_decline_reason(msg) or sip.sip_failure_reason(msg.status_code)

    async def wait_for_final(self, timeout: float = 60.0) -> str:
        """Await the final result from the sole INVITE transaction owner."""

        if self._closing or self._closed:
            return "cancelled"
        if self.dialog is not None:
            return "in_call"
        task = self._invite_task
        if task is None:
            raise RuntimeError("no INVITE transaction owner is active")
        transaction = self._invite_transaction
        if transaction is not None:
            transaction.reset_deadline(timeout)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except TimeoutError:
            self.request_cancel()
            return "timeout"
        except asyncio.CancelledError:
            self.request_cancel()
            raise

    async def _handle_dialog_media_request(
        self,
        request: sip.SipMessage,
        host: str,
        port: int,
    ) -> str | None:
        """Answer one remote re-INVITE/UPDATE and preserve the old session on failure."""

        method = str(request.method or "").upper()
        if not self._request_matches_dialog(request, host, method):
            self._send_response_to_request(
                request, host, port, 481, "Call/Transaction Does Not Exist"
            )
            return None
        cached = self._find_in_dialog_response(request)
        if cached is not None:
            self._send_response_to_request(
                request,
                host,
                port,
                cached.status,
                cached.reason,
                extra_headers=cached.extra_headers,
                body=cached.body,
            )
            return None
        try:
            request_cseq = sip.parse_cseq(request.header("CSeq"))
        except (TypeError, ValueError, sip.SipError):
            self._send_response_to_request(request, host, port, 400, "Bad Request")
            return None
        try:
            requested_timer = sip.negotiate_uas_session_timer(request)
        except sip.SipSessionIntervalTooSmall as err:
            self._send_response_to_request(
                request,
                host,
                port,
                422,
                "Session Interval Too Small",
                extra_headers=(("Min-SE", str(err.minimum)),),
            )
            return None
        except sip.SipError:
            self._send_response_to_request(request, host, port, 400, "Bad Request")
            return None
        if request_cseq.number <= self._remote_cseq:
            self._send_response_to_request(
                request,
                host,
                port,
                500,
                "Server Internal Error",
                extra_headers=(("Retry-After", "1"),),
            )
            return None

        try:
            refreshed_remote_target = sip.contact_target_uri(request)
        except sip.SipError:
            self._send_response_to_request(request, host, port, 400, "Bad Request")
            return None

        status = 0
        reason = ""
        body = b""
        updated: SipDialog | None = None
        commit: DialogMediaCommit | None = None
        rollback: DialogMediaCommit | None = None
        if not request.body:
            if method == "UPDATE":
                status = 200
                reason = "OK"
            elif self.dialog is None or not self.dialog.local_sdp_body:
                status = 488
                reason = "Not Acceptable Here"
            else:
                self._send_response_to_request(request, host, port, 100, "Trying")
                next_version = self.dialog.local_sdp_session_version + 1
                offer = sdp.rewrite_sdp_origin(
                    self.dialog.local_sdp_body,
                    self.dialog.local_sdp_session_id,
                    next_version,
                )
                self._uas_delayed_offer = _DelayedRemoteOffer(
                    request=request,
                    offer_sdp=offer,
                    remote_target_uri=refreshed_remote_target,
                    local_sdp_session_version=next_version,
                )
                status = 200
                reason = "OK"
                body = offer.encode()
        elif request.header("Content-Type").split(";", 1)[0].strip().lower() != "application/sdp":
            status = 415
            reason = "Unsupported Media Type"
        else:
            prepared = self._answer_remote_offer(request)
            if prepared is None or self.dialog is None:
                status = 488
                reason = "Not Acceptable Here"
            else:
                updated, answer = prepared
                if refreshed_remote_target:
                    updated = replace(
                        updated,
                        remote_target_uri=refreshed_remote_target,
                    )
                unchanged = self._same_dialog_media(self.dialog, updated)
                if self.on_media_update is not None:
                    if method == "INVITE":
                        self._send_response_to_request(request, host, port, 100, "Trying")
                    try:
                        prepared_update = await self.on_media_update(
                            self.dialog, updated, method
                        )
                        if isinstance(prepared_update, PreparedDialogMediaUpdate):
                            commit = prepared_update.commit
                            rollback = prepared_update.rollback
                            answer_video_format = (
                                prepared_update.answer_video_format
                            )
                            if prepared_update.answer_video_rtp_port is not None:
                                updated = replace(
                                    updated,
                                    local_video_rtp_port=int(
                                        prepared_update.answer_video_rtp_port
                                    ),
                                )
                            if answer_video_format is not None:
                                video_pair = sdp.video_offer_answer_directional(
                                    updated.video_format,
                                    answer_video_format,
                                )
                                if video_pair is None:
                                    commit = None
                                else:
                                    answer = sdp.build_answer_directional(
                                        self.local_ip,
                                        self.local_ip,
                                        updated.local_rtp_port,
                                        updated.send_format,
                                        updated.recv_format,
                                        dtmf=(
                                            sdp.offered_dtmf_formats(request.body)[0]
                                            if sdp.offered_dtmf_formats(request.body)
                                            else None
                                        ),
                                        remote_sdp=request.body,
                                        video_port=updated.local_video_rtp_port,
                                        video_format=answer_video_format,
                                        audio_direction=updated.local_audio_direction,
                                        video_direction=updated.local_video_direction,
                                    )
                                    answer = sdp.rewrite_sdp_origin(
                                        answer,
                                        updated.local_sdp_session_id,
                                        updated.local_sdp_session_version,
                                    )
                                    updated = replace(
                                        updated,
                                        video_format=video_pair.send,
                                        local_video_format=video_pair.recv,
                                        local_sdp_body=answer,
                                    )
                            elif prepared_update.answer_video_rtp_port is not None:
                                answer = sdp.build_answer_directional(
                                    self.local_ip,
                                    self.local_ip,
                                    updated.local_rtp_port,
                                    updated.send_format,
                                    updated.recv_format,
                                    dtmf=(
                                        sdp.offered_dtmf_formats(request.body)[0]
                                        if sdp.offered_dtmf_formats(request.body)
                                        else None
                                    ),
                                    remote_sdp=request.body,
                                    video_port=updated.local_video_rtp_port,
                                    video_format=updated.video_format,
                                    audio_direction=updated.local_audio_direction,
                                    video_direction=updated.local_video_direction,
                                )
                                answer = sdp.rewrite_sdp_origin(
                                    answer,
                                    updated.local_sdp_session_id,
                                    updated.local_sdp_session_version,
                                )
                                updated = replace(updated, local_sdp_body=answer)
                        else:
                            commit = prepared_update
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        _LOGGER.exception(
                            "SIP remote media update preparation failed call_id=%s method=%s",
                            self.dialog_ids.call_id,
                            method,
                        )
                        status = 500
                        reason = "Server Internal Error"
                    else:
                        if commit is None and not unchanged:
                            status = 488
                            reason = "Not Acceptable Here"
                        else:
                            status = 200
                            reason = "OK"
                            body = answer.encode("utf-8")
                elif unchanged:
                    status = 200
                    reason = "OK"
                    body = answer.encode("utf-8")
                else:
                    status = 488
                    reason = "Not Acceptable Here"

        if status == 415:
            extra_headers = (("Accept", "application/sdp"),)
        elif status == 488 and self.on_media_update is None:
            extra_headers = ((
                "Warning",
                f'399 {self.local_ip} "Session renegotiation is not supported by the active media owner"',
            ),)
        else:
            extra_headers = ()
        sent = self._send_response_to_request(
            request,
            host,
            port,
            status,
            reason,
            extra_headers=extra_headers,
            body=body if 200 <= status < 300 else b"",
        )
        if not sent:
            self._uas_delayed_offer = None
            if rollback is not None:
                await rollback()
            return "transport_unreachable"
        if self._uas_delayed_offer is not None and self.dialog is not None:
            self.dialog.local_sdp_session_version = (
                self._uas_delayed_offer.local_sdp_session_version
            )
            self.dialog.local_sdp_body = self._uas_delayed_offer.offer_sdp
        self._remember_in_dialog_response(
            request,
            status,
            reason,
            extra_headers=extra_headers,
            body=body if 200 <= status < 300 else b"",
        )
        self._remote_cseq = request_cseq.number
        if method == "INVITE" and 200 <= status < 300:
            # Arm before the async media commit: an ACK is allowed to arrive
            # immediately after the 2xx is emitted.
            self._arm_uas_invite_2xx(
                request,
                host,
                port,
                status,
                reason,
                extra_headers=extra_headers,
                body=body,
            )
        if 200 <= status < 300 and updated is not None:
            if not await apply_remote_offer_media(commit, rollback):
                _LOGGER.error(
                    "SIP remote media update commit failed call_id=%s method=%s",
                    self.dialog_ids.call_id,
                    method,
                )
                self._uas_invite_2xx.cancel()
                await self._terminate_confirmed_dialog()
                return "media_update_failed"
            if self._uas_invite_ack_timeout.is_set() or self.dialog is None:
                return "ack_timeout"
            self.dialog = updated
        elif (
            200 <= status < 300
            and refreshed_remote_target
            and self.dialog is not None
            and self._uas_delayed_offer is None
        ):
            self.dialog = replace(
                self.dialog,
                remote_target_uri=refreshed_remote_target,
            )
        if (
            200 <= status < 300
            and requested_timer is not None
            and self.dialog is not None
            and self._uas_delayed_offer is None
        ):
            self.dialog.session_timer.configure(
                requested_timer,
                local_role="uas",
                now=asyncio.get_running_loop().time(),
            )
        if (
            method == "UPDATE"
            and 200 <= status < 300
            and self.dialog is not None
            and self.dialog.peer_supports_from_change
        ):
            try:
                connected_uri = str(sip.parse_sip_uri(request.header("From")))
            except (TypeError, ValueError, sip.SipError):
                connected_uri = ""
            if connected_uri and connected_uri != self.dialog.remote_uri:
                self.dialog = replace(self.dialog, remote_uri=connected_uri)
            connected_name = sip.name_addr_display_name(request.header("From"))
            connected_party = connected_name or sip.name_addr_identity(
                request.header("From")
            )
            if connected_party:
                self._dialog_remote_display_name = connected_name
                self.connected_party = connected_party
                callback = self.on_connected_identity
                if callback is not None:
                    try:
                        callback(connected_party, connected_uri)
                    except Exception as err:  # noqa: BLE001 - observers cannot break the dialog.
                        _LOGGER.warning(
                            "SIP connected identity callback failed: %s",
                            err,
                        )
        return None

    async def _send_in_dialog_request(
        self,
        method: str,
        *,
        extra_headers: tuple[tuple[str, str], ...] = (),
        body: bytes = b"",
        content_type: str = "",
        timeout: float = 8.0,
    ) -> sip.SipMessage | None:
        """Run one serialized non-INVITE client transaction on the dialog."""

        async with self._dialog_writer():
            dialog = self.dialog
            if dialog is None:
                return None
            try:
                request = build_dialog_request(
                    method,
                    call_id=self.dialog_ids.call_id,
                    local_tag=self.dialog_ids.local_tag,
                    remote_tag=self.dialog_ids.remote_tag,
                    cseq=self._next_dialog_cseq(),
                    local_uri=dialog.local_uri,
                    remote_uri=dialog.remote_uri,
                    remote_target_uri=dialog.remote_target_uri,
                    route_set=dialog.route_set,
                    contact_uri=dialog.local_uri,
                    transport=self.signaling_transport,
                    local_display_name=self.local_name,
                    remote_display_name=self._dialog_remote_display_name,
                    extra_headers=extra_headers,
                    content_type=content_type,
                    body=body,
                )
                next_host, next_port = self._dialog_next_hop(
                    request.routing.next_hop_uri,
                    dialog.remote_host,
                    dialog.remote_sip_port,
                )
            except (TypeError, ValueError, sip.SipError):
                return None
            try:
                return await self._run_built_dialog_request(
                    request,
                    next_host,
                    next_port,
                    method=method,
                    timeout=timeout,
                    active=lambda: self._dialog_is_current(dialog),
                )
            except ConnectionAbortedError:
                raise
            except (ConnectionError, OSError):
                return None
        return None

    async def _run_built_dialog_request(
        self,
        request,
        host: str,
        port: int,
        *,
        method: str,
        timeout: float,
        active: Callable[[], bool],
    ) -> sip.SipMessage | None:
        """Run one already-routed request through the common transaction owner."""

        async def handle_request(
            message: sip.SipMessage,
            addr: tuple[str, int],
        ) -> None:
            terminal = await self._dispatch_in_dialog_request(message, addr)
            if terminal is not None:
                raise ConnectionAbortedError(terminal)

        return await async_run_dialog_request_transaction(
            send=lambda: self._send_dialog_request(request.raw, host, port),
            read=self._read_response,
            matches=lambda response: sip.response_matches_dialog_transaction(
                response, request.ids, method
            ),
            active=active,
            transport=self.signaling_transport,
            timeout=timeout,
            on_request=handle_request,
            on_unmatched=lambda message, _source: (
                self._ack_retransmitted_invite_2xx(message)
            ),
            on_sent=lambda: sip.mark_sip_event(self, method),
        )

    @contextlib.asynccontextmanager
    async def _dialog_writer(self) -> AsyncIterator[None]:
        """Wake the dialog reader and serialize one outbound transaction."""

        self._request_dialog_writer()
        try:
            async with self._dialog_read_lock:
                yield
        finally:
            self._release_dialog_writer()

    def _request_dialog_writer(self) -> None:
        self._dialog_writer_count += 1
        self._dialog_writer_requested.set()

    def _release_dialog_writer(self) -> None:
        self._dialog_writer_count -= 1
        if self._dialog_writer_count == 0:
            self._dialog_writer_requested.clear()

    async def request_video_keyframe(self, *, timeout: float = 3.0) -> bool:
        """Request a full intra frame with RFC 5168 media control."""

        response = await self._send_in_dialog_request(
            "INFO",
            body=sip.RFC5168_PICTURE_FAST_UPDATE_BODY,
            content_type="application/media_control+xml",
            timeout=timeout,
        )
        return bool(
            response is not None
            and 200 <= int(response.status_code or 0) < 300
        )

    def _dialog_is_current(self, expected: SipDialog) -> bool:
        current = self.dialog
        return bool(
            current is not None
            and (
                current is expected
                or (
                    current.call_id == expected.call_id
                    and current.remote_tag == expected.remote_tag
                )
            )
        )

    async def refer(
        self,
        target: sip_transfer.SipReferTarget,
        *,
        timeout: float = 30.0,
    ) -> SipTransferResult:
        """Request a blind or attended transfer and await its NOTIFY outcome."""

        if self.dialog is None or self._refer_notifications is not None:
            return SipTransferResult(False, 0, "unavailable")
        notifications: asyncio.Queue[tuple[int, bool]] = asyncio.Queue(maxsize=8)
        self._refer_notifications = notifications
        try:
            response = await self._send_in_dialog_request(
                "REFER",
                extra_headers=(
                    ("Refer-To", target.as_header()),
                    ("Referred-By", sip.format_name_addr(self.dialog.local_uri)),
                ),
                timeout=min(float(timeout), 8.0),
            )
            status = int(response.status_code or 0) if response is not None else 0
            if not 200 <= status < 300:
                return SipTransferResult(False, status, "rejected")
            deadline = asyncio.get_running_loop().time() + float(timeout)
            while True:
                while not notifications.empty():
                    notify_status, terminated = notifications.get_nowait()
                    if notify_status >= 200:
                        return SipTransferResult(
                            200 <= notify_status < 300,
                            notify_status,
                            "completed" if notify_status < 300 else "failed",
                        )
                    if terminated:
                        return SipTransferResult(False, notify_status, "terminated")
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return SipTransferResult(False, 0, "timeout")
                async with self._dialog_read_lock:
                    received = await self._read_response(remaining)
                    if received is None:
                        return SipTransferResult(False, 0, "timeout")
                    message, addr = received
                    if message.is_request:
                        terminal = await self._dispatch_in_dialog_request(message, addr)
                        if terminal is not None:
                            return SipTransferResult(False, 0, terminal)
                    elif self._ack_retransmitted_invite_2xx(message):
                        continue
        finally:
            if self._refer_notifications is notifications:
                self._refer_notifications = None

    async def wait_for_dialog_termination(self, timeout: float | None = None) -> str:
        """Wait for a remote BYE on a confirmed outbound dialog.

        Outbound HA-originated calls keep their SIP client alive after 200 OK so
        the same signaling path can receive the peer's BYE. When that happens we
        must acknowledge it and let the owner remove this client from its active
        dialog registry; otherwise the HA endpoint remains falsely busy.
        """
        if self.dialog is None:
            return "not_in_call"
        deadline = None if timeout is None else asyncio.get_running_loop().time() + float(timeout)
        while True:
            dialog = self.dialog
            if dialog is None:
                return "remote_hangup"
            now = asyncio.get_running_loop().time()
            timer = getattr(dialog, "session_timer", sip.SipSessionTimer())
            timer_driver = SessionTimerDriver(
                timer,
                self._send_in_dialog_request,
                "uac",
                asyncio.get_running_loop().time,
            )
            if timer_driver.deadline and now >= timer_driver.deadline:
                try:
                    refresh_result = await timer_driver.advance()
                except ConnectionAbortedError as err:
                    return str(err) or "remote_hangup"
                if refresh_result == "refreshed":
                    continue
                await self._terminate_confirmed_dialog()
                return refresh_result
            read_task: asyncio.Task[
                tuple[sip.SipMessage, tuple[str, int]] | None
            ] | None = None
            ack_timeout_task: asyncio.Task[bool] | None = None
            offer_request_task: asyncio.Task[bool] | None = None
            reader_acquired = False
            try:
                await self._dialog_read_lock.acquire()
                reader_acquired = True
                if self.dialog is None:
                    return "remote_hangup"
                if self._dialog_writer_requested.is_set():
                    continue
                wait_timeout = 3600.0
                timer_at = timer_driver.deadline
                if timer_at:
                    wait_timeout = max(0.05, timer_at - asyncio.get_running_loop().time())
                if deadline is not None:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        return "timeout"
                    wait_timeout = min(wait_timeout, max(0.05, remaining))
                read_task = asyncio.create_task(
                    self._read_response(wait_timeout),
                    name=f"voip-sip-dialog-read-{self.dialog_ids.call_id}",
                )
                ack_timeout_task = asyncio.create_task(
                    self._uas_invite_ack_timeout.wait(),
                    name=f"voip-sip-dialog-ack-timeout-{self.dialog_ids.call_id}",
                )
                offer_request_task = asyncio.create_task(
                    self._dialog_writer_requested.wait(),
                    name=f"voip-sip-dialog-offer-{self.dialog_ids.call_id}",
                )
                done, _pending = await asyncio.wait(
                    {read_task, ack_timeout_task, offer_request_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if ack_timeout_task in done and ack_timeout_task.result():
                    if read_task in done:
                        read_task.result()
                    return "ack_timeout"
                if offer_request_task in done and read_task not in done:
                    continue
                received = read_task.result()
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                if deadline is not None:
                    return "timeout"
                continue
            except Exception as err:
                _LOGGER.debug("SIP dialog termination wait ignored malformed message: %s", err)
                continue
            finally:
                child_tasks = tuple(
                    task
                    for task in (
                        read_task,
                        ack_timeout_task,
                        offer_request_task,
                    )
                    if isinstance(task, asyncio.Task)
                )
                for task in child_tasks:
                    if not task.done():
                        task.cancel()
                if child_tasks:
                    cleanup = asyncio.gather(*child_tasks, return_exceptions=True)
                    await async_wait_for_cleanup(cleanup)
                if reader_acquired:
                    self._dialog_read_lock.release()
            if received is None:
                if (
                    deadline is not None
                    and asyncio.get_running_loop().time() >= deadline
                ):
                    return "timeout"
                continue
            msg, addr = received
            if msg.is_response:
                if msg.status_code is not None:
                    sip.mark_sip_event(self, "SIP_RESPONSE", int(msg.status_code), msg.reason)
                    _LOGGER.info("SIP RX %s %s from %s:%s", msg.status_code, msg.reason, addr[0], addr[1])
                    self._ack_retransmitted_invite_2xx(msg)
                continue
            terminal = await self._dispatch_in_dialog_request(msg, addr)
            if terminal is not None:
                return terminal

    async def _dispatch_in_dialog_request(
        self,
        msg: sip.SipMessage,
        addr: tuple[str, int],
        *,
        local_offer_pending: bool = False,
    ) -> str | None:
        """Dispatch one sequential request without creating a second reader."""

        if msg.method == "BYE":
            _LOGGER.info("SIP RX BYE from %s:%s", addr[0], addr[1])
            if not self._request_matches_dialog(msg, addr[0], "BYE"):
                self._send_response_to_request(
                    msg, addr[0], addr[1], 481, "Call/Transaction Does Not Exist"
                )
                return None
            try:
                bye_cseq = sip.parse_cseq(msg.header("CSeq"))
            except (TypeError, ValueError, sip.SipError):
                self._send_response_to_request(
                    msg, addr[0], addr[1], 400, "Bad Request"
                )
                return None
            if bye_cseq.number <= self._remote_cseq:
                self._send_response_to_request(
                    msg,
                    addr[0],
                    addr[1],
                    500,
                    "Server Internal Error",
                    extra_headers=(("Retry-After", "1"),),
                )
                return None
            self._uas_invite_2xx.cancel()
            self._uas_delayed_offer = None
            self._send_response_to_request(msg, addr[0], addr[1], 200, "OK")
            self._remote_cseq = bye_cseq.number
            self.dialog = None
            return "remote_hangup"
        if msg.method == "CANCEL":
            self._send_response_to_request(
                msg, addr[0], addr[1], 481, "Call/Transaction Does Not Exist"
            )
            return None
        if msg.method == "ACK":
            if self._acknowledges_uas_invite_2xx(msg, addr[0]):
                _LOGGER.info(
                    "SIP RX ACK remote re-INVITE call_id=%s",
                    self.dialog_ids.call_id,
                )
                return await self._commit_delayed_offer_ack(msg)
            return None
        if msg.method in {"INVITE", "UPDATE"}:
            if local_offer_pending:
                if not self._request_matches_dialog(msg, addr[0], msg.method):
                    self._send_response_to_request(
                        msg,
                        addr[0],
                        addr[1],
                        481,
                        "Call/Transaction Does Not Exist",
                    )
                    return None
                self._send_response_to_request(
                    msg, addr[0], addr[1], 491, "Request Pending"
                )
                return None
            return await self._handle_dialog_media_request(msg, addr[0], addr[1])
        if msg.method == "REFER":
            self._handle_refer_request(msg, addr)
            return None
        if msg.method == "NOTIFY":
            return self._handle_refer_notify(msg, addr)
        if msg.method in {"INFO", "OPTIONS"}:
            if not self._request_matches_dialog(msg, addr[0], msg.method):
                self._send_response_to_request(
                    msg, addr[0], addr[1], 481, "Call/Transaction Does Not Exist"
                )
                return None
            cached = self._find_in_dialog_response(msg)
            if cached is not None:
                self._send_response_to_request(
                    msg,
                    addr[0],
                    addr[1],
                    cached.status,
                    cached.reason,
                    extra_headers=cached.extra_headers,
                    body=cached.body,
                )
                return None
            try:
                request_cseq = sip.parse_cseq(msg.header("CSeq"))
            except (TypeError, ValueError, sip.SipError):
                self._send_response_to_request(
                    msg, addr[0], addr[1], 400, "Bad Request"
                )
                return None
            if request_cseq.number <= self._remote_cseq:
                self._send_response_to_request(
                    msg,
                    addr[0],
                    addr[1],
                    500,
                    "Server Internal Error",
                    extra_headers=(("Retry-After", "1"),),
                )
                return None
            if msg.method == "INFO":
                from .dtmf import parse_sip_info_digit

                digit = parse_sip_info_digit(msg.header("Content-Type"), msg.body)
                if digit and self.on_info_dtmf is not None:
                    try:
                        self.on_info_dtmf(digit)
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.warning("SIP INFO DTMF callback failed: %s", err)
            self._send_response_to_request(msg, addr[0], addr[1], 200, "OK")
            self._remember_in_dialog_response(msg, 200, "OK")
            self._remote_cseq = request_cseq.number
            return None
        self._send_response_to_request(
            msg, addr[0], addr[1], 405, "Method Not Allowed"
        )
        return None

    def _handle_refer_request(
        self,
        request: sip.SipMessage,
        addr: tuple[str, int],
    ) -> None:
        """Accept one in-dialog REFER and hand it to the session owner."""

        host, port = addr
        if not self._request_matches_dialog(request, host, "REFER"):
            self._send_response_to_request(
                request, host, port, 481, "Call/Transaction Does Not Exist"
            )
            return
        cached = self._find_in_dialog_response(request)
        if cached is not None:
            self._send_response_to_request(
                request,
                host,
                port,
                cached.status,
                cached.reason,
                extra_headers=cached.extra_headers,
                body=cached.body,
            )
            return
        try:
            request_cseq = sip.parse_cseq(request.header("CSeq"))
            target = sip_transfer.parse_refer_to(request.header("Refer-To"))
        except (TypeError, ValueError, sip.SipError):
            self._send_response_to_request(request, host, port, 400, "Bad Request")
            return
        if request_cseq.number <= self._remote_cseq:
            self._send_response_to_request(
                request,
                host,
                port,
                500,
                "Server Internal Error",
                extra_headers=(("Retry-After", "1"),),
            )
            return
        if self.on_refer is None or (
            self._incoming_refer_task is not None
            and not self._incoming_refer_task.done()
        ):
            self._send_response_to_request(request, host, port, 603, "Decline")
            self._remember_in_dialog_response(request, 603, "Decline")
            return
        self._send_response_to_request(request, host, port, 202, "Accepted")
        self._remember_in_dialog_response(request, 202, "Accepted")
        self._remote_cseq = request_cseq.number
        task = asyncio.create_task(
            self._run_incoming_refer(target, request_cseq.number),
            name=f"voip-sip-refer-{self.dialog_ids.call_id}",
        )
        self._incoming_refer_task = task
        task.add_done_callback(self._incoming_refer_done)

    def _incoming_refer_done(self, task: asyncio.Task[None]) -> None:
        if self._incoming_refer_task is task:
            self._incoming_refer_task = None
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            _LOGGER.warning(
                "SIP incoming REFER failed call_id=%s error=%s",
                self.dialog_ids.call_id,
                error,
            )

    async def _run_incoming_refer(
        self,
        target: sip_transfer.SipReferTarget,
        refer_cseq: int,
    ) -> None:
        """Report progress and the final transfer outcome with NOTIFY."""

        event_headers = (
            ("Event", f"refer;id={refer_cseq}"),
            ("Subscription-State", "active;expires=60"),
        )
        progress = await self._send_in_dialog_request(
            "NOTIFY",
            extra_headers=event_headers,
            body=b"SIP/2.0 100 Trying\r\n",
            content_type="message/sipfrag",
        )
        if progress is None or not 200 <= int(progress.status_code or 0) < 300:
            return
        try:
            status = int(await self.on_refer(target)) if self.on_refer else 603
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception(
                "SIP REFER handler failed call_id=%s",
                self.dialog_ids.call_id,
            )
            status = 500
        status = status if 200 <= status <= 699 else 500
        reason = "OK" if status < 300 else "Transfer Failed"
        await self._send_in_dialog_request(
            "NOTIFY",
            extra_headers=(
                ("Event", f"refer;id={refer_cseq}"),
                ("Subscription-State", "terminated;reason=noresource"),
            ),
            body=f"SIP/2.0 {status} {reason}\r\n".encode(),
            content_type="message/sipfrag",
        )

    def _handle_refer_notify(
        self,
        request: sip.SipMessage,
        addr: tuple[str, int],
    ) -> None:
        """Acknowledge one RFC 3515 transfer progress notification."""

        host, port = addr
        if not self._request_matches_dialog(request, host, "NOTIFY"):
            self._send_response_to_request(
                request, host, port, 481, "Subscription Does Not Exist"
            )
            return None
        cached = self._find_in_dialog_response(request)
        if cached is not None:
            self._send_response_to_request(
                request,
                host,
                port,
                cached.status,
                cached.reason,
                extra_headers=cached.extra_headers,
                body=cached.body,
            )
            return None
        event = request.header("Event").split(";", 1)[0].strip().casefold()
        content_type = request.header("Content-Type").split(";", 1)[0].strip().casefold()
        if self._refer_notifications is None or event != "refer":
            self._send_response_to_request(request, host, port, 489, "Bad Event")
            return None
        if content_type != "message/sipfrag":
            self._send_response_to_request(request, host, port, 415, "Unsupported Media Type")
            return None
        try:
            request_cseq = sip.parse_cseq(request.header("CSeq"))
            status = sip_transfer.parse_sipfrag_status(request.body)
        except (TypeError, ValueError, sip.SipError):
            self._send_response_to_request(request, host, port, 400, "Bad Request")
            return None
        if request_cseq.number <= self._remote_cseq:
            self._send_response_to_request(
                request,
                host,
                port,
                500,
                "Server Internal Error",
                extra_headers=(("Retry-After", "1"),),
            )
            return None
        self._send_response_to_request(request, host, port, 200, "OK")
        self._remember_in_dialog_response(request, 200, "OK")
        self._remote_cseq = request_cseq.number
        subscription = request.header("Subscription-State").split(";", 1)[0].strip().casefold()
        item = (status, subscription == "terminated")
        try:
            self._refer_notifications.put_nowait(item)
        except asyncio.QueueFull:
            self._refer_notifications.get_nowait()
            self._refer_notifications.put_nowait(item)
        return None

    def _dialog_candidate_from_answer(
        self,
        current: SipDialog,
        offer: str,
        answer: sip.SipMessage,
        *,
        local_video_rtp_port: int,
        offered_video_formats: tuple[sdp.RtpVideoFormat, ...],
        video_direction: str,
        session_version: int,
    ) -> SipDialog | None:
        """Validate one local re-offer answer without publishing it."""

        try:
            if (
                not answer.body
                or answer.header("Content-Type").split(";", 1)[0].strip().lower()
                != "application/sdp"
            ):
                return None
            sdp.validate_sdp_answer(
                offer,
                answer.body,
                # Some deployed SIP UAs retain their allocated RTP port while
                # answering an m=video 0 removal with a=inactive. The stream
                # is still unambiguously disabled, so normalize that legacy
                # answer at this B2BUA boundary while keeping general SDP
                # answer validation strict.
                allow_inactive_rejected_media_port=local_video_rtp_port == 0,
            )
            audio = sdp.negotiate_answer_directional(
                answer.body,
                [fmt.audio_format for fmt in sdp.offered_pcm_formats(offer)],
                [fmt.audio_format for fmt in sdp.offered_pcm_formats(offer)],
                local_offer_direction=current.local_audio_direction,
                local_offer_sdp=offer,
                allow_dahua_pcm=self.include_dahua_pcm,
            )
            if audio is None:
                return None
            parsed = sdp.parse_sdp(answer.body)
            video_pair = (
                sdp.negotiate_video_answer_directional(
                    answer.body,
                    offered_video_formats,
                )
                if offered_video_formats and local_video_rtp_port > 0
                else None
            )
            video_send = video_pair.send if video_pair is not None else None
            video_recv = video_pair.recv if video_pair is not None else None
            remote_video = (
                sdp.parse_video_sdp(answer.body)
                if video_send is not None
                else None
            )
            if local_video_rtp_port > 0 and offered_video_formats and (
                remote_video is None or int(remote_video["media_port"]) <= 0
            ):
                return None
            video_target = sdp.RemoteMediaTarget.from_section(
                remote_video,
                rtcp_mux=False,
            )
            dtmf_pair = sdp.negotiate_dtmf_answer_directional(
                answer.body,
                offer,
            )
            dtmf_recv = dtmf_pair.recv if dtmf_pair is not None else None
            dtmf_send = dtmf_pair.send if dtmf_pair is not None else None
            remote_target = sip.contact_target_uri(answer) or current.remote_target_uri
            if not remote_target:
                return None
            local_video_direction = (
                sdp.constrained_video_direction(
                    video_send.direction,
                    allow_send=(
                        video_direction in {"sendonly", "sendrecv"}
                        and not bool(
                            remote_video and remote_video["connection_held"]
                        )
                    ),
                    allow_receive=video_direction in {"recvonly", "sendrecv"},
                )
                if video_send is not None
                else "inactive"
            )
            return replace(
                current,
                remote_rtp_host=str(parsed["connection_ip"]),
                remote_rtp_port=int(parsed["media_port"]),
                send_format=audio.send,
                recv_format=audio.recv,
                remote_target_uri=remote_target,
                dtmf_payload_type=(
                    dtmf_recv.payload_type if dtmf_recv is not None else None
                ),
                dtmf_clock_rate=(
                    dtmf_recv.sample_rate if dtmf_recv is not None else 8000
                ),
                dtmf_events=(
                    dtmf_recv.events if dtmf_recv is not None else frozenset()
                ),
                send_dtmf_payload_type=(
                    dtmf_send.payload_type if dtmf_send is not None else None
                ),
                send_dtmf_clock_rate=(
                    dtmf_send.sample_rate if dtmf_send is not None else None
                ),
                send_dtmf_events=(
                    dtmf_send.events if dtmf_send is not None else None
                ),
                remote_audio_direction=str(parsed["direction"]),
                local_audio_direction=sdp.local_direction_for_offer(
                    parsed["direction"],
                    remote_connection_held=bool(parsed["connection_held"]),
                ),
                remote_audio_connection_held=bool(parsed["connection_held"]),
                video_format=video_send,
                local_video_format=video_recv,
                local_video_rtp_port=(
                    local_video_rtp_port if video_send is not None else 0
                ),
                local_video_direction=local_video_direction,
                local_sdp_session_version=session_version,
                local_sdp_body=offer,
                **video_target.as_remote_video_fields(),
            )
        except (TypeError, ValueError, sdp.SdpError, sip.SipError):
            return None

    async def async_prepare_video_reinvite(
        self,
        *,
        local_video_rtp_port: int,
        video_formats: tuple[sdp.RtpVideoFormat, ...],
        video_direction: str = "sendrecv",
        timeout: float = 8.0,
    ) -> SipDialog | None:
        """Change video with one serialized in-dialog offer, without publishing it."""

        async with self._local_offer_lock:
            current = self.dialog
            offered_video = tuple(video_formats)
            local_video_port = int(local_video_rtp_port)
            removing_video = local_video_port == 0
            if (
                current is None
                or self._prepared_reinvite is not None
                or self._closing
                or self._closed
                or not offered_video
                or local_video_port < 0
                or (removing_video and current.video_format is None)
            ):
                return None
            try:
                session_id = int(
                    current.local_sdp_session_id or self._sdp_session_id
                )
                session_version = int(current.local_sdp_session_version)
                current_audio_formats = tuple(
                    sdp.offered_pcm_formats(
                        current.local_sdp_body,
                        allow_dahua_pcm=self.include_dahua_pcm,
                    )
                )
                if not current_audio_formats:
                    return None
                offer = sdp.build_offer_directional(
                    self.local_ip,
                    self.local_ip,
                    self.local_rtp_port,
                    [current.send_format.audio_format],
                    [current.recv_format.audio_format],
                    video_port=local_video_port,
                    video_formats=offered_video,
                    audio_direction=current.local_audio_direction,
                    video_direction=(
                        "inactive" if removing_video else video_direction
                    ),
                    # An in-dialog offer preserves the negotiated RTP wire
                    # codec and payload type. Converting PCMA, PCMU, G.722 or
                    # Opus back through their decoded PCM shape would turn
                    # them into L16 and can make a standards-compliant peer
                    # reject an otherwise valid video-only session update.
                    audio_rtp_formats=current_audio_formats,
                )
                offer = sdp.rewrite_sdp_origin(
                    offer, session_id, session_version
                )
                if sdp.sdp_description_changed(current.local_sdp_body, offer):
                    session_version += 1
                    offer = sdp.rewrite_sdp_origin(
                        offer, session_id, session_version
                    )
                request = build_dialog_request(
                    "INVITE",
                    call_id=self.dialog_ids.call_id,
                    local_tag=self.dialog_ids.local_tag,
                    remote_tag=self.dialog_ids.remote_tag,
                    cseq=self._next_dialog_cseq(),
                    local_uri=current.local_uri,
                    remote_uri=current.remote_uri,
                    remote_target_uri=(
                        current.remote_target_uri or current.remote_uri
                    ),
                    route_set=current.route_set,
                    contact_uri=current.local_uri,
                    transport=self.signaling_transport,
                    local_display_name=self.local_name,
                    remote_display_name=self._dialog_remote_display_name,
                    content_type="application/sdp",
                    body=offer.encode(),
                )
                next_host, next_port = self._dialog_next_hop(
                    request.routing.next_hop_uri,
                    current.remote_host,
                    current.remote_sip_port,
                )
            except (TypeError, ValueError, sdp.SdpError, sip.SipError):
                _LOGGER.exception(
                    "SIP local video re-INVITE could not be prepared call_id=%s",
                    self.dialog_ids.call_id,
                )
                return None

            self._request_dialog_writer()
            try:
                await self._dialog_read_lock.acquire()
            except BaseException:
                self._release_dialog_writer()
                raise
            try:
                if self.dialog is not current:
                    return None
                _LOGGER.info(
                    "SIP TX re-INVITE call_id=%s offered=[%s]",
                    self.dialog_ids.call_id,
                    ", ".join(sdp.offered_media_descriptions(offer)),
                )
                early_candidate: SipDialog | None = None
                final_addr = (next_host, next_port)

                async def handle_request(message, addr) -> None:
                    terminal = await self._dispatch_in_dialog_request(
                        message, addr, local_offer_pending=True
                    )
                    if terminal is not None:
                        raise ConnectionAbortedError(terminal)

                async def handle_provisional(message, addr) -> None:
                    nonlocal early_candidate
                    status = int(message.status_code or 0)
                    self.last_sip_status_code = status
                    self.last_sip_reason = message.reason
                    if not await self._ack_reliable_provisional(
                        message, addr, process_early_media=False
                    ):
                        raise ConnectionAbortedError("provisional_rejected")
                    if message.body:
                        early_candidate = self._dialog_candidate_from_answer(
                            current,
                            offer,
                            message,
                            local_video_rtp_port=local_video_port,
                            offered_video_formats=offered_video,
                            video_direction=(
                                "inactive" if removing_video else video_direction
                            ),
                            session_version=session_version,
                        )
                        if early_candidate is None:
                            raise ConnectionAbortedError("invalid_early_answer")

                def capture_final(_message, addr) -> None:
                    nonlocal final_addr
                    final_addr = addr

                try:
                    message = await async_run_dialog_request_transaction(
                        send=lambda: self._send_dialog_request(
                            request.raw, next_host, next_port
                        ),
                        read=self._read_response,
                        matches=lambda response: matches_response(
                            response,
                            method="INVITE",
                            cseq=request.ids.cseq,
                            branch=request.ids.branch,
                        ),
                        active=lambda: self.dialog is current,
                        transport=self.signaling_transport,
                        timeout=timeout,
                        on_request=handle_request,
                        on_provisional=handle_provisional,
                        on_final=capture_final,
                    )
                except (ConnectionAbortedError, ConnectionError, OSError):
                    return None
                if message is None:
                    self._start_bye_request_transaction(
                        current.remote_host,
                        current.remote_sip_port,
                        current.remote_target_uri or current.remote_uri,
                        current.local_uri,
                        current.remote_uri,
                        route_set=current.route_set,
                    )
                    self.dialog = None
                    return None
                addr = final_addr
                status = int(message.status_code or 0)
                self.last_sip_status_code = status
                self.last_sip_reason = message.reason
                if 200 <= status < 300:
                    candidate = (
                        self._dialog_candidate_from_answer(
                            current,
                            offer,
                            message,
                            local_video_rtp_port=local_video_port,
                            offered_video_formats=offered_video,
                            video_direction=(
                                "inactive" if removing_video else video_direction
                            ),
                            session_version=session_version,
                        )
                        if message.body
                        else early_candidate
                    )
                    remote_tag = sip.extract_tag(message.header("To"))
                    ack_target = (
                        candidate.remote_target_uri
                        if candidate is not None
                        else current.remote_target_uri or current.remote_uri
                    )
                    acked = self._send_ack(
                        current.remote_host,
                        current.remote_sip_port,
                        ack_target,
                        current.local_uri,
                        current.remote_uri,
                        route_set=current.route_set,
                        cseq=request.ids.cseq,
                        remote_tag=remote_tag,
                    )
                    if candidate is None or not acked:
                        self._start_bye_request_transaction(
                            current.remote_host,
                            current.remote_sip_port,
                            ack_target,
                            current.local_uri,
                            current.remote_uri,
                            route_set=current.route_set,
                        )
                        self.dialog = None
                        return None
                    self._prepared_reinvite = (
                        current,
                        candidate,
                        offered_video if candidate.video_format is not None else (),
                        (
                            video_direction
                            if candidate.video_format is not None
                            else "inactive"
                        ),
                    )
                    return candidate
                self._send_invite_error_ack(
                    message,
                    addr[0],
                    addr[1],
                    request_uri=request.routing.request_uri,
                    local_uri=current.local_uri,
                    remote_uri=current.remote_uri,
                    route_set=current.route_set,
                    cseq=request.ids.cseq,
                    branch=request.ids.branch,
                )
                if status in {408, 481}:
                    self.dialog = None
                return None
            finally:
                self._release_dialog_writer()
                self._dialog_read_lock.release()

    def commit_prepared_reinvite(
        self,
        previous: SipDialog,
        candidate: SipDialog,
    ) -> bool:
        """Publish exactly the staged dialog generation."""

        prepared = self._prepared_reinvite
        if (
            prepared is None
            or prepared[0] is not previous
            or prepared[1] is not candidate
            or self.dialog is not previous
            or self._closing
            or self._closed
        ):
            return False
        self.dialog = candidate
        self.video_formats = prepared[2]
        self.video_format = candidate.video_format
        self.video_direction = prepared[3]
        self.local_video_rtp_port = candidate.local_video_rtp_port
        self.generic_video_relay = candidate.video_format is not None
        self._prepared_reinvite = None
        return True

    def abort_prepared_reinvite(
        self,
        previous: SipDialog,
        candidate: SipDialog,
    ) -> None:
        """Terminate a destination that accepted video after source rollback."""

        prepared = self._prepared_reinvite
        if (
            (prepared is None or prepared[:2] != (previous, candidate))
            and self.dialog is not candidate
        ):
            return
        self._prepared_reinvite = None
        self._start_bye_request_transaction(
            candidate.remote_host,
            candidate.remote_sip_port,
            candidate.remote_target_uri or candidate.remote_uri,
            candidate.local_uri,
            candidate.remote_uri,
            route_set=candidate.route_set,
        )
        self.dialog = None

    def _commit_200_ok(
        self,
        msg: sip.SipMessage,
        target: str,
        remote_host: str,
        remote_sip_port: int,
        request_uri: str,
        local_uri: str,
        remote_uri: str,
        *,
        provisional: bool = False,
    ) -> bool:
        if not provisional:
            self._invite_transaction_active = False
        if not request_uri:
            request_uri = str(sip.SipUri(target or "voip", remote_host, remote_sip_port))
        transport_param = (("transport", self.signaling_transport.lower()),)
        if not local_uri:
            local_uri = str(
                sip.SipUri(
                    self.local_uri_user,
                    self.local_ip,
                    self.local_sip_port,
                    params=transport_param,
                )
            )
        if not remote_uri:
            remote_uri = request_uri
        remote_target_uri = request_uri
        try:
            contact_target = sip.contact_target_uri(msg)
        except (TypeError, ValueError, sip.SipError):
            _LOGGER.info(
                "SIP 200 OK has invalid Contact; retaining original remote target"
            )
        else:
            if contact_target:
                remote_target_uri = contact_target
        response_display = sip.name_addr_display_name(msg.header("To"))
        self._dialog_remote_display_name = response_display
        self.connected_party = next(
            (
                identity
                for identity in (
                    response_display,
                    self._pending_target_display,
                    sip.name_addr_identity(msg.header("To")),
                )
                if identity
            ),
            str(target or "").strip(),
        )
        try:
            route_set = sip.record_route_set(msg, reverse=True)
        except (TypeError, ValueError, sip.SipError):
            route_set = ()
            _LOGGER.info(
                "SIP 200 OK has invalid Record-Route; using direct dialog routing"
            )
        remote_tag = sip.extract_tag(msg.header("To"))
        if not remote_tag:
            return False
        if (
            not provisional
            and not msg.body
            and (candidate := self.early_dialogs.get(remote_tag)) is not None
        ):
            candidate.remote_target_uri = remote_target_uri
            candidate.route_set = route_set
            self.dialog_ids.remote_tag = remote_tag
            self.dialog = candidate
            self.early_dialogs.clear()
            return self._send_ack(
                remote_host,
                int(remote_sip_port),
                remote_target_uri,
                local_uri,
                remote_uri,
                route_set=route_set,
            )
        if self._closing or self._closed:
            # A 2xx terminates the INVITE transaction even when local teardown
            # won the race.  ACK it and immediately end the just-created remote
            # dialog, but never publish that dialog into a closing client.
            if not provisional:
                self._reject_confirmed_dialog(
                    remote_tag=remote_tag,
                    remote_host=remote_host,
                    remote_sip_port=remote_sip_port,
                    remote_target_uri=remote_target_uri,
                    local_uri=local_uri,
                    remote_uri=remote_uri,
                    route_set=route_set,
                )
            return False
        if self._initial_delayed_offer and not provisional:
            if (
                not msg.body
                or msg.header("Content-Type").split(";", 1)[0].strip().lower()
                != "application/sdp"
            ):
                self._reject_confirmed_dialog(
                    remote_tag=remote_tag,
                    remote_host=remote_host,
                    remote_sip_port=remote_sip_port,
                    remote_target_uri=remote_target_uri,
                    local_uri=local_uri,
                    remote_uri=remote_uri,
                    route_set=route_set,
                )
                return False
            seed_format = self.supported_send_formats[0]
            seed = SipDialog(
                target=target,
                remote_host=remote_host,
                remote_sip_port=int(remote_sip_port),
                remote_rtp_host="",
                remote_rtp_port=0,
                local_rtp_port=self.local_rtp_port,
                call_id=self.dialog_ids.call_id,
                remote_tag=remote_tag,
                local_uri=local_uri,
                remote_uri=remote_uri,
                send_format=seed_format,
                recv_format=self.supported_recv_formats[0],
                remote_target_uri=remote_target_uri,
                route_set=route_set,
                local_sdp_session_id=self._sdp_session_id,
            )
            prepared = self._answer_remote_offer(msg, current=seed)
            if prepared is None:
                self._reject_confirmed_dialog(
                    remote_tag=remote_tag,
                    remote_host=remote_host,
                    remote_sip_port=remote_sip_port,
                    remote_target_uri=remote_target_uri,
                    local_uri=local_uri,
                    remote_uri=remote_uri,
                    route_set=route_set,
                )
                return False
            candidate, answer = prepared
            candidate.peer_supports_from_change = sip.supports_option(
                msg, "from-change"
            )
            self.dialog_ids.remote_tag = remote_tag
            self.dialog = candidate
            self.early_dialogs.clear()
            self._local_sdp_body = answer
            return self._send_ack(
                remote_host,
                int(remote_sip_port),
                remote_target_uri,
                local_uri,
                remote_uri,
                route_set=route_set,
                body=answer.encode(),
            )
        negotiation_error: Exception | None = None
        try:
            if (
                msg.body
                and msg.header("Content-Type").split(";", 1)[0].strip().lower()
                != "application/sdp"
            ):
                raise sdp.SdpError("SIP 200 OK body is not application/sdp")
            local_offer_direction = "sendrecv"
            if self._local_sdp_body:
                sdp.validate_sdp_answer(
                    self._local_sdp_body,
                    msg.body,
                    allow_omitted_trailing_media=True,
                )
                local_offer_direction = str(
                    sdp.parse_sdp(self._local_sdp_body)["direction"]
                )
            selected = sdp.negotiate_answer_directional(
                msg.body,
                self.supported_send_formats,
                self.supported_recv_formats,
                local_offer_direction=local_offer_direction,
                local_offer_sdp=self._local_sdp_body or None,
                allow_dahua_pcm=self.include_dahua_pcm,
            )
        except Exception as err:
            selected = None
            negotiation_error = err
        if selected is None:
            try:
                offered = ", ".join(sdp.offered_media_descriptions(msg.body))
            except Exception as err:
                offered = f"unparseable SDP media: {err}"
            _LOGGER.info(
                "SIP 200 OK rejected: no compatible answer media offered=[%s] error=%s",
                offered,
                negotiation_error or "none",
            )
            if not provisional:
                self._reject_confirmed_dialog(
                    remote_tag=remote_tag,
                    remote_host=remote_host,
                    remote_sip_port=remote_sip_port,
                    remote_target_uri=remote_target_uri,
                    local_uri=local_uri,
                    remote_uri=remote_uri,
                    route_set=route_set,
                )
            return False
        parsed = sdp.parse_sdp(msg.body)
        video_directional = (
            sdp.negotiate_video_answer_directional(msg.body, self.video_formats)
            if self.video_formats
            else None
        )
        video_answer = (
            video_directional.send if video_directional is not None else None
        )
        local_video_answer = (
            video_directional.recv if video_directional is not None else None
        )
        remote_video = sdp.parse_video_sdp(msg.body) if video_answer is not None else None
        answered_dtmf_formats = sdp.offered_dtmf_formats(msg.body)
        dtmf_direction = (
            sdp.negotiate_dtmf_answer_directional(msg.body, self._local_sdp_body)
            if self._local_sdp_body
            else None
        )
        dtmf_recv = (
            dtmf_direction.recv
            if dtmf_direction is not None
            else next(iter(answered_dtmf_formats), None)
        )
        dtmf_send = (
            dtmf_direction.send
            if dtmf_direction is not None
            else dtmf_recv
        )
        session_timer: sip.SipSessionExpires | None = None
        if raw_session_timer := msg.header("Session-Expires"):
            try:
                session_timer = sip.parse_session_expires(raw_session_timer)
                if not session_timer.refresher:
                    raise sip.SipError("Session-Expires response lacks refresher")
            except sip.SipError:
                if not provisional:
                    self._reject_confirmed_dialog(
                        remote_tag=remote_tag,
                        remote_host=remote_host,
                        remote_sip_port=remote_sip_port,
                        remote_target_uri=remote_target_uri,
                        local_uri=local_uri,
                        remote_uri=remote_uri,
                        route_set=route_set,
                    )
                return False
        now = asyncio.get_running_loop().time() if session_timer else 0.0
        if not provisional:
            self.dialog_ids.remote_tag = remote_tag
            if (
                self._dialog_termination_task is not None
                and self._dialog_termination_task.done()
            ):
                self._dialog_termination_task = None
        candidate = SipDialog(
            target=target,
            remote_host=remote_host,
            remote_sip_port=int(remote_sip_port),
            remote_rtp_host=parsed["connection_ip"],
            remote_rtp_port=int(parsed["media_port"]),
            local_rtp_port=self.local_rtp_port,
            call_id=self.dialog_ids.call_id,
            remote_tag=remote_tag,
            local_uri=local_uri,
            remote_uri=remote_uri,
            send_format=selected.send,
            recv_format=selected.recv,
            remote_target_uri=remote_target_uri,
            route_set=route_set,
            dtmf_payload_type=(dtmf_recv.payload_type if dtmf_recv else None),
            dtmf_clock_rate=(dtmf_recv.sample_rate if dtmf_recv else 8000),
            dtmf_events=(dtmf_recv.events if dtmf_recv else frozenset()),
            send_dtmf_payload_type=(
                dtmf_send.payload_type if dtmf_send else None
            ),
            send_dtmf_clock_rate=(
                dtmf_send.sample_rate if dtmf_send else None
            ),
            send_dtmf_events=(dtmf_send.events if dtmf_send else None),
            remote_audio_direction=str(parsed["direction"]),
            local_audio_direction=sdp.local_direction_for_offer(
                parsed["direction"],
                remote_connection_held=bool(parsed["connection_held"]),
            ),
            remote_audio_connection_held=bool(parsed["connection_held"]),
            video_format=video_answer,
            local_video_format=local_video_answer,
            remote_video_rtp_host=(str(remote_video["connection_ip"]) if remote_video else ""),
            remote_video_rtp_port=(int(remote_video["media_port"]) if remote_video else 0),
            remote_video_rtcp_host=(
                str(remote_video["rtcp_address"] or remote_video["connection_ip"])
                if remote_video
                else ""
            ),
            remote_video_rtcp_port=(
                int(remote_video["rtcp_port"] or int(remote_video["media_port"]) + 1)
                if remote_video
                else 0
            ),
            remote_video_rtcp_mux=False,
            remote_video_payload_types=(
                tuple(int(item) for item in remote_video["payload_order"])
                if remote_video
                else ()
            ),
            remote_video_connection_held=bool(
                remote_video and remote_video["connection_held"]
            ),
            local_video_rtp_port=(self.local_video_rtp_port if video_answer is not None else 0),
            local_video_direction=(
                sdp.constrained_video_direction(
                    video_answer.direction,
                    allow_send=(
                        self.video_direction in {"sendonly", "sendrecv"}
                        and (
                            self.generic_video_relay
                            or sdp.browser_video_send_supported(video_answer)
                        )
                        and not bool(
                            remote_video and remote_video["connection_held"]
                        )
                    ),
                    allow_receive=self.video_direction in {"recvonly", "sendrecv"},
                )
                if video_answer is not None
                else "inactive"
            ),
            local_sdp_session_id=self._sdp_session_id,
            local_sdp_session_version=0,
            local_sdp_body=self._local_sdp_body,
            peer_supports_from_change=sip.supports_option(msg, "from-change"),
            session_timer=sip.SipSessionTimer(),
        )
        candidate.session_timer.configure(session_timer, local_role="uac", now=now)
        _LOGGER.info(
            "SIP 200 OK media selected call_id=%s tx=%s rx=%s answer=[%s]",
            self.dialog_ids.call_id,
            selected.send.wire_token(),
            selected.recv.wire_token(),
            ", ".join(sdp.offered_media_descriptions(msg.body)),
        )
        if provisional:
            self.early_dialogs[remote_tag] = candidate
            return True
        self.dialog = candidate
        self.early_dialogs.clear()
        self._send_ack(
            remote_host,
            int(remote_sip_port),
            remote_target_uri,
            local_uri,
            remote_uri,
            route_set=route_set,
        )
        return True

    def _reject_confirmed_dialog(
        self,
        *,
        remote_tag: str,
        remote_host: str,
        remote_sip_port: int,
        remote_target_uri: str,
        local_uri: str,
        remote_uri: str,
        route_set: tuple[str, ...],
    ) -> None:
        """ACK a successful INVITE response, then terminate its unusable dialog."""

        self.dialog_ids.remote_tag = remote_tag
        self._send_ack(
            remote_host,
            int(remote_sip_port),
            remote_target_uri,
            local_uri,
            remote_uri,
            route_set=route_set,
        )
        self._start_bye_request_transaction(
            remote_host,
            int(remote_sip_port),
            remote_target_uri,
            local_uri,
            remote_uri,
            route_set=route_set,
        )

    def _send_ack(
        self,
        host: str,
        port: int,
        request_uri: str,
        local_uri: str,
        remote_uri: str,
        *,
        route_set: tuple[str, ...] = (),
        cseq: int | None = None,
        remote_tag: str = "",
        body: bytes = b"",
    ) -> bool:
        if not self._has_signaling_path():
            return False
        try:
            request = build_dialog_request(
                "ACK",
                call_id=self.dialog_ids.call_id,
                local_tag=self.dialog_ids.local_tag,
                remote_tag=remote_tag or self.dialog_ids.remote_tag,
                cseq=self._invite_cseq if cseq is None else int(cseq),
                local_uri=local_uri,
                remote_uri=remote_uri,
                remote_target_uri=request_uri,
                route_set=route_set,
                transport=self.signaling_transport,
                local_display_name=self.local_name,
                remote_display_name=self._dialog_remote_display_name,
                content_type="application/sdp" if body else "",
                body=body,
            )
        except (TypeError, ValueError, sip.SipError) as err:
            _LOGGER.warning("SIP ACK routing rejected: %s", err)
            return False
        next_host, next_port = self._dialog_next_hop(
            request.routing.next_hop_uri,
            host,
            int(port),
        )
        if not self._send_dialog_request(request.raw, next_host, next_port):
            _LOGGER.warning("SIP TX ACK dropped: signaling path unavailable")
            return False
        sip.mark_sip_event(self, "ACK")
        _LOGGER.info("SIP TX ACK %s:%s", next_host, next_port)
        return True

    def _send_invite_error_ack(
        self,
        msg: sip.SipMessage,
        host: str,
        port: int,
        *,
        request_uri: str = "",
        local_uri: str = "",
        remote_uri: str = "",
        route_set: tuple[str, ...] = (),
        cseq: int | None = None,
        branch: str = "",
    ) -> None:
        if not self._has_signaling_path():
            return
        request_uri = request_uri or self._pending_request_uri
        local_uri = local_uri or self._pending_local_uri
        remote_uri = remote_uri or self._pending_remote_uri
        if not request_uri or not local_uri or not remote_uri:
            return
        try:
            routing = sip.dialog_request_routing(request_uri, route_set)
        except (TypeError, ValueError, sip.SipError):
            return
        ack_ids = sip.SipDialogIds(
            call_id=self.dialog_ids.call_id,
            local_tag=self.dialog_ids.local_tag,
            remote_tag=sip.extract_tag(msg.header("To")),
            cseq=self._invite_cseq if cseq is None else int(cseq),
            branch=branch or self.dialog_ids.branch,
        )
        headers = sip.dialog_headers(
            request_uri=routing.request_uri,
            local_uri=local_uri,
            remote_uri=remote_uri,
            dialog=ack_ids,
            method="ACK",
            contact_uri=local_uri,
            transport=self.signaling_transport,
            local_display_name=self.local_name,
            remote_display_name=sip.name_addr_display_name(msg.header("To")),
        )
        headers.extend(("Route", value) for value in routing.route_headers)
        raw = sip.build_request("ACK", routing.request_uri, headers, b"")
        if not self._send_dialog_request(raw, host, int(port)):
            _LOGGER.warning("SIP TX ACK final INVITE error dropped: signaling path unavailable")
            return
        sip.mark_sip_event(self, "ACK")
        _LOGGER.info("SIP TX ACK final INVITE error %s:%s", host, port)

    def _start_bye_request_transaction(
        self,
        host: str,
        port: int,
        request_uri: str,
        local_uri: str,
        remote_uri: str,
        *,
        route_set: tuple[str, ...] = (),
        remote_tag: str = "",
        timeout: float = 1.5,
    ) -> bool:
        if not self._has_signaling_path() or not request_uri or not local_uri or not remote_uri:
            return False
        try:
            request = build_dialog_request(
                "BYE",
                call_id=self.dialog_ids.call_id,
                local_tag=self.dialog_ids.local_tag,
                remote_tag=remote_tag or self.dialog_ids.remote_tag,
                cseq=self._next_dialog_cseq(),
                local_uri=local_uri,
                remote_uri=remote_uri,
                remote_target_uri=request_uri,
                route_set=route_set,
                transport=self.signaling_transport,
                local_display_name=self.local_name,
                remote_display_name=self._dialog_remote_display_name,
            )
        except (TypeError, ValueError, sip.SipError) as err:
            _LOGGER.warning("SIP BYE routing rejected: %s", err)
            return False
        next_host, next_port = self._dialog_next_hop(
            request.routing.next_hop_uri,
            host,
            int(port),
        )
        async def run() -> None:
            await self._run_built_dialog_request(
                request,
                next_host,
                next_port,
                method="BYE",
                timeout=timeout,
                active=lambda: self._has_signaling_path() and not self._closed,
            )

        task = asyncio.create_task(
            run(),
            name=f"voip-sip-client-exceptional-bye-{self.dialog_ids.call_id}",
        )
        self._exceptional_bye_tasks.add(task)
        task.add_done_callback(self._exceptional_bye_done)
        return True

    def _exceptional_bye_done(self, task: asyncio.Task[None]) -> None:
        self._exceptional_bye_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            _LOGGER.warning(
                "Exceptional SIP BYE failed call_id=%s error=%s",
                self.dialog_ids.call_id,
                error,
            )

    async def _run_bye_transaction(self, dialog: SipDialog, timeout: float) -> str:
        """Own the confirmed-dialog BYE transaction until its final response."""

        if not self._dialog_is_current(dialog) or not self._has_signaling_path():
            return "transport_unreachable"
        self._uas_invite_2xx.cancel()
        try:
            response = await self._send_in_dialog_request("BYE", timeout=timeout)
        except ConnectionAbortedError as err:
            return str(err) or "remote_hangup"
        finally:
            if self._dialog_is_current(dialog):
                self.dialog = None
        if response is None:
            return "timeout"
        if 200 <= int(response.status_code or 0) < 300:
            return "remote_hangup"
        return "remote_hangup"

    def _start_bye_transaction(self, timeout: float = 1.5) -> asyncio.Task[str] | None:
        """Start exactly one BYE owner for the current confirmed dialog."""

        task = self._dialog_termination_task
        if task is not None:
            return task
        dialog = self.dialog
        if dialog is None or not self._has_signaling_path():
            return None
        task = asyncio.create_task(
            self._run_bye_transaction(dialog, timeout),
            name=f"voip-sip-client-bye-{self.dialog_ids.call_id}",
        )
        self._dialog_termination_task = task
        return task

    async def _terminate_confirmed_dialog(self, timeout: float = 1.5) -> str:
        task = self._start_bye_transaction(timeout)
        if task is None:
            return "transport_unreachable"
        return await asyncio.shield(task)

    def bye(self) -> bool:
        """Compatibility entry point that delegates to the single BYE owner."""

        return self._start_bye_transaction() is not None

    def request_cancel(self) -> bool:
        """Request cancellation by the coroutine that owns the INVITE transaction."""
        if (
            not self._invite_transaction_active
            or not self._has_signaling_path()
            or not self._pending_request_uri
        ):
            _LOGGER.info(
                "SIP CANCEL skipped: no signaling path call_id=%s transport=%s pending_uri=%s",
                self.dialog_ids.call_id,
                self.signaling_transport,
                bool(self._pending_request_uri),
            )
            return False
        self._cancel_requested = True
        self._invite_abort_event.set()
        if self._received_provisional:
            return self._send_cancel()
        return True

    def _send_cancel(self) -> bool:
        """Send CANCEL after the INVITE has entered the proceeding state."""
        if self._cancel_sent:
            return True
        cancel_ids = sip.SipDialogIds(
            call_id=self.dialog_ids.call_id,
            local_tag=self.dialog_ids.local_tag,
            remote_tag="",
            cseq=self._invite_cseq,
            branch=self.dialog_ids.branch,
        )
        headers = sip.dialog_headers(
            request_uri=self._pending_request_uri,
            local_uri=self._pending_local_uri,
            remote_uri=self._pending_remote_uri,
            dialog=cancel_ids,
            method="CANCEL",
            contact_uri=self._pending_local_uri,
            transport=self.signaling_transport,
            local_display_name=self.local_name,
            remote_display_name=self._pending_target_display,
        )
        raw = sip.build_request("CANCEL", self._pending_request_uri, headers, b"")
        if not self._send_dialog_request(raw, self._pending_remote_host, self._pending_remote_sip_port):
            _LOGGER.warning("SIP TX CANCEL dropped: signaling path unavailable")
            return False
        self._cancel_sent = True
        sip.mark_sip_event(self, "CANCEL")
        _LOGGER.info("SIP TX CANCEL %s:%s", self._pending_remote_host, self._pending_remote_sip_port)
        return True

    def cancel(self) -> bool:
        """Cancel now, or defer until the INVITE receives a provisional response."""
        sent_or_deferred = self.request_cancel()
        if sent_or_deferred and not self._received_provisional:
            _LOGGER.info("SIP CANCEL deferred until provisional call_id=%s", self.dialog_ids.call_id)
        return sent_or_deferred

    def bye_or_cancel(self) -> None:
        if self.dialog is not None:
            self.bye()
        else:
            self.cancel()

    def _schedule_deferred_close(self, _invite_task: asyncio.Task[str]) -> None:
        """Own and observe cleanup deferred until an INVITE task completes."""

        task = self._deferred_close_task
        if task is not None and not task.done():
            return
        task = asyncio.create_task(
            self.close(),
            name=f"voip-sip-client-deferred-close-{self.dialog_ids.call_id}",
        )
        self._deferred_close_task = task

        def completed(done: asyncio.Task[None]) -> None:
            if self._deferred_close_task is done:
                self._deferred_close_task = None
            if done.cancelled():
                return
            error = done.exception()
            if error is not None:
                _LOGGER.warning(
                    "Deferred SIP client close failed call_id=%s error=%s",
                    self.dialog_ids.call_id,
                    error,
                    exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(completed)

    async def terminate(self, timeout: float = 1.5) -> str:
        """Terminate the SIP dialog/transaction and wait for the SIP response.

        A confirmed dialog ends with BYE + 200 OK. An early INVITE transaction
        ends when its final response arrives after CANCEL. The CANCEL client
        transaction is independent, so a final non-2xx INVITE response is
        sufficient to stop waiting once it has been ACKed.
        """
        if self.dialog is not None or self._dialog_termination_task is not None:
            return await self._terminate_confirmed_dialog(timeout)

        invite_task = self._invite_task
        if invite_task is not None and not invite_task.done():
            if not self.request_cancel():
                return "transport_unreachable"
            try:
                return await asyncio.wait_for(asyncio.shield(invite_task), timeout=timeout)
            except asyncio.TimeoutError:
                invite_task.add_done_callback(self._schedule_deferred_close)
                return "cancel_pending"

        sent_cancel = self.cancel()
        if not sent_cancel:
            return "transport_unreachable"
        saw_cancel_ok = False
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                received = await self._read_response(max(0.05, deadline - asyncio.get_running_loop().time()))
            except asyncio.TimeoutError:
                break
            except sip.SipError as err:
                _LOGGER.debug(
                    "Ignoring malformed SIP response while waiting for CANCEL: %s",
                    err,
                )
                continue
            if received is None:
                break
            msg, addr = received
            if not msg.is_response or msg.status_code is None:
                continue
            sip.mark_sip_event(self, "SIP_RESPONSE", int(msg.status_code), msg.reason)
            _LOGGER.info("SIP RX %s %s from %s:%s", msg.status_code, msg.reason, addr[0], addr[1])
            try:
                cseq = sip.parse_cseq(msg.header("CSeq"))
                via_values = msg.header_values("Via")
                response_branch = sip.parse_via(via_values[0] if via_values else "").branch
            except (TypeError, ValueError, sip.SipError):
                continue
            cseq_method = cseq.method
            if cseq_method in {"CANCEL", "INVITE"}:
                if cseq.number != self._invite_cseq or response_branch != self.dialog_ids.branch:
                    continue
            else:
                continue
            if cseq_method == "CANCEL" and 200 <= msg.status_code < 300:
                saw_cancel_ok = True
            elif cseq_method == "INVITE" and 200 <= msg.status_code < 300:
                # The final 2xx won the race with CANCEL. RFC 3261 requires us
                # to accept it, ACK it, then end the confirmed dialog with BYE.
                committed = self._commit_200_ok(
                    msg,
                    self._pending_target,
                    self._pending_remote_host or addr[0],
                    self._pending_remote_sip_port,
                    self._pending_request_uri,
                    self._pending_local_uri,
                    self._pending_remote_uri,
                )
                if committed:
                    remaining = max(
                        0.05,
                        deadline - asyncio.get_running_loop().time(),
                    )
                    await self._terminate_confirmed_dialog(timeout=remaining)
                return "cancelled"
            elif cseq_method == "INVITE" and msg.status_code >= 300:
                self._send_invite_error_ack(msg, addr[0], addr[1])
                self._invite_transaction_active = False
                # RFC 3261 makes the INVITE and CANCEL client transactions
                # independent. A final INVITE response proves that the call
                # attempt is over even if the 200 to CANCEL was lost or was
                # consumed by the transaction owner just before teardown.
                return "cancelled"
        if saw_cancel_ok:
            return "cancelled"
        return "timeout"

    def snapshot(self) -> dict[str, Any]:
        dialog = self.dialog
        return {
            "call_id": self.dialog_ids.call_id,
            "local_uri": dialog.local_uri if dialog is not None else self._pending_local_uri,
            "remote_uri": dialog.remote_uri if dialog is not None else self._pending_remote_uri,
            "remote_target_uri": (
                (dialog.remote_target_uri or dialog.remote_uri)
                if dialog is not None
                else self._pending_request_uri
            ),
            "route_set": list(dialog.route_set) if dialog is not None else [],
            "remote_host": dialog.remote_host if dialog is not None else self._pending_remote_host,
            "remote_sip_port": dialog.remote_sip_port if dialog is not None else self._pending_remote_sip_port,
            "remote_rtp_host": dialog.remote_rtp_host if dialog is not None else "",
            "remote_rtp_port": dialog.remote_rtp_port if dialog is not None else 0,
            "local_rtp_port": dialog.local_rtp_port if dialog is not None else self.local_rtp_port,
            "selected_tx_format": dialog.send_format.audio_format.wire_token() if dialog is not None else "",
            "selected_rx_format": dialog.recv_format.audio_format.wire_token() if dialog is not None else "",
            "selected_tx_rtp_format": dialog.send_format.wire_token() if dialog is not None else "",
            "selected_rx_rtp_format": dialog.recv_format.wire_token() if dialog is not None else "",
            "dialog_active": dialog is not None,
            "pending_invite": bool(self._invite_transaction_active and dialog is None),
            "pending_remote_invite_ack": self._uas_invite_2xx.cseq,
            "remote_invite_2xx_retransmissions": self._uas_invite_2xx.retransmissions,
            "sip_transport": self.signaling_transport.lower(),
            "last_sip_event": self.last_sip_event,
            "last_sip_status_code": self.last_sip_status_code,
            "last_sip_reason": self.last_sip_reason,
        }
