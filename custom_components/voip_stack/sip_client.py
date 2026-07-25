"""Outbound SIP/RTP primitives for the phase-1 VoIP Stack profile."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, replace
import logging
import secrets
import socket
from typing import Any, Awaitable, Callable

from .audio_format import AudioFormat, HA_SIP_PCM_FORMATS, PcmFormat
from . import g711
from .codec_capabilities import supports_dahua_pcm
from .g722_codec import G722Decoder, G722Encoder
from .opus_codec import OpusDecoder, OpusEncoder
from .session_cleanup import async_wait_for_cleanup
from . import sdp, sip
from .sip_auth import build_digest_authorization
from .sip_tcp_io import SipTcpWriter, read_sip_stream_message as _read_sip_stream_message
from .sip_udp_io import SipDatagramQueueProtocol
from .sip_transaction import (
    SIP_T1,
    SIP_T2,
    SIP_TIMER_B,
    SipClientTransaction,
    async_run_server_transaction,
)

_LOGGER = logging.getLogger(__name__)

_S24_SIGN_EXTENSION = bytes(0xFF if value & 0x80 else 0x00 for value in range(256))


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
        self._codec = (
            OpusDecoder(fmt.pcm_sample_rate, fmt.channels)
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
        self._codec = (
            OpusEncoder(fmt.pcm_sample_rate, fmt.channels)
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
class SipDialog:
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
    remote_target_uri: str = ""
    route_set: tuple[str, ...] = ()
    dtmf_payload_type: int | None = None
    dtmf_clock_rate: int = 8000
    dtmf_events: frozenset[int] = frozenset(range(16))
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
    local_sdp_session_id: int = 0
    local_sdp_session_version: int = 0
    local_sdp_body: str = ""

    @property
    def selected_format(self) -> sdp.RtpPcmFormat:
        return self.send_format

    @property
    def send_video_format(self) -> sdp.RtpVideoFormat | None:
        return self.video_format

    @property
    def recv_video_format(self) -> sdp.RtpVideoFormat | None:
        return self.local_video_format or self.video_format


DialogMediaCommit = Callable[[], Awaitable[None]]
DialogMediaUpdateHandler = Callable[
    [SipDialog, SipDialog, str],
    Awaitable[DialogMediaCommit | None],
]


@dataclass(slots=True, frozen=True)
class _InDialogResponse:
    request: sip.SipMessage
    status: int
    reason: str
    extra_headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b""


_SipClientProtocol = SipDatagramQueueProtocol


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
        peer_user_agent: str = "",
        local_video_rtp_port: int = 0,
        video_format: sdp.RtpVideoFormat | None = None,
        video_formats: tuple[sdp.RtpVideoFormat, ...] | list[sdp.RtpVideoFormat] | None = None,
        video_direction: str = "sendrecv",
        generic_video_relay: bool = False,
        media_reservation=None,
        video_rtp_socket: socket.socket | None = None,
        video_rtcp_socket: socket.socket | None = None,
    ) -> None:
        self.local_ip = local_ip
        self.local_name = local_name
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
        self.peer_user_agent = str(peer_user_agent or "").strip()
        self.include_dahua_pcm = supports_dahua_pcm(self.peer_user_agent)
        self.local_video_rtp_port = int(local_video_rtp_port or 0)
        requested_video = tuple(video_formats or (() if video_format is None else (video_format,)))
        self.video_formats = requested_video if self.local_video_rtp_port > 0 else ()
        self.video_format = self.video_formats[0] if self.video_formats else None
        self.video_direction = str(video_direction or "sendrecv")
        self.generic_video_relay = bool(generic_video_relay)
        self.media_reservation = media_reservation
        self.video_rtp_socket = video_rtp_socket
        self.video_rtcp_socket = video_rtcp_socket
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
        self.dialog_ids = sip.SipDialogIds(call_id=sip.make_call_id("ha"), local_tag=sip.make_tag())
        self.dialog: SipDialog | None = None
        self.early_dialog: SipDialog | None = None
        self.on_info_dtmf: Callable[[str], None] | None = None
        self.on_media_update: DialogMediaUpdateHandler | None = None
        self._invite_cseq = self.dialog_ids.cseq
        self._pending_target = ""
        self._pending_remote_host = ""
        self._pending_remote_sip_port = 5060
        self._pending_request_uri = ""
        self._pending_local_uri = ""
        self._pending_remote_uri = ""
        self._invite_transaction_active = False
        self._cancel_requested = False
        self._cancel_sent = False
        self._received_provisional = False
        self._invite_task: asyncio.Task[str] | None = None
        self._final_response_task: asyncio.Task[str] | None = None
        self._start_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._deferred_close_task: asyncio.Task[None] | None = None
        self._closing = False
        self._closed = False
        self._bye_cseq = 0
        self._bye_branch = ""
        self._local_dialog_cseq = self._invite_cseq
        self._remote_cseq = 0
        self._in_dialog_responses: list[_InDialogResponse] = []
        self._uas_invite_2xx_task: asyncio.Task[None] | None = None
        self._uas_invite_2xx_request: sip.SipMessage | None = None
        self._uas_invite_2xx_host = ""
        self._uas_invite_2xx_port = 0
        self._uas_invite_2xx_status = 0
        self._uas_invite_2xx_reason = ""
        self._uas_invite_2xx_extra_headers: tuple[tuple[str, str], ...] = ()
        self._uas_invite_2xx_body = b""
        self._uas_invite_2xx_cseq = 0
        self._uas_invite_2xx_retransmissions = 0
        self._uas_invite_ack_timeout = asyncio.Event()
        self.last_sip_event = ""
        self.last_sip_status_code = 0
        self.last_sip_reason = ""

    async def start(self) -> None:
        if self.signaling_transport == "TCP":
            return
        async with self._start_lock:
            if self.transport is not None:
                return
            if self._closing or self._closed:
                raise RuntimeError("SIP client is already closed")
            loop = asyncio.get_running_loop()
            protocol = SipDatagramQueueProtocol(self.queue)
            transport, _ = await loop.create_datagram_endpoint(
                lambda: protocol,
                local_addr=("0.0.0.0", 0),
                family=socket.AF_INET,
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
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._close(),
                name=f"voip-sip-client-close-{self.dialog_ids.call_id}",
            )
        await async_wait_for_cleanup(self._close_task)

    async def _close(self) -> None:
        self._cancel_uas_invite_2xx()
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
                for task in (self._final_response_task, self._invite_task)
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
                    self._final_response_task,
                    self._invite_task,
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
        self.dialog = None
        self.early_dialog = None

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

    def _signaling_target(self, remote_host: str, remote_sip_port: int) -> tuple[str, int]:
        proxy = str(self.outbound_proxy or "").strip()
        if not proxy:
            return remote_host, int(remote_sip_port)
        if proxy.startswith("sip:"):
            proxy = proxy[4:]
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
        if self.signaling_transport == "TCP":
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
        if self.signaling_transport == "TCP":
            return self.writer is not None or self._tcp_reuse_send is not None
        return self.transport is not None

    def _send_dialog_request(self, raw: bytes, host: str, port: int) -> bool:
        try:
            if self.signaling_transport == "TCP":
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

    def _response_matches_transaction(
        self,
        message: sip.SipMessage,
        *,
        method: str,
        cseq: int,
        branch: str,
    ) -> bool:
        if not message.is_response:
            return False
        try:
            response_cseq = sip.parse_cseq(message.header("CSeq"))
            via_values = message.header_values("Via")
            response_branch = sip.parse_via(via_values[0] if via_values else "").branch
        except (TypeError, ValueError, sip.SipError):
            return False
        return (
            response_cseq.method == method.upper()
            and response_cseq.number == int(cseq)
            and bool(branch)
            and response_branch == branch
        )

    def _next_dialog_cseq(self) -> int:
        self._local_dialog_cseq = max(
            self._local_dialog_cseq,
            self._invite_cseq,
            self._bye_cseq,
        ) + 1
        return self._local_dialog_cseq

    def _ack_retransmitted_invite_2xx(self, message: sip.SipMessage) -> bool:
        dialog = self.dialog
        if (
            dialog is None
            or message.status_code is None
            or not 200 <= message.status_code < 300
            or not self._response_matches_transaction(
                message,
                method="INVITE",
                cseq=self._invite_cseq,
                branch=self.dialog_ids.branch,
            )
        ):
            return False
        self._send_ack(
            dialog.remote_host,
            dialog.remote_sip_port,
            dialog.remote_target_uri or dialog.remote_uri,
            dialog.local_uri,
            dialog.remote_uri,
            route_set=dialog.route_set,
        )
        return True

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
        headers = [
            *(("Via", value) for value in request.header_values("Via")),
            ("From", request.header("From")),
            ("To", request.header("To")),
            ("Call-ID", request.header("Call-ID")),
            ("CSeq", request.header("CSeq")),
        ]
        if 200 <= int(status) < 300 and request.method in {"INVITE", "UPDATE"}:
            headers.append(("Contact", f"<{self.dialog.local_uri}>" if self.dialog else f"<{self._pending_local_uri}>"))
        if body:
            headers.append(("Content-Type", "application/sdp"))
        headers.extend(extra_headers)
        raw = sip.build_response(status, reason, headers, body)
        if not self._send_dialog_request(raw, host, int(port)):
            _LOGGER.warning("SIP TX %s %s dropped: signaling path unavailable", status, reason)
            return False
        sip.mark_sip_event(self, "SIP_RESPONSE", int(status), reason)
        _LOGGER.info("SIP TX %s %s to %s:%s", status, reason, host, port)
        return True

    def _cancel_uas_invite_2xx(self) -> None:
        task = self._uas_invite_2xx_task
        self._uas_invite_2xx_task = None
        self._uas_invite_2xx_request = None
        self._uas_invite_2xx_host = ""
        self._uas_invite_2xx_port = 0
        self._uas_invite_2xx_status = 0
        self._uas_invite_2xx_reason = ""
        self._uas_invite_2xx_extra_headers = ()
        self._uas_invite_2xx_body = b""
        self._uas_invite_2xx_cseq = 0
        if task is not None and task is not asyncio.current_task():
            task.cancel()

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
        try:
            cseq = sip.parse_cseq(request.header("CSeq"))
        except (TypeError, ValueError, sip.SipError):
            return
        if request.method != "INVITE" or cseq.method != "INVITE" or not 200 <= status < 300:
            return
        self._cancel_uas_invite_2xx()
        self._uas_invite_ack_timeout.clear()
        self._uas_invite_2xx_request = request
        self._uas_invite_2xx_host = host
        self._uas_invite_2xx_port = int(port)
        self._uas_invite_2xx_status = int(status)
        self._uas_invite_2xx_reason = reason
        self._uas_invite_2xx_extra_headers = extra_headers
        self._uas_invite_2xx_body = body
        self._uas_invite_2xx_cseq = cseq.number
        self._uas_invite_2xx_retransmissions = 0
        self._uas_invite_2xx_task = asyncio.create_task(
            self._run_uas_invite_2xx(),
            name=f"voip-sip-client-2xx-{self.dialog_ids.call_id}",
        )

    async def _run_uas_invite_2xx(self) -> None:
        try:
            def _retransmit_final() -> bool:
                request = self._uas_invite_2xx_request
                if request is None:
                    return False
                sent = self._send_response_to_request(
                    request,
                    self._uas_invite_2xx_host,
                    self._uas_invite_2xx_port,
                    self._uas_invite_2xx_status,
                    self._uas_invite_2xx_reason,
                    extra_headers=self._uas_invite_2xx_extra_headers,
                    body=self._uas_invite_2xx_body,
                )
                if sent:
                    self._uas_invite_2xx_retransmissions += 1
                return sent

            result = await async_run_server_transaction(
                send=_retransmit_final,
                active=lambda: self._uas_invite_2xx_request is not None,
                transport=self.signaling_transport,
                timeout=SIP_TIMER_B,
                t1=SIP_T1,
                t2=SIP_T2,
                retransmit_reliable=True,
            )
        except asyncio.CancelledError:
            return
        finally:
            if self._uas_invite_2xx_task is asyncio.current_task():
                self._uas_invite_2xx_task = None

        if self._uas_invite_2xx_request is None or not result.timed_out:
            return
        _LOGGER.warning(
            "SIP remote re-INVITE ACK timed out call_id=%s cseq=%s; terminating dialog",
            self.dialog_ids.call_id,
            self._uas_invite_2xx_cseq,
        )
        dialog = self.dialog
        self._cancel_uas_invite_2xx()
        if dialog is not None:
            self._send_bye_request(
                dialog.remote_host,
                dialog.remote_sip_port,
                dialog.remote_target_uri or dialog.remote_uri,
                dialog.local_uri,
                dialog.remote_uri,
                route_set=dialog.route_set,
            )
        self.dialog = None
        self._uas_invite_ack_timeout.set()

    def _acknowledges_uas_invite_2xx(self, request: sip.SipMessage, host: str) -> bool:
        if self._uas_invite_2xx_request is None or request.method != "ACK":
            return False
        try:
            cseq = sip.parse_cseq(request.header("CSeq"))
        except (TypeError, ValueError, sip.SipError):
            return False
        if (
            cseq.method != "ACK"
            or cseq.number != self._uas_invite_2xx_cseq
            or not self._request_matches_dialog(request, host, "ACK")
        ):
            return False
        self._cancel_uas_invite_2xx()
        self._uas_invite_ack_timeout.clear()
        return True

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

    @staticmethod
    def _same_in_dialog_transaction(current: sip.SipMessage, previous: sip.SipMessage | None) -> bool:
        if previous is None:
            return False
        try:
            current_cseq = sip.parse_cseq(current.header("CSeq"))
            previous_cseq = sip.parse_cseq(previous.header("CSeq"))
            current_via = current.header_values("Via")
            previous_via = previous.header_values("Via")
            current_branch = sip.parse_via(current_via[0] if current_via else "").branch
            previous_branch = sip.parse_via(previous_via[0] if previous_via else "").branch
        except (TypeError, ValueError, sip.SipError):
            return False
        return bool(
            current.method == previous.method
            and current_cseq == previous_cseq
            and current_branch
            and current_branch == previous_branch
        )

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

    def _answer_remote_offer(self, request: sip.SipMessage) -> tuple[SipDialog, str] | None:
        """Build an answer and immutable replacement dialog for one remote offer."""

        current = self.dialog
        if current is None:
            return None
        try:
            selected = sdp.negotiate_directional(
                request.body,
                self.supported_send_formats,
                self.supported_recv_formats,
                allow_dahua_pcm=self.include_dahua_pcm,
            )
            if selected is None:
                return None
            parsed = sdp.parse_sdp(request.body)
            accepted_video = tuple(dict.fromkeys(fmt.encoding for fmt in self.video_formats))
            video_directional = (
                sdp.negotiate_video_offer_directional(
                    request.body,
                    local_formats=self.video_formats,
                    accepted_encodings=accepted_video,
                    prefer_browser_send=self.video_direction in {"sendonly", "sendrecv"},
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
            answer = sdp.build_answer_directional(
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
            if sdp.sdp_description_changed(current.local_sdp_body, answer):
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
                dtmf_payload_type=(dtmf_formats[0].payload_type if dtmf_formats else None),
                dtmf_clock_rate=(dtmf_formats[0].sample_rate if dtmf_formats else 8000),
                dtmf_events=(dtmf_formats[0].events if dtmf_formats else frozenset()),
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

    async def _read_response(self, timeout: float) -> tuple[sip.SipMessage, tuple[str, int]] | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, float(timeout))
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                if self.signaling_transport == "TCP":
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

    async def invite(
        self,
        *,
        target: str,
        remote_host: str,
        remote_sip_port: int,
        request_uri: str = "",
        timeout: float = 8.0,
    ) -> str:
        """Run one owned INVITE transaction that survives caller-task cancellation."""
        if self._closing or self._closed:
            raise RuntimeError("SIP client is already closed")
        if self._invite_task is not None and not self._invite_task.done():
            raise RuntimeError("INVITE transaction already active")
        task = asyncio.create_task(
            self._run_invite(
                target=target,
                remote_host=remote_host,
                remote_sip_port=remote_sip_port,
                request_uri=request_uri,
                timeout=timeout,
            ),
            name=f"voip-sip-client-invite-{self.dialog_ids.call_id}",
        )
        self._invite_task = task
        try:
            return await asyncio.shield(task)
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
        timeout: float = 8.0,
    ) -> str:
        try:
            if self.signaling_transport == "TCP" and self._tcp_reuse_send is None:
                await self._connect_tcp(remote_host, int(remote_sip_port))
            else:
                await self.start()
        except (ConnectionError, OSError, RuntimeError) as err:
            return self._transport_failure(err, target, remote_host, remote_sip_port)
        transport_param = (("transport", self.signaling_transport.lower()),)
        request_uri = request_uri or str(sip.SipUri(target, remote_host, int(remote_sip_port), params=transport_param))
        sip.parse_sip_uri(request_uri)
        local_uri = str(sip.SipUri(self.local_name, self.local_ip, self.local_sip_port, params=transport_param))
        remote_uri = request_uri
        self._pending_target = target
        self._pending_remote_host = remote_host
        self._pending_remote_sip_port = int(remote_sip_port)
        self._pending_request_uri = request_uri
        self._pending_local_uri = local_uri
        self._pending_remote_uri = remote_uri
        self._invite_transaction_active = True
        self._cancel_requested = False
        self._cancel_sent = False
        self._received_provisional = False
        offer = sdp.build_offer_directional(
            self.local_ip,
            self.local_ip,
            self.local_rtp_port,
            self.supported_send_formats,
            self.supported_recv_formats,
            include_common_codecs=self.include_common_codecs,
            include_dahua_pcm=self.include_dahua_pcm,
            video_port=self.local_video_rtp_port,
            video_format=self.video_format,
            video_formats=self.video_formats,
            video_direction=self.video_direction,
        )
        offer = sdp.rewrite_sdp_origin(offer, self._sdp_session_id, 0)
        self._local_sdp_body = offer
        body = offer.encode()
        headers = sip.dialog_headers(
            request_uri=request_uri,
            local_uri=local_uri,
            remote_uri=remote_uri,
            dialog=self.dialog_ids,
            method="INVITE",
            contact_uri=local_uri,
            content_type="application/sdp",
            transport=self.signaling_transport,
        )
        caller_name = _sip_header_token(self.local_name)
        dest_name = _sip_header_token(target)
        if caller_name:
            headers.append(("X-Voip-Stack-Caller-Name", caller_name))
            headers.append(("X-Voip-Stack-Caller-Route", caller_name))
        if dest_name:
            headers.append(("X-Voip-Stack-Dest-Name", dest_name))
            headers.append(("X-Voip-Stack-Dest-Route", dest_name))
        self._invite_cseq = self.dialog_ids.cseq
        raw = sip.build_request("INVITE", request_uri, headers, body)
        sip.mark_sip_event(self, "INVITE")
        try:
            await self._send_raw(raw, remote_host, int(remote_sip_port))
        except (ConnectionError, OSError, RuntimeError) as err:
            self._invite_transaction_active = False
            return self._transport_failure(err, target, remote_host, remote_sip_port)
        _LOGGER.info(
            "SIP TX INVITE %s@%s:%s offered=[%s]",
            target,
            remote_host,
            remote_sip_port,
            ", ".join(sdp.offered_media_descriptions(body)),
        )
        transaction = SipClientTransaction[
            tuple[sip.SipMessage, tuple[str, int]]
        ](
            transport=self.signaling_transport,
            timeout=timeout,
            t1=SIP_T1,
            t2=SIP_T2,
        )
        auth_retried = False
        received_provisional = False

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
                    return "timeout"
                msg, addr = received
            except (ConnectionError, OSError, RuntimeError) as err:
                return self._transport_failure(err, target, remote_host, remote_sip_port)
            except Exception as err:
                _LOGGER.info("SIP RX malformed: %s", err)
                continue
            if not msg.is_response:
                continue
            if not self._response_matches_transaction(
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
                if self._cancel_requested and not self._cancel_sent:
                    self._send_cancel()
                if _is_invite_progress_response(msg.status_code):
                    if self._cancel_requested:
                        continue
                    return "ringing"
                continue
            if msg.status_code and 200 <= msg.status_code < 300:
                if not self._commit_200_ok(msg, target, remote_host, int(remote_sip_port), request_uri, local_uri, remote_uri):
                    return (
                        "cancelled"
                        if self._closing or self._closed
                        else "media_incompatible"
                    )
                if self._cancel_requested:
                    self.bye()
                    return "cancelled"
                return "in_call"
            if msg.status_code in {401, 407} and self.password and not auth_retried:
                self._send_invite_error_ack(msg, addr[0], addr[1])
                if self._cancel_requested:
                    self._invite_transaction_active = False
                    return "cancelled"
                auth_retried = True
                auth_header = "Proxy-Authorization" if msg.status_code == 407 else "Authorization"
                challenge = msg.header("Proxy-Authenticate" if msg.status_code == 407 else "WWW-Authenticate")
                try:
                    auth_value = build_digest_authorization(
                        challenge_header=challenge,
                        username=self.username,
                        auth_username=self.auth_username,
                        password=self.password,
                        method="INVITE",
                        uri=request_uri,
                    )
                except Exception as err:
                    _LOGGER.info("SIP digest auth failed to build INVITE response: %s", err)
                    return sip.sip_failure_reason(msg.status_code)
                self.dialog_ids.cseq += 1
                self.dialog_ids.branch = sip.make_branch()
                self._invite_cseq = self.dialog_ids.cseq
                retry_headers = sip.dialog_headers(
                    request_uri=request_uri,
                    local_uri=local_uri,
                    remote_uri=remote_uri,
                    dialog=self.dialog_ids,
                    method="INVITE",
                    contact_uri=local_uri,
                    content_type="application/sdp",
                    transport=self.signaling_transport,
                )
                if caller_name:
                    retry_headers.append(("X-Voip-Stack-Caller-Name", caller_name))
                    retry_headers.append(("X-Voip-Stack-Caller-Route", caller_name))
                if dest_name:
                    retry_headers.append(("X-Voip-Stack-Dest-Name", dest_name))
                    retry_headers.append(("X-Voip-Stack-Dest-Route", dest_name))
                retry_headers.append((auth_header, auth_value))
                raw = sip.build_request("INVITE", request_uri, retry_headers, body)
                sip.mark_sip_event(self, "INVITE")
                try:
                    await self._send_raw(raw, remote_host, int(remote_sip_port))
                except (ConnectionError, OSError, RuntimeError) as err:
                    return self._transport_failure(err, target, remote_host, remote_sip_port)
                transaction.restart_retransmissions()
                received_provisional = False
                continue
            if msg.status_code and msg.status_code >= 300:
                self._send_invite_error_ack(msg, addr[0], addr[1])
                self._invite_transaction_active = False
                return _sip_decline_reason(msg) or sip.sip_failure_reason(msg.status_code)

    async def wait_for_final(self, timeout: float = 60.0) -> str:
        """Continue an INVITE through one owned final-response transaction."""

        if self._closing or self._closed:
            return "cancelled"
        if self.dialog is not None:
            return "in_call"
        if (
            self._final_response_task is not None
            and not self._final_response_task.done()
        ):
            raise RuntimeError("final INVITE response waiter already active")
        task = asyncio.create_task(
            self._run_wait_for_final(timeout),
            name=f"voip-sip-client-final-{self.dialog_ids.call_id}",
        )
        self._final_response_task = task
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            self.request_cancel()
            raise

    async def _run_wait_for_final(self, timeout: float = 60.0) -> str:
        """Own the response stream after provisional INVITE progress."""

        if self.dialog is not None:
            return "in_call"
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return "timeout"
            try:
                received = await self._read_response(remaining)
                if received is None:
                    return "timeout"
                msg, addr = received
            except Exception:
                continue
            if not msg.is_response or msg.status_code is None:
                continue
            if not self._response_matches_transaction(
                msg,
                method="INVITE",
                cseq=self._invite_cseq,
                branch=self.dialog_ids.branch,
            ):
                continue
            sip.mark_sip_event(self, "SIP_RESPONSE", int(msg.status_code), msg.reason)
            _LOGGER.info("SIP RX %s %s from %s:%s", msg.status_code, msg.reason, addr[0], addr[1])
            if _is_invite_progress_response(msg.status_code):
                continue
            if 200 <= msg.status_code < 300:
                if not self._commit_200_ok(
                    msg,
                    self._pending_target,
                    self._pending_remote_host or addr[0],
                    self._pending_remote_sip_port,
                    self._pending_request_uri,
                    self._pending_local_uri,
                    self._pending_remote_uri,
                ):
                    return (
                        "cancelled"
                        if self._closing or self._closed
                        else "media_incompatible"
                    )
                return "in_call"
            if msg.status_code >= 300:
                self._send_invite_error_ack(msg, addr[0], addr[1])
                self._invite_transaction_active = False
                return _sip_decline_reason(msg) or sip.sip_failure_reason(msg.status_code)

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
        if not request.body:
            status = 200 if method == "UPDATE" else 488
            reason = "OK" if status == 200 else "Not Acceptable Here"
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
                        commit = await self.on_media_update(self.dialog, updated, method)
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
            return "transport_unreachable"
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
            if commit is not None:
                try:
                    await commit()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _LOGGER.exception(
                        "SIP remote media update commit failed call_id=%s method=%s",
                        self.dialog_ids.call_id,
                        method,
                    )
                    self._cancel_uas_invite_2xx()
                    self.bye()
                    return "media_update_failed"
            if self._uas_invite_ack_timeout.is_set() or self.dialog is None:
                return "ack_timeout"
            self.dialog = updated
        elif (
            200 <= status < 300
            and refreshed_remote_target
            and self.dialog is not None
        ):
            self.dialog = replace(
                self.dialog,
                remote_target_uri=refreshed_remote_target,
            )
        return None

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
            wait_timeout = 3600.0
            if deadline is not None:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return "timeout"
                wait_timeout = max(0.05, remaining)
            read_task: asyncio.Task[
                tuple[sip.SipMessage, tuple[str, int]] | None
            ] | None = None
            ack_timeout_task: asyncio.Task[bool] | None = None
            try:
                read_task = asyncio.create_task(
                    self._read_response(wait_timeout),
                    name=f"voip-sip-dialog-read-{self.dialog_ids.call_id}",
                )
                ack_timeout_task = asyncio.create_task(
                    self._uas_invite_ack_timeout.wait(),
                    name=f"voip-sip-dialog-ack-timeout-{self.dialog_ids.call_id}",
                )
                done, _pending = await asyncio.wait(
                    {read_task, ack_timeout_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if ack_timeout_task in done and ack_timeout_task.result():
                    if read_task in done:
                        read_task.result()
                    return "ack_timeout"
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
                    for task in (read_task, ack_timeout_task)
                    if isinstance(task, asyncio.Task)
                )
                for task in child_tasks:
                    if not task.done():
                        task.cancel()
                if child_tasks:
                    cleanup = asyncio.gather(*child_tasks, return_exceptions=True)
                    await async_wait_for_cleanup(cleanup)
            if received is None:
                if deadline is not None:
                    return "timeout"
                continue
            msg, addr = received
            if msg.is_response:
                if msg.status_code is not None:
                    sip.mark_sip_event(self, "SIP_RESPONSE", int(msg.status_code), msg.reason)
                    _LOGGER.info("SIP RX %s %s from %s:%s", msg.status_code, msg.reason, addr[0], addr[1])
                    self._ack_retransmitted_invite_2xx(msg)
                continue
            if msg.method == "BYE":
                _LOGGER.info("SIP RX BYE from %s:%s", addr[0], addr[1])
                if not self._request_matches_dialog(msg, addr[0], "BYE"):
                    self._send_response_to_request(msg, addr[0], addr[1], 481, "Call/Transaction Does Not Exist")
                    continue
                try:
                    bye_cseq = sip.parse_cseq(msg.header("CSeq"))
                except (TypeError, ValueError, sip.SipError):
                    self._send_response_to_request(msg, addr[0], addr[1], 400, "Bad Request")
                    continue
                if bye_cseq.number <= self._remote_cseq:
                    self._send_response_to_request(
                        msg,
                        addr[0],
                        addr[1],
                        500,
                        "Server Internal Error",
                        extra_headers=(("Retry-After", "1"),),
                    )
                    continue
                self._cancel_uas_invite_2xx()
                self._send_response_to_request(msg, addr[0], addr[1], 200, "OK")
                self._remote_cseq = bye_cseq.number
                self.dialog = None
                return "remote_hangup"
            if msg.method == "CANCEL":
                _LOGGER.info("SIP RX CANCEL from %s:%s", addr[0], addr[1])
                # CANCEL only applies to an early INVITE transaction. Once the
                # dialog is confirmed it must not tear the call down.
                self._send_response_to_request(msg, addr[0], addr[1], 481, "Call/Transaction Does Not Exist")
                continue
            if msg.method == "ACK":
                if self._acknowledges_uas_invite_2xx(msg, addr[0]):
                    _LOGGER.info(
                        "SIP RX ACK remote re-INVITE call_id=%s cseq=%s",
                        self.dialog_ids.call_id,
                        sip.parse_cseq(msg.header("CSeq")).number,
                    )
                continue
            if msg.method in {"INVITE", "UPDATE"}:
                terminal = await self._handle_dialog_media_request(
                    msg, addr[0], addr[1]
                )
                if terminal is not None:
                    return terminal
                continue
            if msg.method in {"INFO", "OPTIONS"}:
                if not self._request_matches_dialog(msg, addr[0], msg.method):
                    self._send_response_to_request(
                        msg,
                        addr[0],
                        addr[1],
                        481,
                        "Call/Transaction Does Not Exist",
                    )
                    continue
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
                    continue
                try:
                    request_cseq = sip.parse_cseq(msg.header("CSeq"))
                except (TypeError, ValueError, sip.SipError):
                    self._send_response_to_request(msg, addr[0], addr[1], 400, "Bad Request")
                    continue
                if request_cseq.number <= self._remote_cseq:
                    self._send_response_to_request(
                        msg,
                        addr[0],
                        addr[1],
                        500,
                        "Server Internal Error",
                        extra_headers=(("Retry-After", "1"),),
                    )
                    continue
                if msg.method == "INFO":
                    from .dtmf import parse_sip_info_digit

                    digit = parse_sip_info_digit(msg.header("Content-Type"), msg.body)
                    if digit and self.on_info_dtmf is not None:
                        try:
                            self.on_info_dtmf(digit)
                        except Exception as err:  # noqa: BLE001 - event consumers cannot break the SIP dialog.
                            _LOGGER.warning("SIP INFO DTMF callback failed: %s", err)
                self._send_response_to_request(msg, addr[0], addr[1], 200, "OK")
                self._remember_in_dialog_response(msg, 200, "OK")
                self._remote_cseq = request_cseq.number
                continue
            self._send_response_to_request(msg, addr[0], addr[1], 405, "Method Not Allowed")

    def _commit_200_ok(
        self,
        msg: sip.SipMessage,
        target: str,
        remote_host: str,
        remote_sip_port: int,
        request_uri: str,
        local_uri: str,
        remote_uri: str,
    ) -> bool:
        self._invite_transaction_active = False
        if not request_uri:
            request_uri = str(sip.SipUri(target or "voip", remote_host, remote_sip_port))
        transport_param = (("transport", self.signaling_transport.lower()),)
        if not local_uri:
            local_uri = str(sip.SipUri(self.local_name, self.local_ip, self.local_sip_port, params=transport_param))
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
        try:
            route_set = sip.record_route_set(msg, reverse=True)
        except (TypeError, ValueError, sip.SipError):
            route_set = ()
            _LOGGER.info(
                "SIP 200 OK has invalid Record-Route; using direct dialog routing"
            )
        if self._closing or self._closed:
            # A 2xx terminates the INVITE transaction even when local teardown
            # won the race.  ACK it and immediately end the just-created remote
            # dialog, but never publish that dialog into a closing client.
            self.dialog_ids.remote_tag = sip.extract_tag(msg.header("To"))
            self._send_ack(
                remote_host,
                int(remote_sip_port),
                remote_target_uri,
                local_uri,
                remote_uri,
                route_set=route_set,
            )
            self._send_bye_request(
                remote_host,
                int(remote_sip_port),
                remote_target_uri,
                local_uri,
                remote_uri,
                route_set=route_set,
            )
            return False
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
            self.dialog_ids.remote_tag = sip.extract_tag(msg.header("To"))
            self._send_ack(
                remote_host,
                int(remote_sip_port),
                remote_target_uri,
                local_uri,
                remote_uri,
                route_set=route_set,
            )
            self._send_bye_request(
                remote_host,
                int(remote_sip_port),
                remote_target_uri,
                local_uri,
                remote_uri,
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
        dtmf_format = (
            sdp.negotiate_dtmf_answer(msg.body, self._local_sdp_body)
            if self._local_sdp_body
            else next(iter(answered_dtmf_formats), None)
        )
        self.dialog_ids.remote_tag = sip.extract_tag(msg.header("To"))
        self.dialog = SipDialog(
            target=target,
            remote_host=remote_host,
            remote_sip_port=int(remote_sip_port),
            remote_rtp_host=parsed["connection_ip"],
            remote_rtp_port=int(parsed["media_port"]),
            local_rtp_port=self.local_rtp_port,
            call_id=self.dialog_ids.call_id,
            local_uri=local_uri,
            remote_uri=remote_uri,
            send_format=selected.send,
            recv_format=selected.recv,
            remote_target_uri=remote_target_uri,
            route_set=route_set,
            dtmf_payload_type=(dtmf_format.payload_type if dtmf_format else None),
            dtmf_clock_rate=(dtmf_format.sample_rate if dtmf_format else 8000),
            dtmf_events=(dtmf_format.events if dtmf_format else frozenset()),
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
        )
        _LOGGER.info(
            "SIP 200 OK media selected call_id=%s tx=%s rx=%s answer=[%s]",
            self.dialog_ids.call_id,
            selected.send.wire_token(),
            selected.recv.wire_token(),
            ", ".join(sdp.offered_media_descriptions(msg.body)),
        )
        self._send_ack(
            remote_host,
            int(remote_sip_port),
            remote_target_uri,
            local_uri,
            remote_uri,
            route_set=route_set,
        )
        return True

    def _send_ack(
        self,
        host: str,
        port: int,
        request_uri: str,
        local_uri: str,
        remote_uri: str,
        *,
        route_set: tuple[str, ...] = (),
    ) -> bool:
        if not self._has_signaling_path():
            return False
        try:
            routing = sip.dialog_request_routing(request_uri, route_set)
        except (TypeError, ValueError, sip.SipError) as err:
            _LOGGER.warning("SIP ACK routing rejected: %s", err)
            return False
        ack_ids = sip.SipDialogIds(
            call_id=self.dialog_ids.call_id,
            local_tag=self.dialog_ids.local_tag,
            remote_tag=self.dialog_ids.remote_tag,
            cseq=self._invite_cseq,
            branch=sip.make_branch(),
        )
        headers = sip.dialog_headers(
            request_uri=routing.request_uri,
            local_uri=local_uri,
            remote_uri=remote_uri,
            dialog=ack_ids,
            method="ACK",
            contact_uri=local_uri,
            transport=self.signaling_transport,
        )
        headers.extend(("Route", value) for value in routing.route_headers)
        raw = sip.build_request("ACK", routing.request_uri, headers, b"")
        next_host, next_port = self._dialog_next_hop(
            routing.next_hop_uri,
            host,
            int(port),
        )
        if not self._send_dialog_request(raw, next_host, next_port):
            _LOGGER.warning("SIP TX ACK dropped: signaling path unavailable")
            return False
        sip.mark_sip_event(self, "ACK")
        _LOGGER.info("SIP TX ACK %s:%s", next_host, next_port)
        return True

    def _send_invite_error_ack(self, msg: sip.SipMessage, host: str, port: int) -> None:
        if not self._has_signaling_path():
            return
        request_uri = self._pending_request_uri
        local_uri = self._pending_local_uri
        remote_uri = self._pending_remote_uri
        if not request_uri or not local_uri or not remote_uri:
            return
        ack_ids = sip.SipDialogIds(
            call_id=self.dialog_ids.call_id,
            local_tag=self.dialog_ids.local_tag,
            remote_tag=sip.extract_tag(msg.header("To")),
            cseq=self._invite_cseq,
            branch=self.dialog_ids.branch,
        )
        headers = sip.dialog_headers(
            request_uri=request_uri,
            local_uri=local_uri,
            remote_uri=remote_uri,
            dialog=ack_ids,
            method="ACK",
            contact_uri=local_uri,
            transport=self.signaling_transport,
        )
        raw = sip.build_request("ACK", request_uri, headers, b"")
        if not self._send_dialog_request(raw, host, int(port)):
            _LOGGER.warning("SIP TX ACK final INVITE error dropped: signaling path unavailable")
            return
        sip.mark_sip_event(self, "ACK")
        _LOGGER.info("SIP TX ACK final INVITE error %s:%s", host, port)

    def _send_bye_request(
        self,
        host: str,
        port: int,
        request_uri: str,
        local_uri: str,
        remote_uri: str,
        *,
        route_set: tuple[str, ...] = (),
    ) -> bool:
        if not self._has_signaling_path() or not request_uri or not local_uri or not remote_uri:
            return False
        try:
            routing = sip.dialog_request_routing(request_uri, route_set)
        except (TypeError, ValueError, sip.SipError) as err:
            _LOGGER.warning("SIP BYE routing rejected: %s", err)
            return False
        bye_ids = sip.SipDialogIds(
            call_id=self.dialog_ids.call_id,
            local_tag=self.dialog_ids.local_tag,
            remote_tag=self.dialog_ids.remote_tag,
            cseq=self._next_dialog_cseq(),
            branch=sip.make_branch(),
        )
        headers = sip.dialog_headers(
            request_uri=routing.request_uri,
            local_uri=local_uri,
            remote_uri=remote_uri,
            dialog=bye_ids,
            method="BYE",
            contact_uri=local_uri,
            transport=self.signaling_transport,
        )
        headers.extend(("Route", value) for value in routing.route_headers)
        raw = sip.build_request("BYE", routing.request_uri, headers, b"")
        next_host, next_port = self._dialog_next_hop(
            routing.next_hop_uri,
            host,
            int(port),
        )
        if not self._send_dialog_request(raw, next_host, next_port):
            _LOGGER.warning("SIP TX BYE dropped: signaling path unavailable")
            return False
        self._bye_cseq = bye_ids.cseq
        self._bye_branch = bye_ids.branch
        sip.mark_sip_event(self, "BYE")
        _LOGGER.info("SIP TX BYE %s:%s", next_host, next_port)
        return True

    def bye(self) -> bool:
        if self.dialog is None:
            return False
        self._cancel_uas_invite_2xx()
        return self._send_bye_request(
            self.dialog.remote_host,
            self.dialog.remote_sip_port,
            self.dialog.remote_target_uri or self.dialog.remote_uri,
            self.dialog.local_uri,
            self.dialog.remote_uri,
            route_set=self.dialog.route_set,
        )

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
        ends with CANCEL + 200 OK to CANCEL and a final 487 for the INVITE.
        Keeping the socket alive for that exchange avoids leaving ESP phones in
        ringing state while HA already moved back to idle.
        """
        if self.dialog is not None:
            if not self.bye():
                return "transport_unreachable"
            deadline = asyncio.get_running_loop().time() + timeout
            while asyncio.get_running_loop().time() < deadline:
                try:
                    received = await self._read_response(max(0.05, deadline - asyncio.get_running_loop().time()))
                except asyncio.TimeoutError:
                    break
                except Exception:
                    continue
                if received is None:
                    break
                msg, addr = received
                if not msg.is_response or msg.status_code is None:
                    continue
                sip.mark_sip_event(self, "SIP_RESPONSE", int(msg.status_code), msg.reason)
                _LOGGER.info("SIP RX %s %s from %s:%s", msg.status_code, msg.reason, addr[0], addr[1])
                if self._ack_retransmitted_invite_2xx(msg):
                    continue
                if self._response_matches_transaction(
                    msg,
                    method="BYE",
                    cseq=self._bye_cseq,
                    branch=self._bye_branch,
                ) and 200 <= msg.status_code < 300:
                    self.dialog = None
                    return "remote_hangup"
            return "timeout"

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
        saw_invite_terminated = False
        cancel_race_bye_sent = False
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                received = await self._read_response(max(0.05, deadline - asyncio.get_running_loop().time()))
            except asyncio.TimeoutError:
                break
            except Exception:
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
            elif cseq_method == "BYE":
                if not self._response_matches_transaction(
                    msg,
                    method="BYE",
                    cseq=self._bye_cseq,
                    branch=self._bye_branch,
                ):
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
                    self.bye()
                cancel_race_bye_sent = True
            elif cseq_method == "INVITE" and msg.status_code >= 300:
                self._send_invite_error_ack(msg, addr[0], addr[1])
                self._invite_transaction_active = False
                saw_invite_terminated = True
            elif cseq_method == "BYE" and cancel_race_bye_sent and 200 <= msg.status_code < 300:
                self.dialog = None
                return "cancelled"
            if saw_cancel_ok and saw_invite_terminated:
                return "cancelled"
        if saw_cancel_ok or saw_invite_terminated or cancel_race_bye_sent:
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
            "pending_remote_invite_ack": self._uas_invite_2xx_cseq,
            "remote_invite_2xx_retransmissions": self._uas_invite_2xx_retransmissions,
            "sip_transport": self.signaling_transport.lower(),
            "last_sip_event": self.last_sip_event,
            "last_sip_status_code": self.last_sip_status_code,
            "last_sip_reason": self.last_sip_reason,
        }
