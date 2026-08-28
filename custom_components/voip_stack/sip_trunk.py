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
from dataclasses import dataclass, replace
import ipaddress
import logging
import socket
import ssl
import time
import uuid
from typing import Any, Awaitable, Callable

from .core import sip
from .core.sip_transport import default_tls_context
from .core.sip_auth import DigestChallengeTracker
from .core.sip_resolution import SipServerResolver, SipServerTarget
from .core.sip_transaction import SIP_T1, SIP_T2, SIP_TIMER_F, SipClientTransaction, transaction_key
from .sip_udp_io import SipDatagramQueueProtocol
from .sip_tcp_io import SipTcpWriter, read_sip_stream_message as _read_sip_stream_message
from .queue_utils import put_drop_oldest
from .session_cleanup import async_wait_for_cleanup


_LOGGER = logging.getLogger(__name__)
_MAX_TRUNK_REQUEST_TASKS = 32
_MAX_TRUNK_INVITE_TASKS = 24
_REGISTER_RETRY_INITIAL = 5.0
_REGISTER_RETRY_MAX = 60.0

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
    def __init__(
        self,
        *,
        config: SipTrunkConfig,
        local_ip: str,
        local_sip_port: int,
        target_resolver: SipServerResolver | None = None,
        tls_context: ssl.SSLContext | None = None,
    ) -> None:
        self.config = config
        self.local_ip = local_ip
        self.local_sip_port = int(local_sip_port)
        self.transport_name = (config.transport or "udp").upper()
        self.queue: asyncio.Queue[tuple[bytes, tuple[str, int]]] = asyncio.Queue(maxsize=128)
        self.responses: asyncio.Queue[sip.SipMessage] = asyncio.Queue(maxsize=32)
        self.protocol: SipDatagramQueueProtocol | None = None
        self.transport: asyncio.DatagramTransport | None = None
        self._udp_local_port: int | None = None
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
        self._endpoint_manager: Any | None = None
        self._trusted_udp_hosts: frozenset[str] = frozenset()
        self.target_resolver = target_resolver or SipServerResolver()
        self.tls_context = tls_context
        self._registrar_candidate: SipServerTarget | None = None
        self._registrar_candidates: tuple[SipServerTarget, ...] = ()
        self._registrar_candidate_index = -1
        self._instance_id = f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'{local_ip}:{self.domain}:{config.username}')}"
        self._flow_timer = 0.0
        self._keepalive_task: asyncio.Task[None] | None = None

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
            uri = sip.parse_sip_uri(
                proxy
                if proxy.lower().startswith(("sip:", "sips:"))
                else f"sip:{proxy}"
            )
            return uri.host, int(uri.port or self.config.port)
        except (TypeError, ValueError, sip.SipError):
            return proxy, int(self.config.port)

    @property
    def active_registrar_target(self) -> tuple[str, int]:
        candidate = self._registrar_candidate
        if candidate is None or not candidate.addresses:
            return self.registrar_target
        return candidate.addresses[0], candidate.port

    async def _resolve_registrar(self) -> SipServerTarget:
        host, port = self.registrar_target
        uri = sip.SipUri("", host, port)
        resolved = await self.target_resolver.resolve(
            uri,
            transport=self.transport_name,
        )
        candidates = tuple(
            replace(candidate, addresses=(address,))
            for candidate in resolved
            for address in candidate.addresses
        )
        if not candidates:
            raise OSError(f"SIP registrar {host!r} has no reachable address")
        self._registrar_candidates = candidates
        self._registrar_candidate_index = 0
        self._registrar_candidate = candidates[0]
        return self._registrar_candidate

    async def _next_registrar_candidate(self) -> SipServerTarget | None:
        """Select the next pre-registration server without cycling silently."""
        if not self._registrar_candidates:
            return await self._resolve_registrar()
        next_index = self._registrar_candidate_index + 1
        if next_index >= len(self._registrar_candidates):
            return None
        self._registrar_candidate_index = next_index
        self._registrar_candidate = self._registrar_candidates[next_index]
        return self._registrar_candidate

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

    @property
    def registration_uri(self) -> str:
        """Return the registrar location service URI required by RFC 3261."""

        return str(sip.SipUri("", self.domain))

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
            if self.transport_name in {"TCP", "TLS"}:
                await self._connect_tcp()
            else:
                await self._connect_udp()
            if self._stopped:
                return
            self._ensure_receive_task()
        except Exception as err:
            if self._stopped:
                return
            self.registered = False
            self.status_code = 0
            self.status_reason = str(err)
            _LOGGER.warning(
                "SIP trunk transport startup failed server=%s transport=%s error=%s; background retry will continue",
                self.config.server,
                self.transport_name,
                err,
            )
        if not self._stopped:
            self._ensure_refresh_task()
            # Registration follows the RFC non-INVITE transaction deadline,
            # but Home Assistant setup must not wait up to Timer F for an
            # unreachable PBX. Wake the owned background lifecycle instead.
            self._refresh_wakeup.set()

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
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            await asyncio.gather(self._keepalive_task, return_exceptions=True)
            self._keepalive_task = None
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
        if (
            self._endpoint_manager is not None
            and self.inbound_endpoint is not None
        ):
            detach = getattr(
                self._endpoint_manager, "detach_dialog_endpoint", None
            )
            if callable(detach):
                detach(self.inbound_endpoint)
        self._endpoint_manager = None
        self.request_handler = None
        self.inbound_endpoint = None
        if self.transport is not None:
            self._close_udp_transport()
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

    def _ensure_keepalive_task(self) -> None:
        if (
            not self._stopped
            and self._flow_timer > 0
            and self.transport_name in {"TCP", "TLS"}
            and (self._keepalive_task is None or self._keepalive_task.done())
        ):
            self._keepalive_task = asyncio.create_task(
                self._keepalive_loop(),
                name=f"voip-sip-trunk-keepalive-{self.call_id}",
            )

    async def _keepalive_loop(self) -> None:
        while not self._stopped and self._flow_timer > 0:
            await asyncio.sleep(max(2.5, self._flow_timer / 2.0))
            tx = self._tcp_writer
            if tx is not None and not await tx.send(b"\r\n\r\n"):
                return

    async def _refresh_loop(self) -> None:
        retry_delay = _REGISTER_RETRY_INITIAL
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
                if self.transport_name in {"TCP", "TLS"} and (self.writer is None or self.writer.is_closing()):
                    await self._connect_tcp()
                elif self.transport_name not in {"TCP", "TLS"}:
                    await self._connect_udp()
                self._ensure_receive_task()
                result = await self.register()
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
                retry_delay = _REGISTER_RETRY_INITIAL
            else:
                retry_delay = min(_REGISTER_RETRY_MAX, retry_delay * 2.0)

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
            candidate = self._registrar_candidate or await self._resolve_registrar()
            while True:
                host, port = candidate.addresses[0], candidate.port
                try:
                    tls = self.transport_name == "TLS"
                    tls_context = None
                    if tls:
                        if self.tls_context is None:
                            self.tls_context = await default_tls_context()
                        tls_context = self.tls_context
                    reader, writer = await asyncio.wait_for(
                        (
                            asyncio.open_connection(
                                host,
                                port,
                                ssl=tls_context,
                                server_hostname=self.registrar_host,
                            )
                            if tls
                            else asyncio.open_connection(host, port)
                        ),
                        timeout=2.0,
                    )
                    break
                except (ConnectionError, OSError, TimeoutError):
                    candidate = await self._next_registrar_candidate()
                    if candidate is None:
                        raise
            if self._stopped:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
                raise RuntimeError("SIP trunk stopped while connecting")
            self.reader = reader
            self.writer = writer
            self._tcp_writer = SipTcpWriter(self.writer, label=f"trunk {host}:{port}")
            self._reader_ready.set()

    async def _connect_udp(self, *, refresh: bool = True) -> None:
        """Refresh UDP proxy trust and create the local socket when needed."""

        if refresh:
            await self._refresh_udp_trusted_hosts()
        if self.transport is not None:
            return
        loop = asyncio.get_running_loop()
        self.protocol = SipDatagramQueueProtocol(self.queue)
        candidate = self._registrar_candidate
        assert candidate is not None
        family = socket.AF_INET6 if ":" in candidate.addresses[0] else socket.AF_INET
        transport, _ = await loop.create_datagram_endpoint(
            lambda: self.protocol,
            local_addr=("::" if family == socket.AF_INET6 else "0.0.0.0", 0),
            family=family,
        )
        if self._stopped:
            transport.close()
            raise RuntimeError("SIP trunk stopped while opening UDP transport")
        self.transport = transport  # type: ignore[assignment]
        sockname = transport.get_extra_info("sockname")
        if not isinstance(sockname, tuple) or len(sockname) < 2 or int(sockname[1]) <= 0:
            self._close_udp_transport()
            raise ConnectionError("SIP trunk UDP socket has no usable local port")
        self._udp_local_port = int(sockname[1])
        _LOGGER.info(
            "SIP trunk UDP socket bound %s:%s remote=%s:%s",
            self.local_ip,
            self._udp_local_port,
            candidate.addresses[0],
            candidate.port,
        )

    def _close_udp_transport(self) -> None:
        transport = self.transport
        self.transport = None
        self.protocol = None
        self._udp_local_port = None
        if transport is not None:
            transport.close()

    def _reset_udp_transport_after_timeout(self) -> None:
        """Retire one failed UDP flow before the refresh owner retries."""

        self._close_udp_transport()
        while not self.responses.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self.responses.get_nowait()

    async def _failover_registrar(self) -> bool:
        """Move one unconfirmed REGISTER transaction to its next server."""
        candidate = await self._next_registrar_candidate()
        if candidate is None:
            return False
        address = candidate.addresses[0]
        while not self.responses.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self.responses.get_nowait()
        if self.transport_name in {"TCP", "TLS"}:
            reader = self.reader
            if reader is not None:
                await self._detach_tcp_flow(reader, reason="registrar_failover")
            await self._connect_tcp()
            return True
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        current_family = None
        if self.transport is not None:
            sock = self.transport.get_extra_info("socket")
            current_family = getattr(sock, "family", None)
        if self.transport is not None and current_family not in {None, family}:
            self._close_udp_transport()
        if self.transport is None:
            await self._connect_udp(refresh=False)
        return True

    async def _refresh_udp_trusted_hosts(self) -> None:
        """Resolve the configured UDP proxy to a fail-closed source allowlist."""

        if self.transport_name in {"TCP", "TLS"}:
            return
        try:
            await self._resolve_registrar()
            resolved = {
                normalized
                for item in self._registrar_candidates
                for address in item.addresses
                if (normalized := self._normalize_ip(address))
            }
            server_target = (str(self.config.server), int(self.config.port))
            if server_target != self.registrar_target:
                try:
                    server_candidates = await self.target_resolver.resolve(
                        sip.SipUri("", server_target[0], server_target[1]),
                        transport="UDP",
                    )
                except OSError:
                    # The server may be a logical SIP domain with no address
                    # records.  When an outbound proxy is configured, only
                    # that next hop must resolve; a logical domain must not
                    # invalidate the proxy's source allowlist.
                    server_candidates = ()
                resolved.update(
                    normalized
                    for item in server_candidates
                    for address in item.addresses
                    if (normalized := self._normalize_ip(address))
                )
            resolved = frozenset(resolved)
        except OSError:
            if self._trusted_udp_hosts:
                _LOGGER.warning(
                    "SIP trunk UDP proxy DNS refresh failed for %s; retaining prior source allowlist",
                    self.registrar_host,
                )
                return
            raise
        if not resolved:
            raise OSError(f"SIP trunk UDP proxy {self.registrar_host!r} has no address")
        self._trusted_udp_hosts = resolved

    @staticmethod
    def _normalize_ip(value: str) -> str:
        candidate = str(value or "").strip().strip("[]").split("%", 1)[0]
        try:
            return ipaddress.ip_address(candidate).compressed
        except ValueError:
            return ""

    def _udp_source_is_trusted(self, addr: tuple[str, int]) -> bool:
        source = self._normalize_ip(addr[0])
        return bool(source and self._trusted_udp_hosts) and source in self._trusted_udp_hosts

    def accepts_inbound_source(
        self,
        source_host: str,
        source_port: int,
        signaling_transport: str,
    ) -> bool:
        """Identify an inbound request using the configured UDP peer ACL."""

        del source_port
        return bool(
            self.registered
            and self.transport_name == "UDP"
            and str(signaling_transport or "").upper() == "UDP"
            and self._udp_source_is_trusted((source_host, 0))
        )

    async def _send_raw(self, raw: bytes) -> None:
        if self.transport_name in {"TCP", "TLS"}:
            await self._connect_tcp()
            if self._tcp_writer is None:
                raise ConnectionError("SIP trunk TCP writer is not available")
            if not await self._tcp_writer.send(raw):
                raise ConnectionError("SIP trunk TCP connection is not writable")
            return
        if self.transport is None:
            raise ConnectionError("SIP trunk UDP transport is not available")
        self.transport.sendto(raw, self.active_registrar_target)

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
            key = transaction_key(message)
            matches = bool(
                key is not None
                and message.header("Call-ID") == self.call_id
                and key[:2] == ("REGISTER", expected_cseq)
                and (not expected_branch or key[2] == expected_branch)
                and (message.status_code or 0) >= 200
            )
            if matches:
                return message
            cseq = message.header("CSeq").split()
            _LOGGER.debug(
                "Ignoring stale/non-REGISTER SIP trunk response "
                "status=%s cseq=%s method=%s call_id_match=%s branch_match=%s",
                message.status_code or 0,
                cseq[0] if cseq else "",
                cseq[1].upper() if len(cseq) == 2 else "",
                message.header("Call-ID") == self.call_id,
                bool(
                    key is not None
                    and (not expected_branch or key[2] == expected_branch)
                ),
            )

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

        if (
            self._endpoint_manager is not None
            and self.inbound_endpoint is not None
        ):
            detach = getattr(
                self._endpoint_manager, "detach_dialog_endpoint", None
            )
            if callable(detach):
                detach(self.inbound_endpoint)
        endpoint = SipUdpEndpoint(
            local_ip=manager.local_ip,
            local_sip_port=manager.port,
            local_rtp_port=manager.local_rtp_port,
            supported_formats=manager.supported_formats,
            supported_send_formats=manager.supported_send_formats,
            supported_recv_formats=manager.supported_recv_formats,
            on_invite=manager.on_invite,
            on_offerless_invite=getattr(manager, "on_offerless_invite", None),
            on_terminated=manager.on_terminated,
            on_register=getattr(manager, "on_register", None),
            on_info=getattr(manager, "on_info", None),
            on_refer=getattr(manager, "on_refer", None),
            on_request=getattr(manager, "on_request", None),
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
        attach = getattr(manager, "attach_dialog_endpoint", None)
        if callable(attach):
            attach(endpoint)
        self._endpoint_manager = manager
        self.inbound_endpoint = endpoint
        self.set_request_handler(endpoint._handle_datagram)

    def send_response(self, raw: bytes, addr: tuple[str, int]) -> bool:
        try:
            if self.transport_name in {"TCP", "TLS"}:
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
        return self.active_registrar_target

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
                if self.transport_name in {"TCP", "TLS"}:
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
                    if raw == b"\r\n":
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
                    is_registration = (
                        msg.header("Call-ID") == self.call_id
                        and len(cseq) == 2
                        and cseq[1].upper() == "REGISTER"
                    )
                    if not is_registration:
                        handler = self.request_handler
                        if handler is not None:
                            await handler(raw, addr)
                        else:
                            _LOGGER.debug("SIP trunk ignored response without an endpoint owner")
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

    async def register(
        self,
        *,
        expires: int | None = None,
        timeout: float = SIP_TIMER_F,
    ) -> str:
        expires_value = int(self.config.expires if expires is None else expires)
        auth_values: dict[str, str] = {}
        auth_challenges = DigestChallengeTracker()
        while True:
            self.cseq += 1
            request_uri = self.registration_uri
            headers = self._register_headers(expires_value, auth_values=auth_values)
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
            register_cseq = self.cseq
            register_branch = expected_branch
            register_raw = raw

            async def _read_register_response(
                read_timeout: float,
                cseq: int = register_cseq,
                branch: str = register_branch,
            ) -> sip.SipMessage | None:
                return await self._read_response(
                    read_timeout,
                    expected_cseq=cseq,
                    expected_branch=branch,
                )

            async def _retransmit_register(payload: bytes = register_raw) -> None:
                await self._send_raw(payload)

            try:
                msg = await transaction.receive(
                    _read_register_response,
                    _retransmit_register,
                )
                if msg is None:
                    raise asyncio.TimeoutError
            except asyncio.TimeoutError:
                if await self._failover_registrar():
                    _LOGGER.info(
                        "SIP trunk REGISTER timed out, trying next RFC 3263 target"
                    )
                    continue
                self.registered = False
                self.status_code = 0
                self.status_reason = "timeout"
                _LOGGER.warning(
                    "SIP trunk registration timed out server=%s transport=%s expires=%s",
                    self.config.server,
                    self.transport_name,
                    expires_value,
                )
                if self.transport_name == "UDP":
                    self._reset_udp_transport_after_timeout()
                return "timeout"
            except Exception as err:
                try:
                    failed_over = await self._failover_registrar()
                except Exception:
                    failed_over = False
                if failed_over:
                    _LOGGER.info(
                        "SIP trunk REGISTER transport failed, trying next RFC 3263 target"
                    )
                    continue
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
            if msg.status_code in {401, 407}:
                try:
                    auth_header, _challenge, auth_value = auth_challenges.authorize(
                        msg,
                        username=self.config.username,
                        auth_username=self.config.auth_username,
                        password=self.config.password,
                        method="REGISTER",
                        uri=request_uri,
                    )
                except ValueError as err:
                    self.registered = False
                    self.status_reason = str(err)
                    _LOGGER.warning("SIP trunk digest challenge rejected: %s", err)
                    return sip.sip_failure_reason(msg.status_code)
                auth_values[auth_header] = auth_value
                continue
            if 200 <= msg.status_code < 300:
                granted_expires = _registration_expires(msg, expires_value)
                self.registered = expires_value > 0 and granted_expires > 0
                self.expires_at = time.time() + granted_expires if self.registered else 0.0
                if self.registered:
                    if (
                        "outbound" in sip.option_tags(msg, "Require")
                        and self.transport_name in {"TCP", "TLS"}
                    ):
                        try:
                            self._flow_timer = max(
                                5.0,
                                min(120.0, float(msg.header("Flow-Timer") or 25)),
                            )
                        except ValueError:
                            self._flow_timer = 25.0
                        self._ensure_keepalive_task()
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
            if msg.status_code in {408, 503} and await self._failover_registrar():
                _LOGGER.info(
                    "SIP trunk REGISTER received %s, trying next RFC 3263 target",
                    msg.status_code,
                )
                continue
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

    def _register_headers(
        self,
        expires: int,
        *,
        auth_values: dict[str, str] | None = None,
    ) -> list[tuple[str, str]]:
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
            via_sent_by=(self.local_ip, self._udp_local_port)
            if self.transport_name == "UDP" and self._udp_local_port is not None
            else None,
        )
        headers.append(("Expires", str(int(expires))))
        if self.transport_name in {"TCP", "TLS"}:
            headers = [
                (
                    key,
                    f'{value};+sip.instance="<{self._instance_id}>";reg-id=1'
                    if key.lower() == "contact"
                    else f"{value}, outbound"
                    if key.lower() == "supported" and "outbound" not in value.lower()
                    else value,
                )
                for key, value in headers
            ]
        headers.extend((key, value) for key, value in (auth_values or {}).items())
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
