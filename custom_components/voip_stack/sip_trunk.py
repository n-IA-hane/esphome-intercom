"""SIP trunk registration for provider/PBX interop.

The registration TCP flow is replaceable transport state, not dialog state.
Confirmed inbound dialogs remain keyed by Call-ID and tags when a provider
reconnects; only explicit local shutdown, BYE or normal call lifecycle cleanup
may terminate them.  This separation is essential for in-dialog re-INVITE and
BYE requests delivered on a replacement connection.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import logging
import socket
import time
from typing import Any, Awaitable, Callable

from . import sip
from .sip_auth import build_digest_authorization
from .sip_transaction import SIP_T1, SIP_T2, SipClientTransaction
from .sip_udp_io import SipDatagramQueueProtocol
from .sip_tcp_io import SipTcpWriter, read_sip_stream_message as _read_sip_stream_message
from .queue_utils import put_drop_oldest
from .session_cleanup import async_wait_for_cleanup


_LOGGER = logging.getLogger(__name__)
_MAX_TRUNK_REQUEST_TASKS = 32
_MAX_TRUNK_INVITE_TASKS = 24

TrunkRequestHandler = Callable[[bytes, tuple[str, int]], Awaitable[None]]


def _registration_expires(message: sip.SipMessage, default: int) -> int:
    values = [message.header("Expires")]
    for contact in message.header_values("Contact"):
        for part in contact.split(";")[1:]:
            key, separator, value = part.partition("=")
            if separator and key.strip().lower() == "expires":
                values.insert(0, value.strip())
                break
    for value in values:
        try:
            return max(0, min(86400, int(value)))
        except (TypeError, ValueError):
            continue
    return max(0, int(default))


def _registration_refresh_delay(configured_expires: int, expires_at: float, now: float) -> float:
    """Refresh before the granted expiry, including short PBX bindings."""

    until_expiry = max(1.0, float(expires_at) - float(now) - 10.0)
    return max(1.0, min(float(configured_expires) * 0.8, until_expiry))


@dataclass(frozen=True, slots=True)
class SipTrunkConfig:
    enabled: bool
    transport: str
    server: str
    port: int
    domain: str
    username: str
    auth_username: str
    password: str
    expires: int
    outbound_proxy: str = ""


class SipTrunkClient:
    def __init__(self, *, config: SipTrunkConfig, local_ip: str, local_sip_port: int) -> None:
        self.config = config
        self.local_ip = local_ip
        self.local_sip_port = int(local_sip_port)
        self.transport_name = (config.transport or "udp").upper()
        self.queue: asyncio.Queue[tuple[bytes, tuple[str, int]]] = asyncio.Queue(maxsize=128)
        self.responses: asyncio.Queue[sip.SipMessage] = asyncio.Queue(maxsize=32)
        self.protocol: SipDatagramQueueProtocol | None = None
        self.transport: asyncio.DatagramTransport | None = None
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._tcp_writer: SipTcpWriter | None = None
        self._tcp_connect_lock = asyncio.Lock()
        self._reader_ready = asyncio.Event()
        self._refresh_wakeup = asyncio.Event()
        self._request_tasks: set[asyncio.Task[None]] = set()
        self._invite_tasks: set[asyncio.Task[None]] = set()
        self.call_id = sip.make_call_id("trunk-register")
        self.local_tag = sip.make_tag()
        self.cseq = 1
        self.registered = False
        self.status_code = 0
        self.status_reason = ""
        self.last_sip_event = ""
        self.expires_at = 0.0
        self._refresh_task: asyncio.Task | None = None
        self._receive_task: asyncio.Task | None = None
        self._stopped = False
        self._lifecycle_lock = asyncio.Lock()
        self._start_task: asyncio.Task[None] | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self.request_handler: TrunkRequestHandler | None = None
        self.inbound_endpoint: Any | None = None
        self._trusted_udp_hosts: frozenset[str] = frozenset()

    def _ensure_receive_task(self) -> None:
        if not self._stopped and (
            self._receive_task is None or self._receive_task.done()
        ):
            self._receive_task = asyncio.create_task(
                self._receive_loop(),
                name=f"voip-sip-trunk-receive-{self.call_id}",
            )

    @property
    def registrar_target(self) -> tuple[str, int]:
        proxy = str(self.config.outbound_proxy or "").strip()
        if not proxy:
            return self.config.server, int(self.config.port)
        try:
            uri = sip.parse_sip_uri(proxy if proxy.lower().startswith("sip:") else f"sip:{proxy}")
            return uri.host, int(uri.port or self.config.port)
        except (TypeError, ValueError, sip.SipError):
            return proxy, int(self.config.port)

    @property
    def registrar_host(self) -> str:
        return self.registrar_target[0]

    @property
    def registrar_port(self) -> int:
        return self.registrar_target[1]

    @property
    def domain(self) -> str:
        return self.config.domain or self.config.server

    @property
    def contact_uri(self) -> str:
        return str(sip.SipUri(self.config.username, self.local_ip, self.local_sip_port, params=(("transport", self.transport_name.lower()),)))

    @property
    def address_uri(self) -> str:
        return str(sip.SipUri(self.config.username, self.domain))

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._stopped:
                raise RuntimeError("SIP trunk has already been stopped")
            if self._start_task is None:
                self._start_task = asyncio.create_task(
                    self._start(),
                    name=f"voip-sip-trunk-start-{self.call_id}",
                )
            task = self._start_task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await async_wait_for_cleanup(task)
            await self.stop()
            raise

    async def _start(self) -> None:
        try:
            if self.transport_name == "TCP":
                await self._connect_tcp()
            else:
                await self._connect_udp()
            if self._stopped:
                return
            self._ensure_receive_task()
            await self.register(timeout=2.0)
        except Exception as err:
            if self._stopped:
                return
            self.registered = False
            self.status_code = 0
            self.status_reason = str(err)
            _LOGGER.warning(
                "SIP trunk initial registration failed server=%s transport=%s error=%s; background retry will continue",
                self.config.server,
                self.transport_name,
                err,
            )
        if not self._stopped:
            self._ensure_refresh_task()

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            self._stopped = True
            if self._stop_task is None:
                self._stop_task = asyncio.create_task(
                    self._stop(),
                    name=f"voip-sip-trunk-stop-{self.call_id}",
                )
            task = self._stop_task
        await async_wait_for_cleanup(task)

    async def _stop(self) -> None:
        self._stopped = True
        self._refresh_wakeup.set()
        start_task = self._start_task
        if start_task is not None and start_task is not asyncio.current_task() and not start_task.done():
            start_task.cancel()
            await asyncio.gather(start_task, return_exceptions=True)
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
            self._refresh_task = None
        if self.registered:
            try:
                await self.register(expires=0, timeout=1.5)
            except Exception:
                _LOGGER.debug("Ignoring SIP trunk unregister failure", exc_info=True)
        if self._receive_task is not None:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
            self._receive_task = None
        await self._cancel_request_tasks()
        await self._close_inbound_transactions("local_hangup")
        self.request_handler = None
        self.inbound_endpoint = None
        if self.transport is not None:
            self.transport.close()
            self.transport = None
        if self._tcp_writer is not None:
            await self._tcp_writer.close()
            self._tcp_writer = None
        if self.writer is not None:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:
                pass
            self.writer = None
            self.reader = None
        self._reader_ready.clear()

    def _ensure_refresh_task(self) -> None:
        if not self._stopped and (
            self._refresh_task is None or self._refresh_task.done()
        ):
            self._refresh_task = asyncio.create_task(
                self._refresh_loop(),
                name=f"voip-sip-trunk-refresh-{self.call_id}",
            )

    async def _refresh_loop(self) -> None:
        retry_delay = 30.0
        while not self._stopped:
            if self.registered and self.expires_at > 0:
                delay = _registration_refresh_delay(
                    self.config.expires,
                    self.expires_at,
                    time.time(),
                )
            else:
                delay = retry_delay
            try:
                await asyncio.wait_for(
                    self._refresh_wakeup.wait(),
                    timeout=delay,
                )
            except asyncio.TimeoutError:
                pass
            self._refresh_wakeup.clear()
            if self._stopped:
                return
            try:
                if self.transport_name == "TCP" and (self.writer is None or self.writer.is_closing()):
                    await self._connect_tcp()
                elif self.transport_name != "TCP":
                    await self._connect_udp()
                self._ensure_receive_task()
                result = await self.register(timeout=2.0)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self.registered = False
                self.status_code = 0
                self.status_reason = str(err)
                _LOGGER.warning(
                    "SIP trunk refresh failed server=%s transport=%s error=%s; retrying in %.0fs",
                    self.config.server,
                    self.transport_name,
                    err,
                    retry_delay,
                )
                continue
            if result == "registered":
                retry_delay = 30.0
            else:
                retry_delay = min(300.0, retry_delay * 2.0)

    async def _connect_tcp(self) -> None:
        async with self._tcp_connect_lock:
            if self.writer is not None and not self.writer.is_closing():
                return
            self._reader_ready.clear()
            if self._tcp_writer is not None:
                await self._tcp_writer.close()
                self._tcp_writer = None
            if self.writer is not None:
                self.writer.close()
                with contextlib.suppress(Exception):
                    await self.writer.wait_closed()
                self.writer = None
                self.reader = None
            while not self.responses.empty():
                with contextlib.suppress(asyncio.QueueEmpty):
                    self.responses.get_nowait()
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.registrar_host, self.registrar_port),
                timeout=2.0,
            )
            if self._stopped:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
                raise RuntimeError("SIP trunk stopped while connecting")
            self.reader = reader
            self.writer = writer
            self._tcp_writer = SipTcpWriter(self.writer, label=f"trunk {self.registrar_host}:{self.registrar_port}")
            self._reader_ready.set()

    async def _connect_udp(self) -> None:
        """Refresh UDP proxy trust and create the local socket when needed."""

        await self._refresh_udp_trusted_hosts()
        if self.transport is not None:
            return
        loop = asyncio.get_running_loop()
        self.protocol = SipDatagramQueueProtocol(self.queue)
        transport, _ = await loop.create_datagram_endpoint(
            lambda: self.protocol,
            local_addr=("0.0.0.0", 0),
            family=socket.AF_INET,
        )
        if self._stopped:
            transport.close()
            raise RuntimeError("SIP trunk stopped while opening UDP transport")
        self.transport = transport  # type: ignore[assignment]

    async def _refresh_udp_trusted_hosts(self) -> None:
        """Resolve the configured UDP proxy to a fail-closed source allowlist."""

        if self.transport_name == "TCP":
            return
        loop = asyncio.get_running_loop()
        host, port = self.registrar_target
        try:
            addresses = await loop.getaddrinfo(
                host,
                port,
                family=socket.AF_INET,
                type=socket.SOCK_DGRAM,
            )
        except OSError:
            if self._trusted_udp_hosts:
                _LOGGER.warning(
                    "SIP trunk UDP proxy DNS refresh failed for %s; retaining prior source allowlist",
                    host,
                )
                return
            raise
        resolved = frozenset(str(item[4][0]) for item in addresses if item[4])
        if not resolved:
            raise OSError(f"SIP trunk UDP proxy {host!r} has no IPv4 address")
        self._trusted_udp_hosts = resolved

    def _udp_source_is_trusted(self, addr: tuple[str, int]) -> bool:
        return bool(self._trusted_udp_hosts) and str(addr[0]) in self._trusted_udp_hosts

    async def _send_raw(self, raw: bytes) -> None:
        if self.transport_name == "TCP":
            await self._connect_tcp()
            if self._tcp_writer is None:
                raise ConnectionError("SIP trunk TCP writer is not available")
            if not await self._tcp_writer.send(raw):
                raise ConnectionError("SIP trunk TCP connection is not writable")
            return
        if self.transport is None:
            raise ConnectionError("SIP trunk UDP transport is not available")
        self.transport.sendto(raw, self.registrar_target)

    async def _read_response(
        self,
        timeout: float,
        *,
        expected_cseq: int,
        expected_branch: str = "",
    ) -> sip.SipMessage | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, float(timeout))
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            message = await asyncio.wait_for(self.responses.get(), timeout=remaining)
            try:
                cseq = sip.parse_cseq(message.header("CSeq"))
                vias = message.header_values("Via")
                branch = sip.parse_via(vias[0] if vias else "").branch
                matches = (
                    message.header("Call-ID") == self.call_id
                    and cseq.number == expected_cseq
                    and cseq.method == "REGISTER"
                    and (not expected_branch or branch == expected_branch)
                    and message.status_code is not None
                    and message.status_code >= 200
                )
            except (TypeError, ValueError, sip.SipError):
                matches = False
            if matches:
                return message
            _LOGGER.debug("Ignoring stale/non-REGISTER SIP trunk response")

    def set_request_handler(self, handler: TrunkRequestHandler | None) -> None:
        self.request_handler = handler

    def attach_endpoint_manager(self, manager: Any) -> None:
        """Route inbound trunk SIP requests through the HA SIP endpoint policy."""
        from .sip_listener import SipUdpEndpoint

        enable_video = bool(getattr(manager, "enable_video", False))
        media_update_handler = getattr(manager, "on_media_update", None)
        if enable_video and not callable(media_update_handler):
            raise ValueError(
                "video-enabled trunk endpoints require an explicit media-update handler"
            )
        _LOGGER.info(
            "SIP trunk inbound media policy video=%s transcode=%s browser_send=%s",
            enable_video,
            bool(getattr(manager, "enable_video_transcoding", False)),
            bool(getattr(manager, "prefer_browser_video_send", False)),
        )

        endpoint = SipUdpEndpoint(
            local_ip=manager.local_ip,
            local_sip_port=manager.port,
            local_rtp_port=manager.local_rtp_port,
            supported_formats=manager.supported_formats,
            supported_send_formats=manager.supported_send_formats,
            supported_recv_formats=manager.supported_recv_formats,
            on_invite=manager.on_invite,
            on_terminated=manager.on_terminated,
            on_register=getattr(manager, "on_register", None),
            on_info=getattr(manager, "on_info", None),
            # Inbound requests received on the persistent trunk connection
            # use this endpoint rather than the UDP/TCP listening servers.
            # Keep its in-dialog media policy identical or an audio call can
            # be established but a later audio->video re-INVITE is rejected
            # with 488 before the endpoint runtime can stage the new media.
            on_media_update=media_update_handler,
            send_override=self.send_response,
            signaling_transport=self.transport_name,
            enable_video=enable_video,
            enable_video_transcoding=bool(
                getattr(manager, "enable_video_transcoding", False)
            ),
            prefer_browser_video_send=bool(
                getattr(manager, "prefer_browser_video_send", False)
            ),
            trusted_trunk=True,
        )
        self.inbound_endpoint = endpoint
        self.set_request_handler(endpoint._handle_datagram)

    def send_response(self, raw: bytes, addr: tuple[str, int]) -> bool:
        try:
            if self.transport_name == "TCP":
                if self._tcp_writer is not None:
                    return self._tcp_writer.send_nowait(raw)
                return False
            if self.transport is not None:
                self.transport.sendto(raw, addr)
                return True
        except (ConnectionError, OSError, RuntimeError) as err:
            _LOGGER.debug("SIP trunk response send failed for %s:%s: %s", addr[0], addr[1], err)
        return False

    async def _close_inbound_transactions(self, reason: str) -> None:
        endpoint = self.inbound_endpoint
        if endpoint is None:
            return
        call_ids = set(endpoint.pending_invites) | set(endpoint.active_dialogs)
        endpoint.pending_invites.clear()
        endpoint.completed_invites.clear()
        endpoint.active_dialogs.clear()
        endpoint.completed_byes.clear()
        if endpoint.on_terminated is not None:
            for call_id in call_ids:
                with contextlib.suppress(Exception):
                    await endpoint.on_terminated(call_id, reason)

    async def _close_early_inbound_transactions(self, reason: str) -> None:
        """End only unconfirmed inbound calls after a trunk flow loss.

        Confirmed dialogs are identified by Call-ID and tags. They do not
        belong to the TCP connection that happened to carry their initial
        INVITE and must remain available on a replacement flow.
        """

        endpoint = self.inbound_endpoint
        if endpoint is None:
            return
        call_ids = set(endpoint.pending_invites)
        endpoint.pending_invites.clear()
        if endpoint.on_terminated is not None:
            for call_id in call_ids:
                with contextlib.suppress(Exception):
                    await endpoint.on_terminated(call_id, reason)

    async def _detach_tcp_flow(
        self,
        reader: asyncio.StreamReader,
        *,
        reason: str,
    ) -> None:
        """Detach one failed TCP flow without destroying confirmed dialogs."""

        if reader is not self.reader:
            return
        self.registered = False
        self.status_code = 0
        self.status_reason = reason
        self._reader_ready.clear()
        writer = self.writer
        tx = self._tcp_writer
        self.reader = None
        self.writer = None
        self._tcp_writer = None
        if tx is not None:
            await tx.close()
        if writer is not None and not writer.is_closing():
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        await self._cancel_request_tasks()
        await self._close_early_inbound_transactions("transport_closed")
        if not self._stopped:
            self._refresh_wakeup.set()

    async def _cancel_request_tasks(self) -> None:
        tasks = tuple(self._request_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _remote_addr(self) -> tuple[str, int]:
        if self.writer is not None:
            peer = self.writer.get_extra_info("peername")
            if peer:
                return (str(peer[0]), int(peer[1]))
        return self.registrar_target

    async def _handle_request(self, raw: bytes, addr: tuple[str, int], method: str) -> None:
        try:
            handler = self.request_handler
            if handler is None:
                return
            await handler(raw, addr)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            _LOGGER.exception(
                "SIP trunk inbound request failed method=%s from=%s:%s error=%s",
                method,
                addr[0],
                addr[1],
                err,
            )

    def _submit_request(self, raw: bytes, addr: tuple[str, int], method: str) -> bool:
        is_invite = method == "INVITE"
        is_control = method in {"ACK", "BYE", "CANCEL"}
        if (
            len(self._request_tasks) >= _MAX_TRUNK_REQUEST_TASKS
            or (not is_control and len(self._request_tasks) >= _MAX_TRUNK_INVITE_TASKS)
            or (is_invite and len(self._invite_tasks) >= _MAX_TRUNK_INVITE_TASKS)
        ):
            _LOGGER.warning("SIP trunk inbound handler saturated; dropping %s", method)
            return False
        task = asyncio.create_task(self._handle_request(raw, addr, method))
        self._request_tasks.add(task)
        if is_invite:
            self._invite_tasks.add(task)
        task.add_done_callback(self._request_task_done)
        return True

    def _request_task_done(self, task: asyncio.Task[None]) -> None:
        self._request_tasks.discard(task)
        self._invite_tasks.discard(task)

    async def _receive_loop(self) -> None:
        try:
            while True:
                if self.transport_name == "TCP":
                    if self.reader is None:
                        await self._reader_ready.wait()
                        continue
                    active_reader = self.reader
                    try:
                        raw = await _read_sip_stream_message(active_reader)
                    except asyncio.CancelledError:
                        raise
                    except Exception as err:
                        if active_reader is not self.reader:
                            continue
                        _LOGGER.warning(
                            "SIP trunk TCP flow lost server=%s error=%s",
                            self.config.server,
                            err,
                        )
                        await self._detach_tcp_flow(
                            active_reader,
                            reason=str(err),
                        )
                        continue
                    if raw is None:
                        if active_reader is not self.reader:
                            continue
                        _LOGGER.warning(
                            "SIP trunk TCP flow closed server=%s; preserving confirmed dialogs",
                            self.config.server,
                        )
                        await self._detach_tcp_flow(
                            active_reader,
                            reason="SIP trunk TCP connection closed",
                        )
                        continue
                    addr = self._remote_addr()
                else:
                    raw, addr = await self.queue.get()
                    if not self._udp_source_is_trusted(addr):
                        _LOGGER.warning(
                            "SIP trunk dropped UDP packet from untrusted source %s:%s",
                            addr[0],
                            addr[1],
                        )
                        continue
                try:
                    msg = sip.parse_message(raw)
                except Exception as err:
                    _LOGGER.info("SIP trunk RX malformed from %s:%s: %s", addr[0], addr[1], err)
                    continue
                if msg.is_response:
                    cseq = msg.header("CSeq").split()
                    if msg.header("Call-ID") != self.call_id or len(cseq) != 2 or cseq[1].upper() != "REGISTER":
                        _LOGGER.debug("SIP trunk ignored non-registration response")
                        continue
                    if put_drop_oldest(self.responses, msg):
                        _LOGGER.debug("SIP trunk response queue full; dropped oldest response")
                    continue
                _LOGGER.info("SIP trunk RX %s %s from %s:%s", msg.method, msg.uri, addr[0], addr[1])
                self.last_sip_event = msg.method or "SIP_REQUEST"
                if self.request_handler is None:
                    _LOGGER.warning("SIP trunk inbound request ignored: no SIP endpoint is attached")
                    continue
                self._submit_request(raw, addr, msg.method or "SIP_REQUEST")
        except asyncio.CancelledError:
            raise
        except Exception as err:
            self.registered = False
            self.status_code = 0
            self.status_reason = str(err)
            _LOGGER.warning("SIP trunk receive loop stopped server=%s transport=%s error=%s", self.config.server, self.transport_name, err)

    async def register(self, *, expires: int | None = None, timeout: float = 2.0) -> str:
        expires_value = int(self.config.expires if expires is None else expires)
        auth_value = ""
        retried = False
        while True:
            self.cseq += 1
            request_uri = self.address_uri
            headers = self._register_headers(expires_value, auth_value=auth_value)
            via_values = [value for key, value in headers if key.lower() == "via"]
            expected_branch = sip.parse_via(via_values[0] if via_values else "").branch
            raw = sip.build_request("REGISTER", request_uri, headers, b"")
            await self._send_raw(raw)
            self.last_sip_event = "REGISTER"
            _LOGGER.info("SIP trunk TX REGISTER %s expires=%s", self.domain, expires_value)
            transaction = SipClientTransaction[
                sip.SipMessage
            ](
                transport=self.transport_name,
                timeout=max(0.0, float(timeout)),
                t1=SIP_T1,
                t2=SIP_T2,
            )
            try:
                msg = await transaction.receive(
                    lambda read_timeout: self._read_response(
                        read_timeout,
                        expected_cseq=self.cseq,
                        expected_branch=expected_branch,
                    ),
                    lambda: self._send_raw(raw),
                )
                if msg is None:
                    raise asyncio.TimeoutError
            except asyncio.TimeoutError:
                self.registered = False
                self.status_code = 0
                self.status_reason = "timeout"
                _LOGGER.warning(
                    "SIP trunk registration timed out server=%s transport=%s expires=%s",
                    self.config.server,
                    self.transport_name,
                    expires_value,
                )
                return "timeout"
            except Exception as err:
                self.registered = False
                self.status_code = 0
                self.status_reason = str(err)
                _LOGGER.warning(
                    "SIP trunk registration transport error server=%s transport=%s error=%s",
                    self.config.server,
                    self.transport_name,
                    err,
                )
                return "transport_unreachable"
            if msg is None or not msg.is_response or msg.status_code is None:
                continue
            self.status_code = int(msg.status_code)
            self.status_reason = msg.reason
            self.last_sip_event = "SIP_RESPONSE"
            _LOGGER.info("SIP trunk RX %s %s", msg.status_code, msg.reason)
            if msg.status_code in {401, 407} and not retried:
                retried = True
                challenge = msg.header("Proxy-Authenticate" if msg.status_code == 407 else "WWW-Authenticate")
                auth_value = build_digest_authorization(
                    challenge_header=challenge,
                    username=self.config.username,
                    auth_username=self.config.auth_username,
                    password=self.config.password,
                    method="REGISTER",
                    uri=request_uri,
                )
                if msg.status_code == 407:
                    auth_value = "Proxy-Authorization: " + auth_value
                else:
                    auth_value = "Authorization: " + auth_value
                continue
            if 200 <= msg.status_code < 300:
                granted_expires = _registration_expires(msg, expires_value)
                self.registered = expires_value > 0 and granted_expires > 0
                self.expires_at = time.time() + granted_expires if self.registered else 0.0
                if self.registered:
                    _LOGGER.info(
                        "SIP trunk registered server=%s transport=%s expires=%ss status=%s %s",
                        self.config.server,
                        self.transport_name,
                        granted_expires,
                        msg.status_code,
                        msg.reason,
                    )
                else:
                    _LOGGER.info(
                        "SIP trunk registration ended server=%s transport=%s status=%s %s",
                        self.config.server,
                        self.transport_name,
                        msg.status_code,
                        msg.reason,
                    )
                return "registered" if self.registered else "unregistered"
            self.registered = False
            result = sip.sip_failure_reason(msg.status_code)
            if expires_value <= 0:
                _LOGGER.info(
                    "SIP trunk unregister rejected server=%s transport=%s status=%s %s reason=%s; continuing shutdown/reconfigure",
                    self.config.server,
                    self.transport_name,
                    msg.status_code,
                    msg.reason,
                    result,
                )
                return result
            _LOGGER.warning(
                "SIP trunk registration rejected server=%s transport=%s status=%s %s reason=%s",
                self.config.server,
                self.transport_name,
                msg.status_code,
                msg.reason,
                result,
            )
            return result

    def _register_headers(self, expires: int, *, auth_value: str = "") -> list[tuple[str, str]]:
        local_uri = self.address_uri
        dialog = sip.SipDialogIds(
            call_id=self.call_id,
            local_tag=self.local_tag,
            cseq=self.cseq,
            branch=sip.make_branch(),
        )
        headers = sip.dialog_headers(
            request_uri=self.address_uri,
            local_uri=local_uri,
            remote_uri=local_uri,
            dialog=dialog,
            method="REGISTER",
            contact_uri=self.contact_uri,
            transport=self.transport_name,
        )
        headers.append(("Expires", str(int(expires))))
        if auth_value:
            key, value = auth_value.split(":", 1)
            headers.append((key.strip(), value.strip()))
        return headers

    def snapshot(self) -> dict[str, Any]:
        return {
            "trunk_enabled": bool(self.config.enabled),
            "trunk_registered": self.registered,
            "trunk_status_code": self.status_code,
            "trunk_status_reason": self.status_reason,
            "trunk_expires_at": self.expires_at,
            "trunk_last_sip_event": self.last_sip_event,
            "trunk_transport": self.transport_name.lower(),
            "trunk_server": self.config.server,
        }
