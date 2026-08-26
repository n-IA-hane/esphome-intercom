"""Unified HA SIP endpoint for UDP and TCP signaling."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import logging
from typing import Any, Callable

from .core import sdp
from .core.audio_format import AudioFormat
from .session_cleanup import async_wait_for_cleanup
from .sip_listener import (
    InfoHandler,
    InviteHandler,
    OfferlessInviteHandler,
    MediaUpdateHandler,
    RequestHandler,
    ReferHandler,
    RegisterHandler,
    SipTcpServer,
    SipInvite,
    SipUdpServer,
    TerminateHandler,
)


_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SipEndpointSnapshot:
    udp_ready: bool
    tcp_ready: bool
    pending_transactions: int
    active_dialogs: int
    pending_call_ids: tuple[str, ...]
    active_call_ids: tuple[str, ...]
    last_sip_event: str
    last_sip_status_code: int
    last_sip_reason: str

    @property
    def pending_invites(self) -> int:
        return self.pending_transactions


@dataclass(slots=True)
class PreparedEndpointVideoReinvite:
    """Locally accepted offer awaiting the bridge-wide commit decision."""

    endpoint: Any
    call_id: str
    previous: SipInvite
    candidate: SipInvite
    committed: bool = False

    def commit(self) -> bool:
        if self.committed:
            return True
        self.committed = self.endpoint.commit_prepared_reinvite(
            self.call_id, self.previous, self.candidate
        )
        return self.committed

    async def restore(
        self,
        *,
        local_video_rtp_port: int,
        video_formats: tuple[sdp.RtpVideoFormat, ...],
    ) -> bool:
        """Compensate an accepted offer with the previous video topology."""

        if not self.commit():
            return False
        restoring_video = self.previous.video_format is not None
        restored = await self.endpoint.async_prepare_video_reinvite(
            self.call_id,
            local_video_rtp_port=(
                int(local_video_rtp_port) if restoring_video else 0
            ),
            video_formats=video_formats,
            video_direction=(
                self.previous.video_format.direction
                if restoring_video
                else "inactive"
            ),
        )
        return bool(
            restored is not None
            and self.endpoint.commit_prepared_reinvite(
                self.call_id,
                self.candidate,
                restored,
            )
        )


class SipEndpointManager:
    """Own the HA SIP signaling endpoint across UDP and TCP.

    The manager is the integration-facing object. The UDP and TCP server
    classes remain transport-specific adapters, but call control code should
    not pick one blindly.
    """

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
        on_terminated: TerminateHandler | None = None,
        on_register: RegisterHandler | None = None,
        on_info: InfoHandler | None = None,
        on_media_update: MediaUpdateHandler | None = None,
        on_refer: ReferHandler | None = None,
        on_request: RequestHandler | None = None,
        on_flow_closed: Callable[[str, int, str], None] | None = None,
        udp_enabled: bool = True,
        tcp_enabled: bool = True,
        enable_video: bool = False,
        enable_video_transcoding: bool = False,
        prefer_browser_video_send: bool = False,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.local_ip = local_ip
        self.local_rtp_port = int(local_rtp_port)
        self.supported_formats = supported_formats
        self.supported_send_formats = supported_send_formats
        self.supported_recv_formats = supported_recv_formats
        self.on_invite = on_invite
        self.on_offerless_invite = on_offerless_invite
        self.on_terminated = on_terminated
        self.on_register = on_register
        self.on_info = on_info
        self.on_media_update = on_media_update
        self.on_refer = on_refer
        self.on_request = on_request
        self.on_flow_closed = on_flow_closed
        self.udp_enabled = bool(udp_enabled)
        self.tcp_enabled = bool(tcp_enabled)
        self.enable_video = bool(enable_video)
        self.enable_video_transcoding = bool(enable_video_transcoding)
        self.prefer_browser_video_send = bool(prefer_browser_video_send)
        if self.enable_video and not callable(self.on_media_update):
            raise ValueError(
                "video-enabled SIP endpoints require an explicit media-update handler"
            )
        self.udp_server: SipUdpServer | None = None
        self.tcp_server: SipTcpServer | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._start_task: asyncio.Task[bool] | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._stopping = False
        self._stopped = False
        self._attached_dialog_endpoints: set[Any] = set()

    async def start(self) -> bool:
        async with self._lifecycle_lock:
            if self._stopping or self._stopped:
                raise RuntimeError("SIP endpoint manager has already been stopped")
            if self._start_task is None:
                self._start_task = asyncio.create_task(
                    self._start(),
                    name=f"voip-sip-endpoint-start-{self.port}",
                )
            task = self._start_task
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await async_wait_for_cleanup(task)
            raise

    async def _start(self) -> bool:
        if not self.udp_enabled and not self.tcp_enabled:
            _LOGGER.error("Cannot start SIP endpoint manager: no signaling transport is enabled")
            return False
        if (
            (not self.udp_enabled or self.udp_server is not None)
            and (not self.tcp_enabled or self.tcp_server is not None)
        ):
            return True

        udp: SipUdpServer | None = None
        tcp: SipTcpServer | None = None
        published = False
        try:
            if self.udp_enabled:
                udp = SipUdpServer(
                    host=self.host,
                    port=self.port,
                    local_ip=self.local_ip,
                    local_rtp_port=self.local_rtp_port,
                    supported_formats=self.supported_formats,
                    supported_send_formats=self.supported_send_formats,
                    supported_recv_formats=self.supported_recv_formats,
                    on_invite=self.on_invite,
                    on_offerless_invite=self.on_offerless_invite,
                    on_terminated=self.on_terminated,
                    on_register=self.on_register,
                    on_info=self.on_info,
                    on_media_update=self.on_media_update,
                    on_refer=self.on_refer,
                    on_request=self.on_request,
                    enable_video=self.enable_video,
                    enable_video_transcoding=self.enable_video_transcoding,
                    prefer_browser_video_send=self.prefer_browser_video_send,
                )
                if not await udp.start():
                    return False

            if self.tcp_enabled:
                tcp = SipTcpServer(
                    host=self.host,
                    port=self.port,
                    local_ip=self.local_ip,
                    local_rtp_port=self.local_rtp_port,
                    supported_formats=self.supported_formats,
                    supported_send_formats=self.supported_send_formats,
                    supported_recv_formats=self.supported_recv_formats,
                    on_invite=self.on_invite,
                    on_offerless_invite=self.on_offerless_invite,
                    on_terminated=self.on_terminated,
                    on_register=self.on_register,
                    on_info=self.on_info,
                    on_media_update=self.on_media_update,
                    on_refer=self.on_refer,
                    on_request=self.on_request,
                    on_flow_closed=self.on_flow_closed,
                    enable_video=self.enable_video,
                    enable_video_transcoding=self.enable_video_transcoding,
                    prefer_browser_video_send=self.prefer_browser_video_send,
                )
                if not await tcp.start():
                    return False

            if self._stopping or self._stopped:
                return False
            self.udp_server = udp
            self.tcp_server = tcp
            published = True
            transports = "+".join(
                name
                for name, enabled in (
                    ("UDP", self.udp_enabled),
                    ("TCP", self.tcp_enabled),
                )
                if enabled
            )
            _LOGGER.info("SIP endpoint manager ready on %s/%s", transports, self.port)
            return True
        finally:
            if not published:
                cleanup = asyncio.gather(
                    *(server.stop() for server in (udp, tcp) if server is not None),
                    return_exceptions=True,
                )
                with contextlib.suppress(asyncio.CancelledError):
                    await async_wait_for_cleanup(cleanup)

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            self._stopping = True
            if self._stop_task is None:
                self._stop_task = asyncio.create_task(
                    self._stop(),
                    name=f"voip-sip-endpoint-stop-{self.port}",
                )
            task = self._stop_task
        await async_wait_for_cleanup(task)

    async def _stop(self) -> None:
        start_task = self._start_task
        if start_task is not None and not start_task.done():
            start_task.cancel()
            await asyncio.gather(start_task, return_exceptions=True)
        udp = self.udp_server
        tcp = self.tcp_server
        self.udp_server = None
        self.tcp_server = None
        try:
            if udp is not None or tcp is not None:
                await asyncio.gather(
                    *(server.stop() for server in (udp, tcp) if server is not None),
                    return_exceptions=True,
                )
        finally:
            self._stopped = True

    @property
    def servers(self) -> list[object]:
        return [server for server in (self.udp_server, self.tcp_server) if server is not None]

    def _pending_call_ids(self, server: object) -> set[str]:
        endpoint = getattr(server, "endpoint", None)
        pending = getattr(endpoint, "pending_invites", None)
        if isinstance(pending, dict):
            return set(pending)
        ids: set[str] = set()
        endpoints = getattr(server, "endpoints", None)
        if isinstance(endpoints, set):
            for endpoint in endpoints:
                pending = getattr(endpoint, "pending_invites", None)
                if isinstance(pending, dict):
                    ids.update(pending)
        return ids

    def server_for_pending_call(self, call_id: str) -> object | None:
        if not call_id:
            return None
        for server in self.servers:
            if call_id in self._pending_call_ids(server):
                return server
        return None

    def pending_call_ids(self) -> set[str]:
        ids: set[str] = set()
        for server in self.servers:
            ids.update(self._pending_call_ids(server))
        return ids

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
    ) -> bool:
        preferred = self.server_for_pending_call(call_id)
        candidates = [preferred] if preferred is not None else self.servers
        for server in candidates:
            send = getattr(server, "send_final_response", None)
            if callable(send) and send(
                call_id,
                status,
                reason,
                answer_sdp=answer_sdp,
                decline_reason=decline_reason,
                connected_identity_name=connected_identity_name,
                connected_identity_user=connected_identity_user,
            ):
                return True
        return False

    def send_bye(self, call_id: str = "") -> bool:
        for server in self.servers:
            send = getattr(server, "send_bye", None)
            if callable(send) and send(call_id):
                return True
        return False

    def _dialog_endpoints(self):
        for server in self.servers:
            endpoint = getattr(server, "endpoint", None)
            if endpoint is not None:
                yield endpoint
            endpoints = getattr(server, "endpoints", None)
            if isinstance(endpoints, set):
                yield from endpoints
        yield from tuple(self._attached_dialog_endpoints)

    def attach_dialog_endpoint(self, endpoint: Any) -> None:
        """Expose a transport-owned endpoint through common dialog control."""

        self._attached_dialog_endpoints.add(endpoint)

    def detach_dialog_endpoint(self, endpoint: Any) -> None:
        """Remove a transport endpoint after its flow is closed."""

        self._attached_dialog_endpoints.discard(endpoint)

    async def async_prepare_video_reinvite(
        self,
        call_id: str,
        **kwargs,
    ) -> PreparedEndpointVideoReinvite | None:
        """Stage a local video offer on the transport owning the dialog."""

        for endpoint in self._dialog_endpoints():
            dialog = endpoint.active_dialogs.get(call_id)
            if dialog is None or dialog.invite is None:
                continue
            previous = dialog.invite
            candidate = await endpoint.async_prepare_video_reinvite(
                call_id, **kwargs
            )
            if candidate is None:
                return None
            return PreparedEndpointVideoReinvite(
                endpoint, call_id, previous, candidate
            )
        return None

    async def async_activate_video_reinvite(
        self,
        call_id: str,
        *,
        local_video_rtp_port: int,
        video_formats: tuple[sdp.RtpVideoFormat, ...],
        video_direction: str,
    ) -> bool:
        """Negotiate and commit one local video direction change."""

        prepared = await self.async_prepare_video_reinvite(
            call_id,
            local_video_rtp_port=local_video_rtp_port,
            video_formats=video_formats,
            video_direction=video_direction,
        )
        return bool(prepared is not None and prepared.commit())

    async def async_request_video_keyframe(self, call_id: str) -> bool:
        """Ask the transport owning an inbound dialog for a decoder refresh."""

        for endpoint in self._dialog_endpoints():
            if call_id not in endpoint.active_dialogs:
                continue
            request = getattr(endpoint, "request_video_keyframe", None)
            return bool(callable(request) and await request(call_id))
        return False

    def _active_dialog_count_for(self, server: object) -> int:
        endpoint = getattr(server, "endpoint", None)
        active = getattr(endpoint, "active_dialogs", None)
        count = len(active) if isinstance(active, dict) else 0
        endpoints = getattr(server, "endpoints", None)
        if isinstance(endpoints, set):
            for endpoint in endpoints:
                active = getattr(endpoint, "active_dialogs", None)
                if isinstance(active, dict):
                    count += len(active)
        return count

    def _active_call_ids(self, server: object) -> set[str]:
        endpoint = getattr(server, "endpoint", None)
        active = getattr(endpoint, "active_dialogs", None)
        ids = set(active) if isinstance(active, dict) else set()
        endpoints = getattr(server, "endpoints", None)
        if isinstance(endpoints, set):
            for endpoint in endpoints:
                active = getattr(endpoint, "active_dialogs", None)
                if isinstance(active, dict):
                    ids.update(active)
        return ids

    def _endpoint_snapshots(self) -> list[dict]:
        snapshots: list[dict] = []
        for server in self.servers:
            endpoint = getattr(server, "endpoint", None)
            snap = getattr(endpoint, "snapshot", None)
            if callable(snap):
                snapshots.append(dict(snap()))
            endpoints = getattr(server, "endpoints", None)
            if isinstance(endpoints, set):
                for endpoint in endpoints:
                    snap = getattr(endpoint, "snapshot", None)
                    if callable(snap):
                        snapshots.append(dict(snap()))
        return snapshots

    def active_dialog_count(self) -> int:
        return sum(self._active_dialog_count_for(server) for server in self.servers)

    def snapshot(self) -> SipEndpointSnapshot:
        pending_ids: set[str] = set()
        active_ids: set[str] = set()
        for server in self.servers:
            pending_ids.update(self._pending_call_ids(server))
            active_ids.update(self._active_call_ids(server))
        last_event = ""
        last_status = 0
        last_reason = ""
        for snap in self._endpoint_snapshots():
            if snap.get("last_sip_event"):
                last_event = str(snap.get("last_sip_event") or "")
                last_status = int(snap.get("last_sip_status_code") or 0)
                last_reason = str(snap.get("last_sip_reason") or "")
        return SipEndpointSnapshot(
            udp_ready=self.udp_server is not None,
            tcp_ready=self.tcp_server is not None,
            pending_transactions=len(pending_ids),
            active_dialogs=self.active_dialog_count(),
            pending_call_ids=tuple(sorted(pending_ids)),
            active_call_ids=tuple(sorted(active_ids)),
            last_sip_event=last_event,
            last_sip_status_code=last_status,
            last_sip_reason=last_reason,
        )
