"""Home Assistant adapter for browser-to-browser logical phone calls."""

from __future__ import annotations

import logging
import secrets
import asyncio
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback

from .call_projection import (
    observe_phone_leg_projection,
    publish_phone_projection,
    stage_phone_termination_projection,
)
from .core.audio_format import HA_SIP_PCM_FORMATS
from .endpoint_lifecycle import call_registry
from .endpoint_termination import EndpointTerminationHandler
from .endpoint_session import TerminationInitiator, TerminationIntent
from .fsm import TerminalReason
from .local_softphone_bridge import (
    LocalBridgeEvent,
    LocalBridgeEventType,
    LocalCallEndReason,
    LocalCallSnapshot,
    LocalCallState,
    LocalSoftphoneBridge,
)
from .runtime_data import endpoint_directory, runtime_data

if TYPE_CHECKING:
    from .phone_endpoint import PhoneEndpoint


_LOGGER = logging.getLogger(__name__)
LOCAL_ROUTE_KIND = "local"
LOCAL_VIDEO_FORMAT = "VP8/90000"
LOCAL_AUDIO_FORMAT = HA_SIP_PCM_FORMATS[0]


def local_softphone_bridge(hass: HomeAssistant) -> LocalSoftphoneBridge | None:
    """Return the configured in-memory bridge, if logical phones are enabled."""
    runtime = runtime_data(hass)
    bridge = runtime.local_bridge if runtime is not None else None
    return bridge if isinstance(bridge, LocalSoftphoneBridge) else None


@callback
def start_local_softphone_call(
    hass: HomeAssistant,
    caller_endpoint_id: str,
    callee_endpoint_id: str,
    *,
    call_id: str = "",
    request_video: bool = False,
    enable_caller_video_send: bool = False,
    caller_owner_id: str = "",
    context: object | None = None,
    preserve_controller: bool = False,
    policy_endpoint_ids: tuple[str, ...] | None = None,
    outbound_trunk_id: str | None = None,
) -> LocalCallSnapshot:
    """Create one local call with HA provenance bound before its first event."""
    bridge = local_softphone_bridge(hass)
    if bridge is None:
        raise RuntimeError("local softphone bridge is unavailable")
    call_id = str(call_id or "").strip() or f"local-{secrets.token_hex(16)}"
    registry = call_registry(hass)
    registry.upsert(
        call_id,
        state="connecting",
        owner="local_bridge",
        endpoint_id=caller_endpoint_id,
        source_endpoint_id=caller_endpoint_id,
        dest_endpoint_id=callee_endpoint_id,
        local_bridge=True,
        **({"policy_endpoint_ids": policy_endpoint_ids} if policy_endpoint_ids is not None else {}),
        **({"outbound_trunk_id": outbound_trunk_id} if outbound_trunk_id is not None else {}),
    )
    if not preserve_controller:
        registry.bind_controller(
            call_id,
            context=context,
            endpoint_id=caller_endpoint_id,
        )
    try:
        return bridge.start_call(
            caller_endpoint_id,
            callee_endpoint_id,
            call_id=call_id,
            request_video=request_video,
            enable_caller_video_send=enable_caller_video_send,
            caller_owner_id=caller_owner_id,
        )
    except BaseException:
        EndpointTerminationHandler(hass).request_reason(
            call_id,
            "local_bridge_start_failed",
            TerminationInitiator.RUNTIME,
        )
        raise


def _endpoint(hass: HomeAssistant, endpoint_id: str) -> PhoneEndpoint | None:
    return endpoint_directory(hass).get(endpoint_id)


def _device_id(endpoint: PhoneEndpoint | None) -> str:
    return str(getattr(endpoint, "device_id", "") or "")


def _name(endpoint: PhoneEndpoint | None, fallback: str) -> str:
    return str(getattr(endpoint, "name", "") or fallback).strip()


def _state_value(state: LocalCallState) -> str:
    return state.value


def _terminal_state(
    snapshot: LocalCallSnapshot,
    endpoint_id: str,
) -> str:
    if snapshot.end_reason is LocalCallEndReason.DECLINED:
        return "declined"
    return "idle"


def _reason(snapshot: LocalCallSnapshot, endpoint_id: str) -> str:
    """Return the terminal reason from one endpoint's point of view."""
    if snapshot.end_reason is LocalCallEndReason.DECLINED:
        return "declined"
    if snapshot.end_reason is LocalCallEndReason.SHUTDOWN:
        return "shutdown"
    is_caller = endpoint_id == snapshot.caller_endpoint_id
    ended_locally = (
        is_caller and snapshot.end_reason is LocalCallEndReason.CALLER_HANGUP
    ) or (not is_caller and snapshot.end_reason is LocalCallEndReason.CALLEE_HANGUP)
    return "local_hangup" if ended_locally else "remote_hangup"


def _origin(snapshot: LocalCallSnapshot, endpoint_id: str) -> str:
    """Return whether a terminal transition originated at this endpoint."""
    if snapshot.end_reason is LocalCallEndReason.SHUTDOWN:
        return "system"
    is_caller = endpoint_id == snapshot.caller_endpoint_id
    if snapshot.end_reason is LocalCallEndReason.DECLINED:
        return "remote" if is_caller else "self"
    return "self" if _reason(snapshot, endpoint_id) == "local_hangup" else "remote"


@callback
def _publish_leg(
    hass: HomeAssistant,
    snapshot: LocalCallSnapshot,
    endpoint_id: str,
    *,
    terminal: bool = False,
    event_type: str = "",
) -> None:
    """Project one local bridge leg into the existing softphone contract."""
    caller_endpoint = _endpoint(hass, snapshot.caller_endpoint_id)
    callee_endpoint = _endpoint(hass, snapshot.callee_endpoint_id)
    caller_name = _name(caller_endpoint, snapshot.caller_endpoint_id)
    callee_name = _name(callee_endpoint, snapshot.callee_endpoint_id)
    is_caller = endpoint_id == snapshot.caller_endpoint_id
    peer_endpoint = callee_endpoint if is_caller else caller_endpoint
    local_name = caller_name if is_caller else callee_name
    peer_name = callee_name if is_caller else caller_name
    state = (
        _terminal_state(snapshot, endpoint_id)
        if terminal
        else _state_value(snapshot.state_for(endpoint_id))
    )
    video_direction = (
        snapshot.video_direction_for(endpoint_id) if state == "in_call" else "inactive"
    )
    extra: dict[str, object] = {
        "local_name": local_name,
        "role": "caller" if is_caller else "callee",
        "route_kind": LOCAL_ROUTE_KIND,
        "media_transport": "websocket",
        "source_endpoint_id": snapshot.caller_endpoint_id,
        "dest_endpoint_id": snapshot.callee_endpoint_id,
        "source_device_id": _device_id(caller_endpoint),
        "dest_device_id": _device_id(callee_endpoint),
        "target_device_id": _device_id(peer_endpoint),
        "video_offered": bool(snapshot.video_requested),
        "video_active": bool(state == "in_call" and video_direction != "inactive"),
        "video_format": LOCAL_VIDEO_FORMAT if snapshot.video_enabled else "",
        "video_send_format": LOCAL_VIDEO_FORMAT if snapshot.video_enabled else "",
        "video_receive_format": LOCAL_VIDEO_FORMAT if snapshot.video_enabled else "",
        "video_direction": video_direction,
        "last_sip_event": (
            "LOCAL_CALL_ENDED"
            if terminal
            else "LOCAL_ANSWER"
            if state == "in_call"
            else "LOCAL_INVITE"
        ),
    }
    if state == "in_call":
        extra.update(
            {
                "selected_tx_format": LOCAL_AUDIO_FORMAT.wire_token(),
                "selected_rx_format": LOCAL_AUDIO_FORMAT.wire_token(),
                "audio_mode": "full_duplex",
                "audio_direction": "sendrecv",
                "sip_status_code": 200,
            }
        )
    elif state == "ringing":
        extra["sip_status_code"] = 180
    if event_type:
        extra["event_type"] = event_type
    if terminal:
        terminal_reason = _reason(snapshot, endpoint_id)
        extra.update(
            {
                "reason": terminal_reason,
                "terminal_reason": terminal_reason,
                "origin": _origin(snapshot, endpoint_id),
            }
        )
    session = call_registry(hass).get_session(snapshot.call_id)
    if session is None:
        return
    if terminal:
        registry = call_registry(hass)
        registry.observe_leg(
            snapshot.call_id,
            f"local:{endpoint_id}",
            role="local_phone",
            state=state,
            endpoint_id=endpoint_id,
            generation=session.generation,
        )
        stage_phone_termination_projection(
            session,
            endpoint_id,
            peer_name=peer_name,
            direction="outgoing" if is_caller else "incoming",
            **extra,
        )
        return
    publish_phone_projection(
        hass,
        session,
        endpoint_id,
        leg_id=f"local:{endpoint_id}",
        peer_name=peer_name,
        direction="outgoing" if is_caller else "incoming",
        **extra,
    )


@callback
def _bridge_event(hass: HomeAssistant, event: LocalBridgeEvent) -> None:
    snapshot = event.call
    registry = call_registry(hass)
    if event.event_type is LocalBridgeEventType.REDIRECTED:
        session = registry.get_session(snapshot.call_id)
        if session is None:
            return
        observe_phone_leg_projection(
            hass, registry, session, event.endpoint_id, "idle",
            leg_id=f"local:{event.endpoint_id}", role="local_phone",
            peer_name=_name(_endpoint(hass, snapshot.caller_endpoint_id), snapshot.caller_endpoint_id),
            direction="incoming", reason="forwarded", last_sip_event="ROUTE_FORWARD",
        )
        registry.remove_leg(snapshot.call_id, f"local:{event.endpoint_id}")
    if event.event_type in {LocalBridgeEventType.STARTED, LocalBridgeEventType.REDIRECTED}:
        registry.upsert(
            snapshot.call_id,
            state="ringing",
            owner="local_bridge",
            caller=_name(
                _endpoint(hass, snapshot.caller_endpoint_id),
                snapshot.caller_endpoint_id,
            ),
            callee=_name(
                _endpoint(hass, snapshot.callee_endpoint_id),
                snapshot.callee_endpoint_id,
            ),
            route_kind=LOCAL_ROUTE_KIND,
            endpoint_id=snapshot.caller_endpoint_id,
            source_endpoint_id=snapshot.caller_endpoint_id,
            dest_endpoint_id=snapshot.callee_endpoint_id,
            target_endpoint_id=snapshot.callee_endpoint_id,
            media_client_id=snapshot.caller_media_owner_id,
            source_media_client_id=snapshot.caller_media_owner_id,
            local_bridge=True,
        )
        registry.add_leg(
            snapshot.call_id,
            f"local:{snapshot.caller_endpoint_id}",
            role="local_phone",
            state="calling",
            endpoint_id=snapshot.caller_endpoint_id,
        )
        registry.add_leg(
            snapshot.call_id,
            f"local:{snapshot.callee_endpoint_id}",
            role="local_phone",
            state="ringing",
            endpoint_id=snapshot.callee_endpoint_id,
        )
        media = {
            "local_bridge": True,
            "endpoint_ids": (
                snapshot.caller_endpoint_id,
                snapshot.callee_endpoint_id,
            ),
        }
        if event.event_type is LocalBridgeEventType.REDIRECTED:
            registry.update_media(snapshot.call_id, **media)
        else:
            registry.attach_media(snapshot.call_id, media)
        _publish_leg(hass, snapshot, snapshot.caller_endpoint_id)
        _publish_leg(hass, snapshot, snapshot.callee_endpoint_id)
        return

    if event.event_type is LocalBridgeEventType.ANSWERED:
        registry.transition(
            snapshot.call_id,
            state="in_call",
            owner="local_bridge",
        )
        registry.add_leg(
            snapshot.call_id,
            f"local:{snapshot.caller_endpoint_id}",
            role="local_phone",
            state="in_call",
        )
        registry.add_leg(
            snapshot.call_id,
            f"local:{snapshot.callee_endpoint_id}",
            role="local_phone",
            state="in_call",
        )
        _publish_leg(hass, snapshot, snapshot.caller_endpoint_id)
        _publish_leg(hass, snapshot, snapshot.callee_endpoint_id)
        return

    if event.event_type is LocalBridgeEventType.VIDEO_UPDATED:
        _publish_leg(hass, snapshot, snapshot.caller_endpoint_id, event_type="state_changed")
        _publish_leg(hass, snapshot, snapshot.callee_endpoint_id, event_type="state_changed")
        return

    if event.event_type is LocalBridgeEventType.ENDED:
        _publish_leg(
            hass,
            snapshot,
            snapshot.caller_endpoint_id,
            terminal=True,
        )
        _publish_leg(
            hass,
            snapshot,
            snapshot.callee_endpoint_id,
            terminal=True,
        )
        reason = (
            TerminalReason.DECLINED.value
            if snapshot.end_reason is LocalCallEndReason.DECLINED
            else TerminalReason.LOCAL_HANGUP.value
        )
        EndpointTerminationHandler(hass).request(
            snapshot.call_id,
            TerminationIntent(reason, TerminationInitiator.RUNTIME),
        )


@callback
def async_setup_local_softphone_bridge(
    hass: HomeAssistant,
) -> LocalSoftphoneBridge | None:
    """Install one local bridge over the configured endpoint registry."""
    runtime = runtime_data(hass)
    if runtime is None:
        return None
    existing = local_softphone_bridge(hass)
    if existing is not None:
        return existing
    endpoint_registry = endpoint_directory(hass)
    bridge = LocalSoftphoneBridge(endpoint_registry)
    runtime.local_bridge = bridge
    runtime.local_bridge_unsub = bridge.subscribe(
        lambda event: _bridge_event(hass, event)
    )
    return bridge


@callback
def async_shutdown_local_softphone_bridge(hass: HomeAssistant) -> None:
    """End every local call and detach its state adapter."""
    runtime = runtime_data(hass)
    if runtime is None:
        return
    bridge = local_softphone_bridge(hass)
    if bridge is not None:
        bridge.close()
    unsubscribe = runtime.local_bridge_unsub
    if unsubscribe is not None:
        unsubscribe()
    runtime.local_bridge = None
    runtime.local_bridge_unsub = None


@dataclass(frozen=True, slots=True)
class BrowserSourceIdentity:
    """The existing browser caller and its current destination, not a new phone."""

    call_id: str
    caller_endpoint_id: str
    callee_endpoint_id: str
    caller_media_owner_id: str


class PendingLocalSource:
    """Move a ringing browser source into the existing RTP call owners."""

    def __init__(
        self, hass: HomeAssistant, snapshot: LocalCallSnapshot | None,
        local_ip: str, sip_port: int, *, call_id: str = "", retained_endpoint_id: str = "",
    ) -> None:
        from .core import sdp, sip
        from .sip_listener import SipInvite

        self.hass = hass
        self.registry = call_registry(hass)
        self.bridge = local_softphone_bridge(hass)
        self.snapshot = snapshot
        self.outgoing = snapshot is None
        self.retired = False
        self.established = bool(retained_endpoint_id)
        session = self.registry.get_session(snapshot.call_id if snapshot else call_id)
        if self.established:
            if (snapshot is None or snapshot.caller_state is not LocalCallState.IN_CALL
                    or snapshot.callee_state is not LocalCallState.IN_CALL or snapshot.video_active
                    or retained_endpoint_id not in {snapshot.caller_endpoint_id, snapshot.callee_endpoint_id}):
                raise RuntimeError("local handoff requires an established audio participant")
        if session is None or (snapshot is not None and not self.established
                               and snapshot.callee_state is not LocalCallState.RINGING):
            raise RuntimeError("local call is no longer ringing")
        if self.outgoing:
            self.previous_client = self.registry.sip_client_for(call_id)
            if (session.owner != "ha_softphone" or session.state not in {"calling", "connecting", "remote_ringing"}
                    or self.previous_client is None or self.previous_client.dialog is not None):
                raise RuntimeError("outgoing browser call is no longer ringing")
            self.call = BrowserSourceIdentity(
                session.call_id, str(session.metadata.get("source_endpoint_id") or ""),
                str(session.metadata.get("target_endpoint_id") or ""),
                str(session.metadata.get("source_media_client_id") or session.metadata.get("media_client_id") or ""),
            )
        else:
            self.previous_client = None
            if retained_endpoint_id == snapshot.callee_endpoint_id:
                self.call = BrowserSourceIdentity(snapshot.call_id, snapshot.callee_endpoint_id,
                    snapshot.caller_endpoint_id, snapshot.callee_media_owner_id)
            else:
                self.call = BrowserSourceIdentity(snapshot.call_id, snapshot.caller_endpoint_id,
                                                  snapshot.callee_endpoint_id, snapshot.caller_media_owner_id)
        self.video_requested = bool(self.previous_client.video_formats) if self.outgoing else snapshot.video_requested
        self.caller_video_send = (
            self.previous_client.video_direction in {"sendonly", "sendrecv"} and self.video_requested
            if self.outgoing else snapshot.caller_video_send
        )
        self.token = session.token
        self.media = self.registry.resource_for(session.call_id, "softphone_media")
        if self.outgoing and self.media is None:
            self.media = {}
        if not isinstance(self.media, dict):
            raise RuntimeError("local call media ownership is missing")
        self.original_media = dict(self.media)
        self.original_identity = {key: session.metadata.get(key) for key in (
            "endpoint_id", "source_endpoint_id", "dest_endpoint_id", "target_endpoint_id", "local_bridge",
            "source_device_id", "dest_device_id", "target_device_id", "session_device_id",
            "media_client_id", "source_media_client_id", "bridge_dest_call_id", "outbound_trunk_id",
        )} if self.established else {}
        self.original_parties = (session.caller, session.callee) if self.established else ("", "")
        self.paused = False
        self.committed = False
        caller = _endpoint(hass, self.call.caller_endpoint_id)
        callee = _endpoint(hass, self.call.callee_endpoint_id)
        if caller is None or (callee is None and not self.outgoing):
            raise RuntimeError("local phone disappeared during forwarding")
        rtp_format = next(
            fmt for fmt in HA_SIP_PCM_FORMATS
            if fmt.channels == 1 and fmt.nominal_frame_bytes <= 1200
        )
        self.invite = SipInvite(
            source_host=local_ip, source_port=sip_port,
            request_uri=sip.SipUri(callee.sip_uri_user if callee else session.callee, local_ip, sip_port),
            caller_uri=sip.SipUri(caller.sip_uri_user, local_ip, sip_port),
            target=callee.name if callee else session.callee, caller=caller.name, call_id=session.call_id,
            cseq="1 INVITE", remote_sdp=b"",
            send_format=sdp.audio_format_to_rtp(rtp_format, 96),
            recv_format=sdp.audio_format_to_rtp(rtp_format, 96),
            remote_rtp_host=local_ip, remote_rtp_port=0,
            video_format=(replace(self.previous_client.video_format, direction=self.previous_client.video_direction)
                          if self.outgoing and self.previous_client.video_format is not None else None),
            remote_video_rtp_host=local_ip if self.video_requested else "",
        )

    def pause(self) -> None:
        if not self.registry.is_generation_current(self.token.call_id, self.token.generation):
            raise RuntimeError("local source ended during forwarding")
        session = self.registry.get_session(self.token.call_id)
        if self.outgoing or self.established:
            self.paused = True
            return
        self._publish_retired_phone(session)
        self.paused = True

    def _publish_retired_phone(self, session) -> None:
        retired = self.call.callee_endpoint_id
        outgoing = self.snapshot is not None and retired == self.snapshot.caller_endpoint_id
        observe_phone_leg_projection(
            self.hass, self.registry, session, retired, "idle",
            leg_id=f"local:{retired}", role="local_phone",
            peer_name=self.invite.caller, direction="outgoing" if outgoing else "incoming", reason="forwarded",
            last_sip_event="ROUTE_FORWARD",
        )

    async def retire_destination(self) -> None:
        """Close the old UAC leg through its owner before using a replacement."""
        if not self.outgoing or self.retired:
            return
        try:
            await self.registry.close_leg(self.token.call_id, self.previous_client.dialog_ids.call_id,
                                          reason="forwarded")
        finally:
            self.retired = self.registry.sip_client_for(self.previous_client.dialog_ids.call_id) is not self.previous_client
        if self.call.callee_endpoint_id:
            self.registry.release_endpoint_claim(self.token.call_id, self.call.callee_endpoint_id)

    def stage_media(self, descriptor: dict) -> None:
        self.media.clear()
        self.media.update(descriptor)
        if self.outgoing and self.registry.resource_for(self.token.call_id, "softphone_media") is not self.media:
            self.registry.attach_media(self.token.call_id, self.media)

    def stage_application_media(self, invite, source_port: int, browser_port: int):
        """Connect the existing browser RTP adapter to a local application."""
        from dataclasses import replace
        from .core.sdp import build_answer_directional

        local_ip = invite.source_host
        browser_reservation = self.media.get("rtp_reservation")
        if browser_reservation is not None:
            browser_port = browser_reservation.ports[0]
        self.stage_media({
            **({"rtp_reservation": browser_reservation} if browser_reservation is not None else {}),
            "invite": replace(
                invite, remote_rtp_port=source_port,
                remote_sdp=build_answer_directional(
                    local_ip, local_ip, source_port, invite.send_format, invite.recv_format,
                ).encode(),
            ),
            "local_rtp_port": browser_port,
            "endpoint_id": self.call.caller_endpoint_id,
            "media_client_id": self.call.caller_media_owner_id,
        })
        return replace(
            invite, remote_rtp_port=browser_port,
            remote_sdp=build_answer_directional(
                local_ip, local_ip, browser_port, invite.send_format, invite.recv_format,
            ).encode(),
        )

    def connect_browser(self, endpoint_id: str, media_owner_id: str, *, send_video: bool,
                        accept_video: bool = True) -> None:
        """Adopt a selected browser winner without replacing the caller media."""
        if not self.registry.is_generation_current(self.token.call_id, self.token.generation):
            raise RuntimeError("local source ended during forwarding")
        if self.outgoing:
            self.redirect_browser(endpoint_id, request_video=send_video)
            self.bridge.answer(self.token.call_id, endpoint_id, media_owner_id,
                               enable_video_send=send_video, accept_video=accept_video)
            return
        current = self.bridge.require_call(self.token.call_id)
        if current.callee_state is not LocalCallState.RINGING:
            raise RuntimeError("local source was already answered")
        self.registry.release_endpoint_claim(self.token.call_id, endpoint_id)
        self.registry.take_pending_invite(self.token.call_id)
        self.bridge.redirect_ringing(self.token.call_id, endpoint_id)
        self.committed = True
        self.bridge.answer(self.token.call_id, endpoint_id, media_owner_id,
                           enable_video_send=send_video, accept_video=accept_video)

    def redirect_browser(self, endpoint_id: str, *, request_video: bool | None = None) -> None:
        """Continue the actual browser caller through the existing local bridge."""
        if not self.committed and (not self.outgoing or not self.retired):
            raise RuntimeError("previous destination has not been released")
        from .media_reservation import release_media_reservation
        self.registry.take_pending_invite(self.token.call_id)
        release_media_reservation(self.registry.take_media(self.token.call_id))
        release_media_reservation(self.registry.take_media(self.token.call_id, provisional=True))
        self.registry.release_endpoint_claim(self.token.call_id, endpoint_id)
        # The local bridge acquires both endpoint claims synchronously below.
        # Transfer the caller claim instead of leaving it owned by the old UAC.
        self.registry.release_endpoint_claim(self.token.call_id, self.call.caller_endpoint_id)
        start_local_softphone_call(
            self.hass, self.call.caller_endpoint_id, endpoint_id, call_id=self.token.call_id,
            caller_owner_id=self.call.caller_media_owner_id, preserve_controller=True,
            request_video=self.video_requested if request_video is None else request_video,
            enable_caller_video_send=self.caller_video_send,
        )
        self.committed = True
        self.registry.release_resource(self.token.call_id, f"browser_source:{self.token.call_id}", value=self)

    def commit(self, *, keep_media: bool = False) -> None:
        if not self.registry.is_generation_current(self.token.call_id, self.token.generation):
            raise RuntimeError("local source ended during forwarding")
        if self.outgoing:
            if not self.retired:
                raise RuntimeError("previous SIP destination is still active")
        else:
            current = self.bridge.require_call(self.token.call_id)
            expected = LocalCallState.IN_CALL if self.established else LocalCallState.RINGING
            if current.callee_state is not expected:
                raise RuntimeError("local source changed before handoff")
            if self.established and current != replace(
                self.snapshot, caller_media=current.caller_media, callee_media=current.callee_media
            ):
                raise RuntimeError("local media ownership changed before handoff")
        self.registry.claim_endpoint(self.token.call_id, self.call.caller_endpoint_id, role="source")
        if not self.outgoing:
            if self.established:
                self.bridge.detach_established(self.token.call_id, self.call.caller_endpoint_id)
            else:
                self.bridge.detach_ringing(self.token.call_id)
        self.committed = True
        if not keep_media:
            self.registry.take_media(self.token.call_id)
        for endpoint in (self.call.caller_endpoint_id, self.call.callee_endpoint_id):
            self.registry.remove_leg(self.token.call_id, f"local:{endpoint}")
        session = self.registry.get_session(self.token.call_id)
        session.metadata.update(
            local_bridge=False, endpoint_id=self.call.caller_endpoint_id,
            source_endpoint_id=self.call.caller_endpoint_id,
            dest_endpoint_id="", target_endpoint_id="",
            media_client_id=self.call.caller_media_owner_id,
            source_media_client_id=self.call.caller_media_owner_id,
        )
        if self.established:
            self._publish_retired_phone(session)
            self.registry.remove_leg(self.token.call_id, f"local:{self.call.callee_endpoint_id}")

    def resume(self, *, redial: bool = False, destination: str = "", trunk_id: str = "") -> None:
        if self.outgoing:
            if self.retired and redial:
                session = self.registry.get_session(self.token.call_id, generation=self.token.generation)
                previous = self.registry.task_for(self.token.call_id, "forward")

                async def retry_original_route():
                    if previous is not None and previous is not asyncio.current_task():
                        await asyncio.gather(previous, return_exceptions=True)
                    if not self.registry.is_generation_current(self.token.call_id, self.token.generation):
                        return
                    try:
                        await self.registry.forward_call(
                            call_id=self.token.call_id, destination=destination or self.invite.target, on_failure="terminate",
                            **({"trunk_id": trunk_id} if trunk_id else {}),
                        )
                    except Exception:
                        self.registry.request_termination(self.token.call_id, TerminationIntent("resume_failed"),
                                                           generation=self.token.generation)

                if session is not None and session.live:
                    session.create_task(retry_original_route(), name="browser-route-resume")
            elif self.retired:
                self.registry.request_termination(
                    self.token.call_id, TerminationIntent("route_failed", initiator=TerminationInitiator.ROUTING),
                    generation=self.token.generation,
                )
            else:
                if self.registry.artifact_for(self.token.call_id, "pending_invite") is self.invite:
                    self.registry.take_pending_invite(self.token.call_id)
                self.registry.release_resource(self.token.call_id, f"browser_source:{self.token.call_id}", value=self)
            return
        current = self.bridge.get_call(self.token.call_id)
        if current is None or not self.registry.is_generation_current(self.token.call_id, self.token.generation):
            return
        self.media.clear()
        self.media.update(self.original_media)
        self.registry.take_pending_invite(self.token.call_id)
        session = self.registry.get_session(self.token.call_id)
        if self.established:
            for key, value in self.original_identity.items():
                if value is None:
                    session.metadata.pop(key, None)
                else:
                    session.metadata[key] = value
            session.update_metadata(media_client_id=current.caller_media_owner_id,
                                    source_media_client_id=current.caller_media_owner_id)
            self.registry.transition(self.token.call_id, owner="local_bridge", state="in_call",
                caller=self.original_parties[0], callee=self.original_parties[1], route_kind="local")
            for endpoint in (self.call.caller_endpoint_id, self.call.callee_endpoint_id):
                _publish_leg(self.hass, current, endpoint, event_type="state_changed")
            return
        if session.owner != "local_bridge" or session.state != "ringing":
            self.registry.transition(
                self.token.call_id, owner="local_bridge", state="ringing",
                callee=self.invite.target, route_kind="local",
            )
        if not self.paused:
            return
        for endpoint in (self.call.caller_endpoint_id, self.call.callee_endpoint_id):
            self.registry.add_leg(
                self.token.call_id, f"local:{endpoint}", role="local_phone",
                state=self.snapshot.state_for(endpoint).value, endpoint_id=endpoint,
            )
            _publish_leg(self.hass, self.snapshot, endpoint)

    def connected(self, destination: str, route_kind: str) -> None:
        session = self.registry.get_session(self.token.call_id, generation=self.token.generation)
        if session is None or not session.live:
            return
        peer_id = str(session.metadata.get("dest_endpoint_id") or "")
        peer = _endpoint(self.hass, peer_id)
        source = _endpoint(self.hass, self.call.caller_endpoint_id)
        session.update_metadata(target_endpoint_id=peer_id, target_device_id=_device_id(peer),
                                source_device_id=_device_id(source), dest_device_id=_device_id(peer))
        observe_phone_leg_projection(
            self.hass, self.registry, session, self.call.caller_endpoint_id, "in_call",
            leg_id=f"browser-origin:{self.call.caller_endpoint_id}",
            peer_name=destination, direction="outgoing", media_transport="rtp",
            target_device_id=_device_id(peer),
            selected_tx_format=self.invite.recv_format.audio_format.wire_token(),
            selected_rx_format=self.invite.send_format.audio_format.wire_token(),
            selected_tx_rtp_format=self.invite.recv_format.wire_token(),
            selected_rx_rtp_format=self.invite.send_format.wire_token(),
            audio_mode="full_duplex", route_kind=route_kind,
            sip_status_code=200, last_sip_event="ROUTE_FORWARD",
        )
