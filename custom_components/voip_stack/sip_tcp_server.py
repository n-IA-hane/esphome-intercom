"""Own TCP connections and tasks independently of the SIP request protocol.

The endpoint factory is supplied by composition. This module never imports the
listener protocol at runtime and creates one endpoint for the whole listener,
not one dialog owner per TCP connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
from typing import TYPE_CHECKING, Any, Callable

from .core import sip
from .core.audio_format import AudioFormat
from .core.sip_transport import bind_flow_usage
from .queue_utils import put_drop_oldest
from .sip_tcp_io import SipTcpWriter, read_sip_stream_message as _read_sip_stream_message

if TYPE_CHECKING:
    from .sip_listener import (
        ApplicationRequestRouter, InfoHandler, InviteAuthorizer, InviteHandler,
        MediaUpdateHandler, OfferlessInviteHandler, ReferHandler, RegisterHandler,
        RequestHandler, SipUdpEndpoint, TcpDialogSender, TerminateHandler,
    )

# Keep the established category for existing per-module logging settings.
_LOGGER = logging.getLogger(f"{__package__}.sip_listener")


class SipTcpServer:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        local_ip: str,
        local_rtp_port: int,
        supported_formats: list[AudioFormat],
        supported_send_formats: list[AudioFormat] | None = None,
        supported_recv_formats: list[AudioFormat] | None = None,
        on_invite: InviteHandler,
        on_offerless_invite: OfferlessInviteHandler | None = None,
        codec_policy_resolver=None,
        codec_policy=None,
        invite_identity_resolver=None,
        invite_authorizer: InviteAuthorizer | None = None,
        on_terminated: TerminateHandler | None = None,
        on_register: RegisterHandler | None = None,
        on_info: InfoHandler | None = None,
        on_media_update: MediaUpdateHandler | None = None,
        on_refer: ReferHandler | None = None,
        on_request: RequestHandler | None = None,
        request_router: ApplicationRequestRouter | None = None,
        on_flow_closed: Callable[[str, int, str], None] | None = None,
        enable_video: bool = False,
        enable_video_transcoding: bool = False,
        prefer_browser_video_send: bool = False,
        max_connections: int = 128,
        max_connections_per_host: int = 16,
        initial_message_timeout: float = 15.0,
        frame_timeout: float = 10.0,
        endpoint_factory: Callable[..., Any],
    ) -> None:
        self._endpoint_factory = endpoint_factory
        self.host = host
        self.port = port
        self.local_ip = local_ip
        self.local_rtp_port = local_rtp_port
        self.supported_formats = supported_formats
        self.supported_send_formats = supported_send_formats
        self.supported_recv_formats = supported_recv_formats
        self.on_invite = on_invite
        self.on_offerless_invite = on_offerless_invite
        self.codec_policy_resolver = codec_policy_resolver
        self.codec_policy = codec_policy
        self.invite_identity_resolver = invite_identity_resolver
        self.invite_authorizer = invite_authorizer
        self.on_terminated = on_terminated
        self.on_register = on_register
        self.on_info = on_info
        self.on_media_update = on_media_update
        self.on_refer = on_refer
        self.on_request = on_request
        self.request_router = request_router
        self.on_flow_closed = on_flow_closed
        self.enable_video = bool(enable_video)
        self.enable_video_transcoding = bool(enable_video_transcoding)
        self.prefer_browser_video_send = bool(prefer_browser_video_send)
        self.max_connections = max(1, int(max_connections))
        self.max_connections_per_host = max(1, int(max_connections_per_host))
        self.initial_message_timeout = max(0.1, float(initial_message_timeout))
        self.frame_timeout = max(0.1, float(frame_timeout))
        self.server: asyncio.AbstractServer | None = None
        self.endpoint: SipUdpEndpoint | None = None
        self.endpoints: set[SipUdpEndpoint] = set()
        self._writers: dict[tuple[str, int], asyncio.StreamWriter] = {}
        self._tcp_writers: dict[tuple[str, int], SipTcpWriter] = {}
        self._dialog_queues: dict[tuple[tuple[str, int], str], asyncio.Queue[bytes]] = {}
        self._client_tasks: set[asyncio.Task] = set()
        self._connections_by_host: dict[str, int] = {}

    async def start(self) -> bool:
        if self.server is not None:
            return True
        try:
            self.server = await asyncio.start_server(self._handle_client, self.host, self.port)
        except OSError as err:
            _LOGGER.error("Failed to bind SIP TCP %s:%s: %s", self.host, self.port, err)
            return False
        # A SIP dialog belongs to the listening user agent, not to one TCP
        # connection.  RFC 3261 explicitly allows subsequent in-dialog
        # requests to arrive over a different connection.  Keep one logical
        # endpoint for the whole TCP listener and select the current writer by
        # the source address of each request.
        def _send(data: bytes, addr: tuple[str, int]) -> bool:
            tx = self._tcp_writers.get((str(addr[0]), int(addr[1])))
            return tx is not None and tx.send_nowait(data)

        self.endpoint = self._endpoint_factory(
            local_ip=self.local_ip,
            local_sip_port=self.port,
            local_rtp_port=self.local_rtp_port,
            supported_formats=self.supported_formats,
            supported_send_formats=self.supported_send_formats,
            supported_recv_formats=self.supported_recv_formats,
            on_invite=self.on_invite,
            on_offerless_invite=self.on_offerless_invite,
            codec_policy_resolver=self.codec_policy_resolver,
            codec_policy=self.codec_policy,
            invite_identity_resolver=self.invite_identity_resolver,
            invite_authorizer=self.invite_authorizer,
            on_terminated=self.on_terminated,
            on_register=self.on_register,
            on_info=self.on_info,
            on_media_update=self.on_media_update,
            on_refer=self.on_refer,
            on_request=self.on_request,
            request_router=self.request_router,
            send_override=_send,
            request_route=self._request_route,
            transaction_flow=lambda addr: self._writers.get(addr),
            signaling_transport="TCP",
            enable_video=self.enable_video,
            enable_video_transcoding=self.enable_video_transcoding,
            prefer_browser_video_send=self.prefer_browser_video_send,
        )
        self.endpoints.add(self.endpoint)
        _LOGGER.info("SIP TCP listener ready on %s:%s", self.host, self.port)
        return True

    async def _request_route(self, target: sip.SipUri, source: tuple[str, int]) -> tuple[str, int]:
        from .core.sip_resolution import SipServerResolver

        async with asyncio.timeout(8.0):
            if self.server is None:
                raise ConnectionError("SIP TCP listener is stopped")
            candidates = await SipServerResolver().resolve(target, transport="TCP")
            connection_errors = []
            for candidate in candidates:
                for host, port, transport in candidate.endpoints():
                    if transport != "TCP":
                        continue
                    address = (host, port)
                    tx = self._tcp_writers.get(address)
                    if tx is not None and not tx.writer.is_closing() and not tx.task.done():
                        return address
                    if len(self._client_tasks) >= self.max_connections:
                        raise ConnectionError("SIP TCP connection budget exhausted")
                    try:
                        reader, writer = await asyncio.open_connection(host, port)
                    except OSError as err:
                        connection_errors.append(err)
                        continue
                    ready = asyncio.get_running_loop().create_future()
                    task = asyncio.create_task(self._handle_client(reader, writer, ready), name="voip-sip-outbound-flow")
                    task.add_done_callback(self._outbound_flow_done)
                    try:
                        published = await ready
                    except BaseException:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        raise
                    if published:
                        return published
                    await asyncio.gather(task, return_exceptions=True)
            if connection_errors and all(error.errno in {
                    errno.ECONNREFUSED, errno.ECONNRESET, errno.ENOPROTOOPT} for error in connection_errors):
                raise connection_errors[-1]
            raise ConnectionError("SIP subscription next hop is unreachable")

    @staticmethod
    def _outbound_flow_done(task):
        if not task.cancelled() and (error := task.exception()) is not None:
            _LOGGER.warning("Outgoing SIP subscription flow failed: %s", error)

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, ready=None) -> None:
        peer = writer.get_extra_info("peername") or ("0.0.0.0", 0)
        addr = (str(peer[0]), int(peer[1]))
        active_for_host = self._connections_by_host.get(addr[0], 0)
        if (
            len(self._client_tasks) >= self.max_connections
            or active_for_host >= self.max_connections_per_host
        ):
            _LOGGER.warning(
                "SIP TCP connection limit reached for %s (global=%s/%s host=%s/%s)",
                addr[0],
                len(self._client_tasks),
                self.max_connections,
                active_for_host,
                self.max_connections_per_host,
            )
            if ready is not None and not ready.done():
                ready.set_result(None)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return
        client_task = asyncio.current_task()
        if client_task is not None:
            self._client_tasks.add(client_task)
        self._connections_by_host[addr[0]] = active_for_host + 1
        self._writers[addr] = writer
        tx = SipTcpWriter(writer, label=f"listener {addr[0]}:{addr[1]}")
        tx.task.add_done_callback(lambda _task: writer.close())
        self._tcp_writers[addr] = tx
        if ready is not None and not ready.done():
            ready.set_result(addr)

        endpoint = self.endpoint
        if endpoint is None:
            await tx.close()
            writer.close()
            return
        first_message = True
        keepalive_half = False
        flow_error = None
        try:
            while not reader.at_eof():
                raw = await _read_sip_stream_message(
                    reader,
                    writer=writer,
                    first_byte_timeout=(
                        self.initial_message_timeout if first_message else None
                    ),
                    frame_timeout=self.frame_timeout,
                )
                if raw is None:
                    break
                if raw == b"\r\n":
                    if keepalive_half:
                        tx.send_nowait(b"\r\n")
                    keepalive_half = not keepalive_half
                    continue
                keepalive_half = False
                try:
                    msg = sip.parse_message(raw)
                except sip.SipError:
                    # Framing is complete; a bad request need not retire the flow.
                    envelope = sip.oversized_request_envelope(raw, 400)
                    if envelope is not None:
                        request, status = envelope
                        await tx.send(sip.build_uas_response(request, status, "Bad Request"))
                    continue
                first_message = False
                if msg is not None:
                    queue = self._dialog_queues.get((addr, msg.header("Call-ID")))
                    if queue is not None:
                        if put_drop_oldest(queue, raw):
                            _LOGGER.debug("SIP TCP dialog queue full for %s; dropped oldest message", addr)
                        continue
                endpoint.submit_datagram(raw, addr, parsed=msg)
        except sip.SipMessageTooLarge as err:
            oversized = sip.oversized_request_envelope(err.headers, err.status)
            if oversized is not None:
                request, status = oversized
                response = sip.build_uas_response(request, status,
                    "Request Entity Too Large" if status == 413 else "Message Too Large")
                await tx.send(response)
        except ConnectionError as err:
            flow_error = err
            _LOGGER.debug("SIP TCP peer disconnected %s:%s: %s", addr[0], addr[1], err)
        finally:
            # Closing one TCP connection must not destroy dialogs owned by
            # the listener; the peer may already have opened the replacement
            # connection used for a re-INVITE, ACK or BYE.
            # A reconnect can reuse the same advertised/source address before
            # this connection's cleanup callback runs.  Never let the stale
            # connection remove the replacement writer or its dialog queues.
            if self._writers.get(addr) is writer:
                self._writers.pop(addr, None)
                self._tcp_writers.pop(addr, None)
                for key in [key for key in self._dialog_queues if key[0] == addr]:
                    self._dialog_queues.pop(key, None)
                if self.on_flow_closed is not None:
                    try:
                        self.on_flow_closed(addr[0], addr[1], "TCP")
                    except Exception:
                        _LOGGER.exception(
                            "SIP TCP flow-close observer failed for %s:%s",
                            addr[0],
                            addr[1],
                        )
            if flow_error is None and (reader.at_eof() or writer.is_closing()):
                flow_error = ConnectionError("SIP TCP flow closed while a transaction was pending")
            endpoint.close_transaction_flow(writer, flow_error)
            await tx.close()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            if client_task is not None:
                self._client_tasks.discard(client_task)
            remaining = self._connections_by_host.get(addr[0], 1) - 1
            if remaining > 0:
                self._connections_by_host[addr[0]] = remaining
            else:
                self._connections_by_host.pop(addr[0], None)

    def open_reused_dialog(
        self,
        addr: tuple[str, int],
        call_id: str,
    ) -> tuple[TcpDialogSender, asyncio.Queue[bytes]] | None:
        writer = self._writers.get(addr)
        tx = self._tcp_writers.get(addr)
        if writer is None or writer.is_closing() or tx is None:
            return None
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=64)
        key = (addr, call_id)
        self._dialog_queues[key] = queue

        def _send(data: bytes) -> bool:
            return tx.send_nowait(data)

        return _send, queue

    def bind_reused_usage(self, addr: tuple[str, int], call_id: str, usage_id: str) -> Callable[[], None]:
        """Attach a subscription dialog to the existing registered TCP flow."""
        return bind_flow_usage(self._dialog_queues, (addr, call_id), (addr, usage_id))

    def close_reused_dialog(self, addr: tuple[str, int], call_id: str) -> None:
        self._dialog_queues.pop((addr, call_id), None)

    def send_final_response(
        self,
        call_id: str,
        status: int,
        reason: str,
        *,
        answer_sdp: str = "",
        decline_reason: str = "",
        connected_identity_name: str = "",
        connected_identity_user: str = "",
        media_contract=None,
    ) -> bool:
        for endpoint in tuple(self.endpoints):
            if endpoint.send_final_response(
                call_id,
                status,
                reason,
                answer_sdp=answer_sdp,
                decline_reason=decline_reason,
                connected_identity_name=connected_identity_name,
                connected_identity_user=connected_identity_user,
                media_contract=media_contract,
            ):
                return True
        return False

    def send_bye(self, call_id: str = "") -> bool:
        for endpoint in tuple(self.endpoints):
            if endpoint.send_bye(call_id):
                return True
        return False

    async def async_send_bye(self, call_id: str = "") -> bool:
        for endpoint in tuple(self.endpoints):
            if await endpoint.async_send_bye(call_id):
                return True
        return False

    async def stop(self) -> None:
        server = self.server
        self.server = None
        if server is not None:
            server.close()
        for tx in tuple(self._tcp_writers.values()):
            await tx.close()
        for writer in tuple(self._writers.values()):
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        current = asyncio.current_task()
        client_tasks = tuple(task for task in self._client_tasks if task is not current)
        if client_tasks:
            await asyncio.gather(*client_tasks, return_exceptions=True)
        if server is not None:
            await server.wait_closed()
        endpoint = self.endpoint
        self.endpoint = None
        if endpoint is not None:
            disconnected_calls = set(endpoint.pending_invites) | set(endpoint.active_dialogs)
            endpoint.cancel_request_tasks()
            await endpoint.wait_closed()
            if endpoint.on_terminated is not None:
                for call_id in disconnected_calls:
                    with contextlib.suppress(Exception):
                        await endpoint.on_terminated(call_id, "transport_closed")
            endpoint.pending_invites.clear()
            endpoint.completed_invites.clear()
            endpoint.active_dialogs.clear()
            endpoint.completed_byes.clear()
            endpoint.completed_infos.clear()
        self.endpoints.clear()
        self._writers.clear()
        self._tcp_writers.clear()
        self._dialog_queues.clear()
        self._client_tasks.clear()
        self._connections_by_host.clear()
