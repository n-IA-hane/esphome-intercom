"""Inbound SIP INVITE route selection and UAS orchestration.

The router chooses a destination and builds the provisional/final SIP result.
It does not own the dialog lifetime: the listener owns UAS transactions and
the PBX session owner owns calls, legs and teardown.  ``InviteRuntime`` makes
the remaining composition dependencies explicit so route-specific code cannot
quietly grow a second registry or cleanup path.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import logging
import time
from typing import TYPE_CHECKING, Any, Callable

from homeassistant.core import HomeAssistant

from . import sdp as sip_sdp
from .audio_format import HA_TRUNK_AUDIO_FORMATS
from .call_registry import TERMINAL_STATES
from .call_scope import pending_routes as _pending_routes
from .config import debug_mode as _debug_mode
from .const import (
    CONF_AUTOMATION_ROUTING_ENABLED,
    CONF_SIP_VIDEO,
    CONF_TRUNK_AUTH_USERNAME,
    CONF_TRUNK_DTMF_ENABLED,
    CONF_TRUNK_DTMF_TIMEOUT_MS,
    CONF_TRUNK_INBOUND_DEFAULT_TARGET,
    CONF_TRUNK_INBOUND_MODE,
    CONF_TRUNK_OUTBOUND_PROXY,
    CONF_TRUNK_PASSWORD,
    CONF_TRUNK_PORT,
    CONF_TRUNK_SERVER,
    CONF_TRUNK_TRANSPORT,
    CONF_TRUNK_USERNAME,
    DOMAIN,
    HA_SOFTPHONE_DEVICE_ID,
    TRUNK_INBOUND_MODE_DTMF,
)
from .conference import conference_manager
from .dtmf_events import attach_dtmf_event_bridge as _attach_dtmf_event_bridge
from .endpoint_lifecycle import call_registry as _call_registry, create_runtime_task
from .endpoint_registry import EndpointBusyError
from .endpoint_routing import (
    peer_audio_formats as _peer_audio_formats,
    peer_for_target as _peer_for_target,
    roster_entry_formats as _roster_entry_formats,
    roster_from_peers as _roster_from_peers,
    sip_target_audio_profile as _sip_target_audio_profile,
)
from .fsm import (
    CallState,
    TerminalReason,
    sip_failure_response as _sip_failure_response,
    sip_public_state as _sip_public_state,
    sip_terminal_reason as _sip_terminal_reason,
)
from .groups import GROUP_TYPE_CONFERENCE, GROUP_TYPE_RING
from .media_ports import (
    RtpPortReservation,
    allocate_sip_rtp_port as _allocate_sip_rtp_port,
    release_sip_rtp_port_pair as _release_sip_rtp_port_pair,
    reserve_sip_video_media,
    reserve_sip_video_relay_media,
)
from .outbound_attempts import (
    async_close_client_and_release as _close_client_and_release,
)
from .pbx_routing import roster_entry_for_target as _roster_entry_for_target
from .phone_endpoint import (
    DEFAULT_ENDPOINT_ID,
    EndpointAvailability,
    EndpointKind,
    OfflinePolicy,
)
from .phonebook_runtime import registered_roster_entries as _registered_roster_entries
from .router import RouteAction, RouteReason
from .sdp import (
    build_answer_directional,
    constrained_video_direction,
)
from .sip import parse_sip_uri, sip_endpoints_equal, sip_uri_targets_listener
from .sip_bridge import (
    build_invite_client_relay,
    build_pending_invite_video_relay,
    configure_answered_invite_video_relay,
)
from .sip_client import SIP_TIMER_B, SipCallClient
from .sip_listener import SipInviteResult
from .websocket_api import (
    _set_ha_softphone_call_state,
    _set_sip_bridge_call_state,
)

if TYPE_CHECKING:
    from .sip_listener import SipInvite
    from .sip_registrar import SipRegistrar

_LOGGER = logging.getLogger(__name__)
SIP_ROUTE_DECISION_TIMEOUT = 1.5
MAX_TRUNK_INFO_DIGITS = 16
MAX_PENDING_HA_INVITES = 64


def _invite_dtmf_format(invite):
    formats = sip_sdp.offered_dtmf_formats(invite.remote_sdp)
    return formats[0] if formats else None


@dataclass(slots=True)
class InviteRuntime:
    """Explicit dependencies used while routing one inbound INVITE."""

    hass: HomeAssistant
    config: dict[str, Any]
    local_ip: str
    registrar: SipRegistrar
    ha_peer_name: Callable[..., str]
    get_trunk_config: Callable[..., dict[str, Any]]
    trunk_enabled: Callable[..., bool]
    is_trunk_invite: Callable[..., bool]
    is_ha_target: Callable[..., bool]
    ha_router_decision: Callable[..., Any]
    inbound_route_decision: Callable[..., Any]
    build_peer_snapshot: Callable[..., Any]
    attach_client_media_update: Callable[..., None]
    browser_leg_for_member: Callable[..., Any]
    defer_invite_to_softphone: Callable[..., None]
    enable_reused_sip_tcp_connection: Callable[..., Any]
    on_conference_inbound_timeout: Callable[..., Any]
    ring_conference_members: Callable[..., Any]
    run_ring_group_call: Callable[..., Any]
    run_trunk_inbound_route_guarded: Callable[..., Any]
    send_final_response: Callable[..., Any]
    sip_uri_transport: Callable[..., Any]
    start_local_assist_bridge: Callable[..., Any]
    terminate_sip_bridge: Callable[..., Any]


async def route_invite(
    runtime: InviteRuntime,
    invite: SipInvite,
) -> SipInviteResult:
    hass = runtime.hass
    cfg = runtime.config
    local_ip = runtime.local_ip
    registrar = runtime.registrar
    _ha_peer_name = runtime.ha_peer_name
    _get_trunk_config = runtime.get_trunk_config
    _trunk_enabled = runtime.trunk_enabled
    _is_trunk_invite = runtime.is_trunk_invite
    _is_ha_target = runtime.is_ha_target
    _ha_router_decision = runtime.ha_router_decision
    _inbound_route_decision = runtime.inbound_route_decision
    _async_build_peer_snapshot = runtime.build_peer_snapshot
    _attach_client_media_update = runtime.attach_client_media_update
    _browser_leg_for_member = runtime.browser_leg_for_member
    _defer_invite_to_ha_softphone = runtime.defer_invite_to_softphone
    _enable_reused_sip_tcp_connection = runtime.enable_reused_sip_tcp_connection
    _on_conference_inbound_timeout = runtime.on_conference_inbound_timeout
    _ring_conference_members = runtime.ring_conference_members
    _run_ring_group_call = runtime.run_ring_group_call
    _run_trunk_inbound_route_guarded = runtime.run_trunk_inbound_route_guarded
    _sip_send_final_response = runtime.send_final_response
    _sip_uri_transport = runtime.sip_uri_transport
    _start_local_assist_bridge = runtime.start_local_assist_bridge
    _terminate_sip_bridge = runtime.terminate_sip_bridge
    peers = await _async_build_peer_snapshot(hass)
    caller_identity = str(
        (invite.caller_uri.user if invite.caller_uri is not None else "")
        or invite.caller
        or ""
    ).strip()
    caller_peer = _peer_for_target(caller_identity, peers)
    if caller_peer is not None and str(caller_peer.host) != str(invite.source_host):
        caller_peer = None
    if caller_peer is not None:
        send_candidates, recv_candidates = _sip_target_audio_profile(
            remote_tx_formats=_peer_audio_formats(caller_peer, "tx_formats"),
            remote_rx_formats=_peer_audio_formats(caller_peer, "rx_formats"),
            target=caller_peer.name,
        )
        selected = sip_sdp.negotiate_directional(
            invite.remote_sdp,
            send_candidates,
            recv_candidates,
            allow_dahua_pcm=invite.peer_profile == "dahua",
        )
        if selected is None:
            _LOGGER.info(
                "SIP INVITE from %s rejected: roster directional PCM profile is incompatible",
                invite.caller or invite.source_host,
            )
            return SipInviteResult(
                488,
                "Not Acceptable Here",
                to_tag="",
                decline_reason=TerminalReason.MEDIA_INCOMPATIBLE.value,
            )
        invite = replace(
            invite, send_format=selected.send, recv_format=selected.recv
        )
    registered_entries = _registered_roster_entries(hass)
    roster_entries = _roster_from_peers(hass, peers, registered_entries)
    registered_source = registrar.registration_matches_source(
        caller_identity,
        invite.source_host,
        invite.source_port,
        invite.signaling_transport,
    )
    caller_roster_entry = _roster_entry_for_target(caller_identity, roster_entries)
    caller_is_known_roster_endpoint = bool(
        caller_roster_entry is not None
        and caller_roster_entry.address
        and str(caller_roster_entry.address) == str(invite.source_host)
    )
    trunk_invite = _is_trunk_invite(invite)
    trunk_direct_preprocessed = False
    local_ha_origin = bool(
        _is_ha_target(caller_identity)
        and invite.source_host in {local_ip, "127.0.0.1", "::1"}
    )
    caller_is_trusted_endpoint = bool(
        registered_source
        or caller_peer is not None
        or caller_is_known_roster_endpoint
        or trunk_invite
        or local_ha_origin
    )
    decision = _inbound_route_decision(invite, peers, roster_entries)
    if (
        not caller_is_trusted_endpoint
        and decision.action is RouteAction.TRUNK
    ):
        _LOGGER.warning(
            "SIP unauthenticated trunk route rejected caller=%s source=%s:%s target=%s",
            caller_identity or "unknown",
            invite.source_host,
            invite.source_port,
            invite.target,
        )
        return SipInviteResult(
            403,
            "Forbidden",
            to_tag="",
            decline_reason="unauthenticated_trunk",
        )
    if trunk_invite:
        trunk_cfg = _get_trunk_config(hass)
        dtmf_timeout_ms = max(
            0, int(trunk_cfg.get(CONF_TRUNK_DTMF_TIMEOUT_MS) or 0)
        )
        dtmf_preanswer = bool(
            trunk_cfg.get(CONF_TRUNK_INBOUND_MODE) == TRUNK_INBOUND_MODE_DTMF
            and trunk_cfg.get(CONF_TRUNK_DTMF_ENABLED)
            and dtmf_timeout_ms > 0
        )
        if not dtmf_preanswer:
            _LOGGER.info(
                "SIP trunk inbound skips DTMF pre-answer call_id=%s caller=%s",
                invite.call_id,
                invite.caller or invite.source_host,
            )
            default_target = (
                str(
                    trunk_cfg.get(CONF_TRUNK_INBOUND_DEFAULT_TARGET) or "HA"
                ).strip()
                or "HA"
            )
            invite = replace(invite, target=default_target)
            decision = _inbound_route_decision(invite, peers, roster_entries)
            trunk_direct_preprocessed = True
    bucket = hass.data.setdefault(DOMAIN, {})
    registry = _call_registry(hass)
    endpoint_registry = bucket.get("endpoint_registry")
    source_endpoint_id = str(
        ((caller_roster_entry.metadata or {}).get("endpoint_id"))
        if caller_roster_entry is not None
        else ""
    ).strip()
    source_endpoint = (
        endpoint_registry.get(source_endpoint_id)
        if endpoint_registry is not None and source_endpoint_id
        else None
    )
    route_bucket = _pending_routes(hass)
    pending = registry.pending_invites
    if invite.call_id in route_bucket:
        _LOGGER.debug(
            "SIP INVITE retransmit while route is pending call_id=%s",
            invite.call_id,
        )
        return SipInviteResult(100, "Trying", to_tag="")
    if invite.call_id in pending:
        _LOGGER.debug(
            "SIP INVITE retransmit while HA softphone is ringing call_id=%s",
            invite.call_id,
        )
        return SipInviteResult(180, "Ringing", to_tag="", defer_final=True)
    if len(pending) >= MAX_PENDING_HA_INVITES:
        _LOGGER.warning(
            "SIP pending HA call limit reached; rejecting call_id=%s",
            invite.call_id,
        )
        return SipInviteResult(
            503,
            "Service Unavailable",
            to_tag="",
            decline_reason="capacity_exhausted",
        )
    if decision.action is RouteAction.ASSIST and any(
        session.route_kind == RouteAction.ASSIST.value
        and session.state not in TERMINAL_STATES
        for session in registry.sessions.values()
    ):
        return SipInviteResult(
            486, "Busy Here", to_tag="", decline_reason=TerminalReason.BUSY.value
        )
    if decision.action is RouteAction.ASSIST:
        if source_endpoint is not None and source_endpoint.kind is not EndpointKind.BROWSER:
            registry.upsert(
                invite.call_id,
                state=CallState.CONNECTING.value,
                owner="router",
                caller=invite.caller,
                callee=invite.target,
                route_kind=RouteAction.ASSIST.value,
                source_endpoint_id=source_endpoint.endpoint_id,
            )
            try:
                registry.claim_endpoint(
                    invite.call_id,
                    source_endpoint.endpoint_id,
                    role="source",
                    adopt_transport=True,
                )
            except EndpointBusyError:
                registry.finish_and_pop(
                    invite.call_id,
                    reason=TerminalReason.BUSY.value,
                    state=CallState.BUSY.value,
                )
                return SipInviteResult(
                    486,
                    "Busy Here",
                    to_tag="",
                    decline_reason=TerminalReason.BUSY.value,
                )
        try:
            assist_ports = RtpPortReservation.allocate(hass)
        except RuntimeError as err:
            _LOGGER.warning("Assist RTP port allocation failed: %s", err)
            registry.finish_and_pop(
                invite.call_id,
                reason=TerminalReason.TRANSPORT_UNREACHABLE.value,
                state=CallState.TRANSPORT_UNREACHABLE.value,
            )
            return SipInviteResult(503, "Service Unavailable", to_tag="")
        assist_rtp_port = assist_ports.ports[0]
        try:
            await _start_local_assist_bridge(
                invite,
                reservation=assist_ports,
                local_rtp_port=assist_rtp_port,
                roster_entries=roster_entries,
                source="sip",
                called_extension=str(decision.entry.extension or invite.target)
                if decision.entry is not None
                else invite.target,
            )
        except Exception:
            _LOGGER.exception("Assist bridge failed call_id=%s", invite.call_id)
            assist_ports.release()
            registry.finish_and_pop(
                invite.call_id,
                reason=TerminalReason.PROTOCOL_ERROR.value,
                state=CallState.TRANSPORT_UNREACHABLE.value,
            )
            return SipInviteResult(
                500,
                "Server Internal Error",
                to_tag="",
                decline_reason=TerminalReason.PROTOCOL_ERROR.value,
            )
        answer = build_answer_directional(
            local_ip,
            local_ip,
            assist_rtp_port,
            invite.send_format,
            invite.recv_format,
            remote_sdp=invite.remote_sdp,
        )
        return SipInviteResult(200, "OK", answer_sdp=answer, to_tag="")
    if decision.action is RouteAction.GROUP:
        if source_endpoint is not None and source_endpoint.kind is not EndpointKind.BROWSER:
            registry.upsert(
                invite.call_id,
                state=CallState.RINGING.value,
                owner="router",
                caller=invite.caller,
                callee=invite.target,
                route_kind=RouteAction.GROUP.value,
                source_endpoint_id=source_endpoint.endpoint_id,
            )
            try:
                registry.claim_endpoint(
                    invite.call_id,
                    source_endpoint.endpoint_id,
                    role="source",
                    adopt_transport=True,
                )
            except EndpointBusyError:
                registry.finish_and_pop(
                    invite.call_id,
                    reason=TerminalReason.BUSY.value,
                    state=CallState.BUSY.value,
                )
                return SipInviteResult(
                    486,
                    "Busy Here",
                    to_tag="",
                    decline_reason=TerminalReason.BUSY.value,
                )
        group_type = (
            str((decision.entry.metadata or {}).get("group_type") or "")
            if decision.entry is not None
            else ""
        )
        if group_type == GROUP_TYPE_CONFERENCE:
            ring_members = [
                str(member).strip()
                for member in (
                    (decision.entry.metadata or {}).get("ring_members") or []
                )
            ]
            ring_endpoint_ids = tuple(
                leg.endpoint_id
                for member in ring_members
                if (
                    leg := _browser_leg_for_member(
                        member, peers, roster_entries
                    )
                )
                is not None
                and leg.endpoint_id != source_endpoint_id
            )
            result = await conference_manager(
                hass,
                local_ip=local_ip,
                on_inbound_timeout=_on_conference_inbound_timeout,
            ).join(
                invite,
                decision.entry,
                ring_endpoint_ids=ring_endpoint_ids,
            )
            if result.status == 200:
                registry.upsert(
                    invite.call_id,
                    state=CallState.IN_CALL.value,
                    owner="bridge",
                    caller=invite.caller,
                    callee=invite.target,
                    route_kind=GROUP_TYPE_CONFERENCE,
                )
                registry.add_leg(
                    invite.call_id,
                    invite.call_id,
                    role="caller",
                    state=CallState.IN_CALL.value,
                )
                create_runtime_task(
                    hass,
                    _ring_conference_members(
                        room_name=str(
                            decision.entry.name
                            or decision.entry.id
                            or invite.target
                        ),
                        caller=invite.caller,
                        source_host=invite.source_host,
                        entry=decision.entry,
                        peers=peers,
                        roster_entries=roster_entries,
                        owner_call_id=invite.call_id,
                    ),
                )
            else:
                registry.finish_and_pop(
                    invite.call_id,
                    reason=result.decline_reason
                    or TerminalReason.TRANSPORT_UNREACHABLE.value,
                    state=_sip_public_state(
                        result.decline_reason
                        or TerminalReason.TRANSPORT_UNREACHABLE.value
                    ),
                )
            return result
        if group_type == GROUP_TYPE_RING and decision.entry is not None:
            registry.upsert(
                invite.call_id,
                state=CallState.RINGING.value,
                owner="router",
                caller=invite.caller,
                callee=invite.target,
                route_kind=GROUP_TYPE_RING,
            )
            registry.add_leg(
                invite.call_id,
                invite.call_id,
                role="caller",
                state=CallState.RINGING.value,
            )
            create_runtime_task(
                hass,
                _run_ring_group_call(invite, decision.entry, peers, roster_entries),
            )
            return SipInviteResult(180, "Ringing", to_tag="", defer_final=True)
        return SipInviteResult(480, "Temporarily Unavailable", to_tag="")
    if trunk_invite:
        trunk_cfg = _get_trunk_config(hass)
        dtmf_timeout_ms = max(
            0, int(trunk_cfg.get(CONF_TRUNK_DTMF_TIMEOUT_MS) or 0)
        )
        dtmf_preanswer = bool(
            trunk_cfg.get(CONF_TRUNK_INBOUND_MODE) == TRUNK_INBOUND_MODE_DTMF
            and trunk_cfg.get(CONF_TRUNK_DTMF_ENABLED)
            and dtmf_timeout_ms > 0
        )
        if not dtmf_preanswer:
            if not trunk_direct_preprocessed:
                raise RuntimeError("direct trunk route was not preprocessed")
            # Continue through the normal dialplan. The optional route
            # window below is opened only when automation overrides are
            # explicitly enabled.
            source_relay_port = 0
            dest_relay_port = 0
        else:
            # Clear a theoretically reused Call-ID before handing control
            # back to the event loop. A BYE received after this point must
            # remain visible to the background DTMF/router task.
            bucket.setdefault("trunk_closed_calls", set()).discard(invite.call_id)
            bucket.setdefault("trunk_info_queues", {})[invite.call_id] = (
                asyncio.Queue(maxsize=MAX_TRUNK_INFO_DIGITS)
            )
            try:
                bridge_ports = RtpPortReservation.allocate(hass)
            except RuntimeError as err:
                _LOGGER.warning(
                    "SIP trunk RTP bridge port allocation failed: %s", err
                )
                return SipInviteResult(503, "Service Unavailable", to_tag="")
            source_relay_port, _dest_relay_port = bridge_ports.ports
            video_media_reservation = None
            video_rtp_socket = None
            video_rtcp_socket = None
            source_video_port = 0
            video_failure_reason = ""
            if (
                invite.video_format is not None
                and cfg.get(CONF_SIP_VIDEO, False)
            ):
                try:
                    (
                        video_media_reservation,
                        video_rtp_socket,
                        video_rtcp_socket,
                    ) = reserve_sip_video_media(hass)
                    _unused_audio_port, source_video_port = (
                        video_media_reservation.ports
                    )
                except (OSError, RuntimeError) as err:
                    video_failure_reason = (
                        "local_video_resources_unavailable"
                    )
                    _LOGGER.warning(
                        "SIP trunk DTMF video socket unavailable; collecting digits audio-only: %s",
                        err,
                    )
            registry.pending_invites[invite.call_id] = invite
            preanswered_media = {
                # Early media is provisional.  The winning endpoint still
                # owns the final 200/SDP answer and may narrow or enable
                # media according to its actual capabilities and user's
                # camera choice.
                "final_response_sent": False,
                "local_rtp_port": source_relay_port,
                "local_video_rtp_port": source_video_port,
                "video_direction": (
                    "recvonly" if source_video_port else "inactive"
                ),
                "rtp_reservation": bridge_ports,
                "video_rtp_reservation": video_media_reservation,
                "video_rtp_socket": video_rtp_socket,
                "video_rtcp_socket": video_rtcp_socket,
                "video_failure_reason": video_failure_reason,
            }
            registry.upsert(
                invite.call_id,
                state=CallState.CONNECTING.value,
                owner="router",
                caller=invite.caller,
                callee=str(
                    trunk_cfg.get(CONF_TRUNK_INBOUND_DEFAULT_TARGET) or "HA"
                ),
                route_kind="trunk",
                ingress="trunk",
                origin="trunk",
            )
            registry.attach_media(
                invite.call_id,
                preanswered_media,
                provisional=True,
            )
            expires_at = time.time() + (float(dtmf_timeout_ms) / 1000.0)
            dtmf_format = None
            dtmf_formats = sip_sdp.offered_dtmf_formats(invite.remote_sdp)
            dtmf_format = dtmf_formats[0] if dtmf_formats else None
            # RFC 4733 can carry digits in provisional early media. SIP
            # INFO is an in-dialog compatibility transport and common
            # user agents do not expose keypad input until a final 2xx.
            # Confirm only the INFO-only branch; keep RFC 4733 routing
            # provisional so the selected endpoint owns the final answer.
            confirm_for_sip_info = dtmf_format is None
            preanswered_media["final_response_sent"] = confirm_for_sip_info
            preanswer_video_direction = (
                constrained_video_direction(
                    invite.video_format.direction,
                    allow_send=True,
                )
                if source_video_port and invite.video_format is not None
                else "inactive"
            )
            registry.preanswered[invite.call_id]["video_direction"] = (
                preanswer_video_direction
            )
            answer = build_answer_directional(
                local_ip,
                local_ip,
                source_relay_port,
                invite.send_format,
                invite.recv_format,
                dtmf=dtmf_format,
                remote_sdp=invite.remote_sdp,
                video_port=source_video_port,
                video_format=(
                    invite.answer_video_format if source_video_port else None
                ),
                # Advertising the supported direction establishes a
                # standards-valid media contract; it does not grant
                # browser camera access. Actual camera RTP remains gated
                # by the explicit per-card answer choice.
                video_direction=preanswer_video_direction,
            )
            registry.preanswered[invite.call_id]["early_answer_sdp"] = answer
            _set_sip_bridge_call_state(
                hass,
                CallState.CONNECTING.value,
                caller=invite.caller,
                callee=str(
                    trunk_cfg.get(CONF_TRUNK_INBOUND_DEFAULT_TARGET) or "HA"
                ),
                peer_name=invite.caller,
                call_id=invite.call_id,
                selected_tx_format=invite.send_format.audio_format.wire_token(),
                selected_rx_format=invite.recv_format.audio_format.wire_token(),
                selected_tx_rtp_format=invite.send_format.wire_token(),
                selected_rx_rtp_format=invite.recv_format.wire_token(),
                audio_mode="full_duplex",
                route_kind="trunk",
                sip_status_code=200 if confirm_for_sip_info else 183,
                last_sip_event="INVITE",
                direction="incoming",
                scope="sip_trunk",
                phase="dtmf_route",
                source_host=invite.source_host,
                expires_at=expires_at,
                decision_timeout_ms=dtmf_timeout_ms,
                video_requested=bool(invite.video_format is not None),
                video_negotiated=bool(source_video_port),
                video_status=(
                    "degraded"
                    if video_failure_reason
                    else "active"
                    if source_video_port
                    else "rejected"
                    if invite.video_format is not None
                    else "inactive"
                ),
                video_failure_reason=video_failure_reason,
            )
            create_runtime_task(
                hass,
                _run_trunk_inbound_route_guarded(
                    invite,
                    bridge_ports=bridge_ports,
                ),
            )
            if confirm_for_sip_info:
                return SipInviteResult(200, "OK", answer_sdp=answer, to_tag="")
            return SipInviteResult(
                183,
                "Session Progress",
                answer_sdp=answer,
                to_tag="",
                defer_final=True,
            )
    route_action = "default"
    route_destination = ""
    route_status = 0
    route_reason = ""
    route_decline_reason = ""
    automation_routing_enabled = bool(
        _get_trunk_config(hass).get(CONF_AUTOMATION_ROUTING_ENABLED, False)
    )
    if (
        registered_source
        or not caller_is_trusted_endpoint
        or not automation_routing_enabled
    ):
        _LOGGER.debug(
            "SIP caller uses central dialplan without automation window caller=%s target=%s route=%s uri=%s",
            invite.caller or invite.source_host,
            invite.target,
            decision.action.value,
            decision.sip_uri or "-",
        )
    else:
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        expires_at = time.time() + SIP_ROUTE_DECISION_TIMEOUT
        route_bucket[invite.call_id] = {
            "future": future,
            "invite": invite,
            "decision": decision,
            "created_at": time.time(),
            "expires_at": expires_at,
            "decision_deadline": expires_at,
            "fallback_destination": decision.target,
        }
        _set_sip_bridge_call_state(
            hass,
            CallState.CONNECTING.value,
            caller=invite.caller,
            callee=invite.target,
            peer_name=invite.caller,
            local_name=_ha_peer_name(hass),
            call_id=invite.call_id,
            selected_tx_format=invite.send_format.audio_format.wire_token(),
            selected_rx_format=invite.recv_format.audio_format.wire_token(),
            selected_tx_rtp_format=invite.send_format.wire_token(),
            selected_rx_rtp_format=invite.recv_format.wire_token(),
            audio_mode="full_duplex",
            route_kind=decision.action.value,
            sip_uri=decision.sip_uri,
            sip_status_code=100,
            last_sip_event="INVITE",
            direction="incoming",
            ingress="trunk" if trunk_invite else "extension",
            origin="trunk" if trunk_invite else "extension",
            route_request=True,
            phase="route_decision",
            source_host=invite.source_host,
            target=decision.target,
            default_destination=decision.target,
            fallback_destination=decision.target,
            expires_at=expires_at,
            decision_deadline=expires_at,
            decision_timeout_ms=int(SIP_ROUTE_DECISION_TIMEOUT * 1000),
            rtp_format=(
                f"{invite.selected_format.encoding}/"
                f"{invite.selected_format.sample_rate}/"
                f"{invite.selected_format.channels}"
            ),
        )
        _LOGGER.info(
            "SIP route requested: caller=%s target=%s route=%s uri=%s media=%s/%s",
            invite.caller or invite.source_host,
            invite.target,
            decision.action.value,
            decision.sip_uri or "-",
            invite.selected_format.encoding,
            invite.selected_format.sample_rate,
        )
        try:
            route_decision = await asyncio.wait_for(future, timeout=SIP_ROUTE_DECISION_TIMEOUT)
        except asyncio.TimeoutError:
            route_decision = {}
        finally:
            route_bucket.pop(invite.call_id, None)
        if isinstance(route_decision, dict):
            route_action = str(route_decision.get("action") or "default").strip().lower()
            route_destination = str(route_decision.get("destination") or "").strip()
            route_status = int(route_decision.get("status") or 0)
            route_reason = str(route_decision.get("reason") or "").strip()
            route_decline_reason = str(
                route_decision.get("decline_reason") or ""
            ).strip()

    if route_action in {"decline", "busy", "cancel"}:
        if route_action == "busy":
            status = route_status or 486
            reason = route_reason or "Busy Here"
            app_reason = TerminalReason.BUSY.value
        elif route_action == "cancel":
            status = route_status or 487
            reason = route_reason or "Request Terminated"
            app_reason = TerminalReason.CANCELLED.value
        else:
            status = route_status or 603
            reason = route_reason or "Decline"
            app_reason = route_decline_reason or TerminalReason.DECLINED.value
        _set_sip_bridge_call_state(
            hass,
            CallState.BUSY.value
            if app_reason == TerminalReason.BUSY.value
            else CallState.CANCELLED.value
            if status == 487
            else "declined",
            caller=invite.caller,
            callee=invite.target,
            peer_name=invite.caller,
            call_id=invite.call_id,
            reason=app_reason,
            origin="self",
            sip_status_code=status,
            last_sip_event="SIP_RESPONSE",
        )
        return SipInviteResult(status, reason, to_tag="", decline_reason=app_reason)

    fallback_destination = decision.target or invite.target
    if route_action in {"forward", "bridge"} and route_destination:
        decision = _ha_router_decision(route_destination, roster_entries)
        _LOGGER.info(
            "SIP route override call_id=%s action=%s destination=%s route=%s uri=%s",
            invite.call_id,
            route_action,
            route_destination,
            decision.action.value,
            decision.sip_uri or "-",
        )

        # An automation selects a dial-plan destination, not a transport
        # shortcut. Re-enter the canonical PBX dispatcher for destination
        # types that were resolved before the automation window.
        if decision.action is RouteAction.ASSIST:
            try:
                assist_ports = RtpPortReservation.allocate(hass)
            except RuntimeError as err:
                _LOGGER.warning("Assist RTP port allocation failed: %s", err)
                return SipInviteResult(503, "Service Unavailable", to_tag="")
            assist_rtp_port = assist_ports.ports[0]
            try:
                await _start_local_assist_bridge(
                    invite,
                    reservation=assist_ports,
                    local_rtp_port=assist_rtp_port,
                    roster_entries=roster_entries,
                    source="trunk" if trunk_invite else "sip",
                    called_extension=str(
                        decision.entry.extension or route_destination
                    )
                    if decision.entry is not None
                    else route_destination,
                )
            except Exception:
                _LOGGER.exception(
                    "Assist bridge failed call_id=%s", invite.call_id
                )
                assist_ports.release()
                return SipInviteResult(
                    500,
                    "Server Internal Error",
                    to_tag="",
                    decline_reason=TerminalReason.PROTOCOL_ERROR.value,
                )
            answer = build_answer_directional(
                local_ip,
                local_ip,
                assist_rtp_port,
                invite.send_format,
                invite.recv_format,
                remote_sdp=invite.remote_sdp,
            )
            return SipInviteResult(200, "OK", answer_sdp=answer, to_tag="")

        if decision.action is RouteAction.GROUP:
            group_type = (
                str((decision.entry.metadata or {}).get("group_type") or "")
                if decision.entry is not None
                else ""
            )
            if group_type == GROUP_TYPE_RING and decision.entry is not None:
                registry.upsert(
                    invite.call_id,
                    state=CallState.RINGING.value,
                    owner="router",
                    caller=invite.caller,
                    callee=decision.target or route_destination,
                    route_kind=GROUP_TYPE_RING,
                    source_endpoint_id=source_endpoint_id,
                )
                if source_endpoint is not None:
                    try:
                        registry.claim_endpoint(
                            invite.call_id,
                            source_endpoint.endpoint_id,
                            role="source",
                            adopt_transport=True,
                        )
                    except EndpointBusyError:
                        registry.finish_and_pop(
                            invite.call_id,
                            reason=TerminalReason.BUSY.value,
                            state=CallState.BUSY.value,
                        )
                        return SipInviteResult(
                            486,
                            "Busy Here",
                            to_tag="",
                            decline_reason=TerminalReason.BUSY.value,
                        )
                registry.add_leg(
                    invite.call_id,
                    invite.call_id,
                    role="caller",
                    state=CallState.RINGING.value,
                )
                create_runtime_task(
                    hass,
                    _run_ring_group_call(
                        replace(
                            invite,
                            target=decision.target or route_destination,
                        ),
                        decision.entry,
                        peers,
                        roster_entries,
                    ),
                )
                return SipInviteResult(
                    180, "Ringing", to_tag="", defer_final=True
                )
            if group_type == GROUP_TYPE_CONFERENCE and decision.entry is not None:
                ring_members = [
                    str(member).strip()
                    for member in (
                        (decision.entry.metadata or {}).get("ring_members") or []
                    )
                ]
                ring_endpoint_ids = tuple(
                    leg.endpoint_id
                    for member in ring_members
                    if (
                        leg := _browser_leg_for_member(
                            member, peers, roster_entries
                        )
                    )
                    is not None
                    and leg.endpoint_id != source_endpoint_id
                )
                routed_invite = replace(
                    invite,
                    target=decision.target or route_destination,
                )
                result = await conference_manager(
                    hass,
                    local_ip=local_ip,
                    on_inbound_timeout=_on_conference_inbound_timeout,
                ).join(
                    routed_invite,
                    decision.entry,
                    ring_endpoint_ids=ring_endpoint_ids,
                )
                if result.status == 200:
                    registry.upsert(
                        invite.call_id,
                        state=CallState.IN_CALL.value,
                        owner="bridge",
                        caller=invite.caller,
                        callee=routed_invite.target,
                        route_kind=GROUP_TYPE_CONFERENCE,
                        source_endpoint_id=source_endpoint_id,
                    )
                    registry.add_leg(
                        invite.call_id,
                        invite.call_id,
                        role="caller",
                        state=CallState.IN_CALL.value,
                    )
                    create_runtime_task(
                        hass,
                        _ring_conference_members(
                            room_name=str(
                                decision.entry.name
                                or decision.entry.id
                                or routed_invite.target
                            ),
                            caller=invite.caller,
                            source_host=invite.source_host,
                            entry=decision.entry,
                            peers=peers,
                            roster_entries=roster_entries,
                            owner_call_id=invite.call_id,
                        ),
                    )
                return result
            return SipInviteResult(480, "Temporarily Unavailable", to_tag="")

    _LOGGER.info(
        "Inbound route selected call_id=%s source=%s destination=%s fallback=%s",
        invite.call_id,
        "automation"
        if route_action in {"forward", "bridge"} and route_destination
        else "fallback",
        route_destination
        if route_action in {"forward", "bridge"} and route_destination
        else decision.target or invite.target,
        fallback_destination,
    )

    def _decision_endpoint(current_decision):
        if endpoint_registry is None or current_decision.entry is None:
            return None
        endpoint_id = str(
            (current_decision.entry.metadata or {}).get("endpoint_id") or ""
        ).strip()
        return endpoint_registry.get(endpoint_id) if endpoint_id else None

    # Offline forwarding is a logical dial-plan operation. Resolve it
    # before any SIP leg is created and guard loops across endpoint names,
    # extensions and usernames through stable endpoint IDs.
    visited_endpoint_ids: set[str] = set()
    while True:
        candidate_endpoint = _decision_endpoint(decision)
        if (
            candidate_endpoint is None
            or candidate_endpoint.availability
            is EndpointAvailability.AVAILABLE
            or candidate_endpoint.kind is EndpointKind.BROWSER
            or candidate_endpoint.offline_policy is not OfflinePolicy.FORWARD
        ):
            break
        if candidate_endpoint.endpoint_id in visited_endpoint_ids:
            _LOGGER.warning(
                "Offline forward loop rejected call_id=%s endpoint=%s visited=%s",
                invite.call_id,
                candidate_endpoint.endpoint_id,
                sorted(visited_endpoint_ids),
            )
            return SipInviteResult(
                480,
                "Temporarily Unavailable",
                to_tag="",
                decline_reason="forward_loop",
            )
        visited_endpoint_ids.add(candidate_endpoint.endpoint_id)
        forward_target = candidate_endpoint.offline_forward_target
        if not forward_target:
            break
        decision = _ha_router_decision(forward_target, roster_entries)
        _LOGGER.info(
            "Offline endpoint forward call_id=%s endpoint=%s destination=%s route=%s",
            invite.call_id,
            candidate_endpoint.endpoint_id,
            forward_target,
            decision.action.value,
        )

    target_endpoint = _decision_endpoint(decision)

    resolved_callee = str(
        (
            decision.entry.display_name
            if decision.entry is not None
            else decision.target
        )
        or invite.target
    ).strip()

    force_ha_softphone = route_action == "answer_ha"
    trunk_cfg = _get_trunk_config(hass)
    trunk = hass.data.get(DOMAIN, {}).get("sip_trunk")
    trunk_ready = _trunk_enabled(trunk_cfg) and bool(
        getattr(trunk, "registered", False)
    )
    bridge_to_trunk = bool(
        not force_ha_softphone
        and decision.action is RouteAction.TRUNK
        and trunk_ready
    )
    if target_endpoint is not None:
        if target_endpoint.dnd:
            _LOGGER.info(
                "SIP INVITE rejected by endpoint DND call_id=%s endpoint=%s",
                invite.call_id,
                target_endpoint.endpoint_id,
            )
            return SipInviteResult(
                486,
                "Busy Here",
                to_tag="",
                decline_reason="dnd",
            )
        if (
            target_endpoint.active_call_id
            and target_endpoint.active_call_id != invite.call_id
        ):
            return SipInviteResult(
                486,
                "Busy Here",
                to_tag="",
                decline_reason=TerminalReason.BUSY.value,
            )
        if target_endpoint.availability is EndpointAvailability.UNAVAILABLE:
            return SipInviteResult(
                480,
                "Temporarily Unavailable",
                to_tag="",
                decline_reason=RouteReason.TARGET_DISABLED.value,
            )
        if (
            target_endpoint.availability is EndpointAvailability.OFFLINE
            and target_endpoint.kind is not EndpointKind.BROWSER
        ):
            # Registrar phones persist as Devices while offline, but a
            # missing Contact cannot receive a standards SIP dialog.
            return SipInviteResult(
                480,
                "Temporarily Unavailable",
                to_tag="",
                decline_reason=RouteReason.TARGET_UNREACHABLE.value,
            )
        # A browser card is a media attachment, not the logical phone.
        # Keep an offline browser endpoint ringable so HA automations can
        # observe ringing/missed-call state and apply their own timeout or
        # forwarding policy. DND and administrative UNAVAILABLE remain
        # authoritative above.
    if not force_ha_softphone and decision.action is RouteAction.REJECT:
        if decision.reason is RouteReason.TARGET_DISABLED:
            status = 403
            sip_reason = "Forbidden"
        elif decision.reason in {
            RouteReason.TRUNK_UNAVAILABLE,
            RouteReason.TARGET_UNREACHABLE,
        }:
            status = 480
            sip_reason = "Temporarily Unavailable"
        else:
            status = 404
            sip_reason = "Not Found"
        _set_sip_bridge_call_state(
            hass,
            CallState.TRANSPORT_UNREACHABLE.value if status == 480 else "declined",
            caller=invite.caller,
            callee=invite.target,
            peer_name=invite.caller,
            call_id=invite.call_id,
            reason=decision.reason.value
            if decision.reason
            else TerminalReason.DECLINED.value,
            origin="self",
            sip_status_code=status,
            last_sip_event="SIP_RESPONSE",
        )
        return SipInviteResult(
            status,
            sip_reason,
            to_tag="",
            decline_reason=decision.reason.value
            if decision.reason
            else TerminalReason.DECLINED.value,
        )
    if (
        not force_ha_softphone
        and decision.action is RouteAction.TRUNK
        and not bridge_to_trunk
    ):
        return SipInviteResult(503, "Service Unavailable", to_tag="")
    routeable_sip_target = decision.action in {
        RouteAction.DIRECT,
        RouteAction.FORWARD,
        RouteAction.BRIDGE,
        RouteAction.ASSIST,
    } and (decision.entry is not None or bool(decision.sip_uri))
    if not force_ha_softphone and (bridge_to_trunk or routeable_sip_target):
        peer_target = _peer_for_target(decision.target or invite.target, peers)
        bridge_uri = None
        if bridge_to_trunk:
            bridge_uri = parse_sip_uri(
                f"sip:{decision.target or invite.target}@{trunk_cfg[CONF_TRUNK_SERVER]}:"
                f"{int(trunk_cfg[CONF_TRUNK_PORT])};"
                f"transport={str(trunk_cfg[CONF_TRUNK_TRANSPORT]).lower()}"
            )
        elif peer_target is not None and peer_target.host:
            sip_transport = str(
                (peer_target.device or {}).get("sip_transport") or "tcp"
            ).lower()
            if sip_transport not in {"tcp", "udp"}:
                sip_transport = "tcp"
            bridge_uri = parse_sip_uri(
                f"sip:{decision.target or invite.target}@{peer_target.host}:{peer_target.sip_port or cfg['sip_port']};transport={sip_transport}"
            )
        elif decision.entry is not None and decision.entry.sip_uri:
            bridge_uri = parse_sip_uri(decision.entry.sip_uri)
        elif (
            decision.entry is not None and not decision.entry.metadata.get("local_ha")
            and decision.entry.address
        ):
            bridge_port = int(
                decision.entry.port
                or (decision.entry.metadata or {}).get("port")
                or (decision.entry.metadata or {}).get("sip_port")
                or cfg["sip_port"]
            )
            bridge_uri = parse_sip_uri(
                f"sip:{decision.entry.id}@{decision.entry.address}:{bridge_port}"
            )
        decision_uri = bridge_uri or (
            parse_sip_uri(decision.sip_uri) if decision.sip_uri else None
        )
        if peer_target is not None and sip_endpoints_equal(
            peer_target.host,
            peer_target.sip_port,
            invite.source_host,
            invite.source_port,
            default_port=int(cfg["sip_port"]),
        ):
            _set_sip_bridge_call_state(
                hass,
                CallState.BUSY.value,
                caller=invite.caller,
                callee=invite.target,
                peer_name=invite.caller,
                call_id=invite.call_id,
                direction="incoming",
                reason=TerminalReason.BUSY.value,
                origin="self",
                sip_status_code=486,
                last_sip_event="SIP_RESPONSE",
            )
            return SipInviteResult(486, "Busy Here", to_tag="", decline_reason=TerminalReason.BUSY.value)
        points_to_local_listener = sip_uri_targets_listener(
            decision_uri,
            listener_hosts=(local_ip, "127.0.0.1", "localhost", "::1"),
            listener_port=int(cfg["sip_port"]),
            default_port=int(cfg["sip_port"]),
        )
        if decision_uri is not None and not points_to_local_listener:
            try:
                bridge_ports = RtpPortReservation.allocate(hass)
            except RuntimeError as err:
                _LOGGER.warning("SIP RTP bridge port allocation failed: %s", err)
                return SipInviteResult(503, "Service Unavailable", to_tag="")
            source_relay_port, dest_relay_port = bridge_ports.ports
            peer_target = _peer_for_target(decision.target or invite.target, peers)
            remote_tx_formats = _peer_audio_formats(
                peer_target, "tx_formats"
            ) or _roster_entry_formats(decision.entry, "tx_formats")
            remote_rx_formats = _peer_audio_formats(
                peer_target, "rx_formats"
            ) or _roster_entry_formats(decision.entry, "rx_formats")
            sip_send_formats, sip_recv_formats = _sip_target_audio_profile(
                remote_tx_formats=remote_tx_formats,
                remote_rx_formats=remote_rx_formats,
                target=decision.target or invite.target,
            )
            bridge_to_softphone = bool(
                decision.entry is not None
                and decision.entry.sip_uri
                and decision.entry.metadata.get("registered")
            )
            if bridge_to_trunk or bridge_to_softphone:
                sip_send_formats = list(HA_TRUNK_AUDIO_FORMATS)
                sip_recv_formats = list(HA_TRUNK_AUDIO_FORMATS)
            video_bridge_ports = None
            video_relay = None
            video_failure_reason = ""
            if (
                bool(cfg.get(CONF_SIP_VIDEO, False))
                and invite.video_format is not None
            ):
                sockets = ()
                try:
                    (
                        video_bridge_ports,
                        sockets,
                    ) = reserve_sip_video_relay_media(hass)
                    source_video_port, dest_video_port = video_bridge_ports.ports
                    video_relay = build_pending_invite_video_relay(
                        invite,
                        remote_host=str(decision_uri.host),
                        left_port=source_video_port,
                        right_port=dest_video_port,
                        sockets=sockets,
                        on_release=lambda ports: _release_sip_rtp_port_pair(
                            hass, ports
                        ),
                    )
                except (OSError, RuntimeError) as err:
                    for sock in sockets:
                        sock.close()
                    if video_bridge_ports is not None:
                        video_bridge_ports.release()
                    video_bridge_ports = None
                    video_relay = None
                    video_failure_reason = (
                        "local_video_resources_unavailable"
                    )
                    _LOGGER.warning(
                        "SIP video relay ports unavailable; bridge remains audio-only: %s",
                        err,
                    )
            client = SipCallClient(
                local_ip=local_ip,
                local_name=(
                    str(trunk_cfg.get(CONF_TRUNK_USERNAME) or _ha_peer_name(hass))
                    if bridge_to_trunk
                    else invite.caller or _ha_peer_name(hass)
                ),
                local_sip_port=int(cfg["sip_port"]),
                local_rtp_port=dest_relay_port,
                supported_send_formats=sip_send_formats,
                supported_recv_formats=sip_recv_formats,
                signaling_transport=_sip_uri_transport(decision_uri),
                auth_username=str(trunk_cfg.get(CONF_TRUNK_AUTH_USERNAME) or "")
                if bridge_to_trunk
                else "",
                username=str(trunk_cfg.get(CONF_TRUNK_USERNAME) or "")
                if bridge_to_trunk
                else "",
                password=str(trunk_cfg.get(CONF_TRUNK_PASSWORD) or "")
                if bridge_to_trunk
                else "",
                outbound_proxy=str(trunk_cfg.get(CONF_TRUNK_OUTBOUND_PROXY) or "")
                if bridge_to_trunk
                else "",
                include_common_codecs=bridge_to_trunk or bridge_to_softphone,
                peer_user_agent=(
                    str((decision.entry.metadata or {}).get("user_agent") or "")
                    if bridge_to_softphone and decision.entry is not None
                    else ""
                ),
                local_video_rtp_port=(
                    video_bridge_ports.ports[1] if video_bridge_ports else 0
                ),
                video_formats=(
                    (invite.video_format,) if video_bridge_ports else ()
                ),
                video_direction=(
                    invite.video_format.direction
                    if video_bridge_ports
                    else "inactive"
                ),
                generic_video_relay=bool(video_bridge_ports),
            )
            if not bridge_to_trunk:
                _enable_reused_sip_tcp_connection(
                    hass,
                    client,
                    decision_uri,
                    target=decision.target or invite.target,
                    default_sip_port=int(cfg["sip_port"]),
                )
            logical_source_endpoint = (
                source_endpoint
                if source_endpoint is not None
                and source_endpoint.kind is not EndpointKind.BROWSER
                else None
            )
            logical_target_endpoint = (
                target_endpoint
                if target_endpoint is not None
                and target_endpoint.kind is not EndpointKind.BROWSER
                else None
            )
            if logical_source_endpoint is not None or logical_target_endpoint is not None:
                registry.upsert(
                    invite.call_id,
                    state=CallState.CONNECTING.value,
                    owner="router",
                    caller=invite.caller,
                    callee=resolved_callee,
                    route_kind=decision.action.value,
                    source_endpoint_id=(
                        logical_source_endpoint.endpoint_id
                        if logical_source_endpoint is not None
                        else ""
                    ),
                    target_endpoint_id=(
                        logical_target_endpoint.endpoint_id
                        if logical_target_endpoint is not None
                        else ""
                    ),
                )
                try:
                    if logical_source_endpoint is not None:
                        registry.claim_endpoint(
                            invite.call_id,
                            logical_source_endpoint.endpoint_id,
                            role="source",
                            adopt_transport=True,
                        )
                    if logical_target_endpoint is not None:
                        registry.claim_endpoint(
                            invite.call_id,
                            logical_target_endpoint.endpoint_id,
                            role="destination",
                        )
                except EndpointBusyError:
                    await _close_client_and_release(client, bridge_ports)
                    if video_relay is not None:
                        await video_relay.stop()
                    registry.finish_and_pop(
                        invite.call_id,
                        reason=TerminalReason.BUSY.value,
                        state=CallState.BUSY.value,
                    )
                    return SipInviteResult(
                        486,
                        "Busy Here",
                        to_tag="",
                        decline_reason=TerminalReason.BUSY.value,
                    )
            try:
                result = await client.invite(
                    target=decision_uri.user,
                    remote_host=decision_uri.host,
                    remote_sip_port=decision_uri.port or int(cfg["sip_port"]),
                    request_uri=str(decision_uri),
                    timeout=SIP_TIMER_B if bridge_to_trunk else 8.0,
                )
            except Exception as err:  # noqa: BLE001 - isolate one SIP leg.
                _LOGGER.warning(
                    "SIP bridge INVITE failed call_id=%s target=%s: %s",
                    invite.call_id,
                    decision_uri.user,
                    err,
                )
                await _close_client_and_release(client, bridge_ports)
                if video_relay is not None:
                    await video_relay.stop()
                registry.finish_and_pop(
                    invite.call_id,
                    reason=TerminalReason.TRANSPORT_UNREACHABLE.value,
                    state=CallState.TRANSPORT_UNREACHABLE.value,
                )
                return SipInviteResult(
                    503,
                    "Service Unavailable",
                    to_tag="",
                    decline_reason=TerminalReason.TRANSPORT_UNREACHABLE.value,
                )
            if invite.call_id in bucket.get("trunk_closed_calls", set()):
                bucket["trunk_closed_calls"].discard(invite.call_id)
                _LOGGER.info(
                    "SIP bridge invite completed after caller cancelled call_id=%s; closing outbound leg",
                    invite.call_id,
                )
                await _close_client_and_release(client, bridge_ports, bye=True)
                if video_relay is not None:
                    await video_relay.stop()
                registry.finish_and_pop(
                    invite.call_id,
                    reason=TerminalReason.CANCELLED.value,
                    state=CallState.CANCELLED.value,
                )
                return SipInviteResult(
                    487,
                    "Request Terminated",
                    to_tag="",
                    decline_reason=TerminalReason.CANCELLED.value,
                )
            if result not in {"ringing", "in_call"}:
                status_code, sip_reason, terminal_reason, public_state = (
                    _sip_failure_response(result)
                )
                await _close_client_and_release(client, bridge_ports)
                if video_relay is not None:
                    await video_relay.stop()
                registry.finish_and_pop(
                    invite.call_id,
                    reason=terminal_reason,
                    state=public_state,
                )
                _set_sip_bridge_call_state(
                    hass,
                    public_state,
                    caller=invite.caller,
                    callee=resolved_callee,
                    peer_name=resolved_callee,
                    call_id=invite.call_id,
                    dest_call_id=client.dialog_ids.call_id,
                    direction="incoming",
                    reason=terminal_reason,
                    terminal_reason=terminal_reason,
                    origin="remote",
                    sip_status_code=status_code,
                    last_sip_event=client.last_sip_event or "SIP_RESPONSE",
                    route_kind=decision.action.value,
                    sip_uri=str(decision_uri),
                )
                return SipInviteResult(
                    status_code,
                    sip_reason,
                    to_tag="",
                    decline_reason=terminal_reason,
                )
            registry.register_bridge(
                source_call_id=invite.call_id,
                dest_call_id=client.dialog_ids.call_id,
                client=client,
                state=CallState.CONNECTING.value,
                caller=invite.caller,
                callee=resolved_callee,
                route_kind=decision.action.value,
                ingress="trunk" if trunk_invite else "extension",
                origin="trunk" if trunk_invite else "extension",
                source_state=CallState.CONNECTING.value,
                dest_state=result,
            )
            _LOGGER.info(
                "SIP bridge registered call_id=%s dest_call_id=%s target=%s",
                invite.call_id,
                client.dialog_ids.call_id,
                decision_uri.user,
            )
            if result == "ringing":
                _set_sip_bridge_call_state(
                    hass,
                    CallState.REMOTE_RINGING.value,
                    caller=invite.caller,
                    callee=resolved_callee,
                    peer_name=resolved_callee,
                    call_id=invite.call_id,
                    dest_call_id=client.dialog_ids.call_id,
                    direction="incoming",
                    route_kind=decision.action.value,
                    sip_uri=str(decision_uri),
                    sip_status_code=180,
                    last_sip_event="SIP_RESPONSE",
                )

            async def _finish_bridge(initial_result: str) -> None:
                nonlocal video_failure_reason, video_relay
                final = initial_result
                if final == "ringing":
                    final = await client.wait_for_final()
                if final != "in_call" or client.dialog is None:
                    status_code, sip_reason, terminal_reason, public_state = (
                        _sip_failure_response(final)
                    )
                    _sip_send_final_response(
                        hass,
                        invite.call_id,
                        status_code,
                        sip_reason,
                        decline_reason=terminal_reason,
                    )
                    registry.discard_bridge_session(
                        invite.call_id,
                        client.dialog_ids.call_id,
                        reason=terminal_reason,
                        state=public_state,
                    )
                    registry.take_client_watcher(client.dialog_ids.call_id)
                    await _close_client_and_release(client, bridge_ports)
                    if video_relay is not None:
                        await video_relay.stop()
                    _set_sip_bridge_call_state(
                        hass,
                        public_state,
                        caller=invite.caller,
                        callee=resolved_callee,
                        peer_name=resolved_callee,
                        call_id=invite.call_id,
                        dest_call_id=client.dialog_ids.call_id,
                        direction="incoming",
                        reason=terminal_reason,
                        terminal_reason=terminal_reason,
                        origin="remote",
                        sip_status_code=status_code,
                        last_sip_event="SIP_RESPONSE",
                        route_kind=decision.action.value,
                        sip_uri=str(decision_uri),
                    )
                    return
                selected_video = None
                selected_video_direction = "inactive"
                if video_relay is not None:
                    video_answer = configure_answered_invite_video_relay(
                        invite, client.dialog, video_relay
                    )
                    if video_answer is None:
                        _LOGGER.info(
                            "SIP bridge video rejected: destination did not accept an exact codec call_id=%s source=%s destination=%s",
                            invite.call_id,
                            invite.video_format.wire_token()
                            if invite.video_format
                            else "none",
                            client.dialog.video_format.wire_token()
                            if client.dialog.video_format
                            else "none",
                        )
                        await video_relay.stop()
                        video_relay = None
                        video_failure_reason = "remote_video_rejected"
                    else:
                        selected_video = video_answer.video_format
                        selected_video_direction = video_answer.direction
                try:
                    relay = build_invite_client_relay(
                        invite=invite,
                        client=client,
                        source_relay_port=source_relay_port,
                        dest_relay_port=dest_relay_port,
                        debug_capture=_debug_mode(hass),
                        on_release=lambda ports: _release_sip_rtp_port_pair(
                            hass, ports
                        ),
                    )
                    _attach_dtmf_event_bridge(
                        hass,
                        relay,
                        call_id=invite.call_id,
                        dest_call_id=client.dialog_ids.call_id,
                        caller=invite.caller,
                        callee=(
                            decision.entry.display_name
                            if decision.entry is not None
                            else decision.target or invite.target
                        ),
                        client=client,
                    )
                    if video_relay is not None:
                        if video_bridge_ports is not None:
                            video_bridge_ports.detach()
                        relay.attach_video_relay(video_relay)
                    await relay.start()
                except Exception as err:
                    _LOGGER.warning(
                        "SIP RTP bridge media conversion unavailable: %s", err
                    )
                    _sip_send_final_response(
                        hass,
                        invite.call_id,
                        488,
                        "Not Acceptable Here",
                        decline_reason=TerminalReason.MEDIA_INCOMPATIBLE.value,
                    )
                    registry.discard_bridge_session(
                        invite.call_id,
                        client.dialog_ids.call_id,
                        reason=TerminalReason.MEDIA_INCOMPATIBLE.value,
                        state=CallState.MEDIA_INCOMPATIBLE.value,
                    )
                    registry.take_client_watcher(client.dialog_ids.call_id)
                    await _close_client_and_release(client, bridge_ports)
                    if video_relay is not None:
                        await video_relay.stop()
                        video_relay = None
                    return
                bridge_ports.detach()
                _attach_client_media_update(
                    client,
                    relay,
                    source_call_id=invite.call_id,
                )
                registry.attach_relay(invite.call_id, relay)
                registry.upsert(
                    invite.call_id,
                    state=CallState.IN_CALL.value,
                    owner="bridge",
                    caller=invite.caller,
                    callee=resolved_callee,
                    route_kind=decision.action.value,
                )
                answer = build_answer_directional(
                    local_ip,
                    local_ip,
                    source_relay_port,
                    invite.send_format,
                    invite.recv_format,
                    dtmf=_invite_dtmf_format(invite),
                    remote_sdp=invite.remote_sdp,
                    video_port=(
                        video_relay.left_port if video_relay is not None else 0
                    ),
                    video_format=selected_video,
                    video_direction=selected_video_direction,
                )
                _sip_send_final_response(
                    hass, invite.call_id, 200, "OK", answer_sdp=answer
                )
                _set_sip_bridge_call_state(
                    hass,
                    CallState.IN_CALL.value,
                    caller=invite.caller,
                    callee=resolved_callee,
                    peer_name=resolved_callee,
                    call_id=invite.call_id,
                    dest_call_id=client.dialog_ids.call_id,
                    direction="incoming",
                    selected_tx_format=invite.send_format.audio_format.wire_token(),
                    selected_rx_format=invite.recv_format.audio_format.wire_token(),
                    selected_tx_rtp_format=invite.send_format.wire_token(),
                    selected_rx_rtp_format=invite.recv_format.wire_token(),
                    sip_status_code=200,
                    last_sip_event="SIP_RESPONSE",
                    route_kind=decision.action.value,
                    sip_uri=str(decision_uri),
                    video_active=bool(video_relay is not None),
                    video_requested=bool(invite.video_format is not None),
                    video_negotiated=bool(video_relay is not None),
                    video_status=(
                        "degraded"
                        if video_failure_reason
                        == "local_video_resources_unavailable"
                        else "rejected"
                        if video_failure_reason
                        else "active"
                        if video_relay is not None
                        else "inactive"
                    ),
                    video_failure_reason=video_failure_reason,
                    video_format=(
                        selected_video.wire_token() if selected_video else ""
                    ),
                )
                try:
                    terminal = await client.wait_for_dialog_termination()
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # noqa: BLE001 - detached bridge watcher.
                    _LOGGER.warning(
                        "SIP bridge destination watcher failed call_id=%s dest_call_id=%s: %s",
                        invite.call_id,
                        client.dialog_ids.call_id,
                        err,
                    )
                    terminal = "error"
                terminal_reason = (
                    TerminalReason.REMOTE_HANGUP.value
                    if terminal == "remote_hangup"
                    else _sip_terminal_reason(terminal, _sip_public_state(terminal))
                )
                (
                    bridge_handled,
                    source_call_id,
                    dest_call_id,
                    _client_closed,
                    source_bye,
                ) = await _terminate_sip_bridge(
                    hass,
                    client.dialog_ids.call_id,
                    terminal_reason=terminal_reason,
                )
                if bridge_handled:
                    _set_sip_bridge_call_state(
                        hass,
                        _sip_public_state(terminal),
                        caller=invite.caller,
                        callee=resolved_callee,
                        peer_name=resolved_callee,
                        call_id=source_call_id or invite.call_id,
                        dest_call_id=dest_call_id,
                        direction="incoming",
                        reason=terminal_reason,
                        terminal_reason=terminal_reason,
                        origin="remote",
                        sip_status_code=client.last_sip_status_code,
                        last_sip_event=client.last_sip_event or "BYE",
                        route_kind=decision.action.value,
                        sip_uri=str(decision_uri),
                    )
                    _LOGGER.info(
                        "SIP bridge destination ended call_id=%s dest_call_id=%s reason=%s source_bye=%s",
                        source_call_id,
                        dest_call_id,
                        terminal_reason,
                        source_bye,
                    )

            finish_task = hass.async_create_task(_finish_bridge(result))
            registry.attach_client_watcher(
                client.dialog_ids.call_id,
                finish_task,
            )
            return SipInviteResult(180, "Ringing", to_tag="", defer_final=True)
    if not force_ha_softphone and decision.action is RouteAction.ANSWER_HA:
        browser_endpoint = target_endpoint
        if browser_endpoint is None and endpoint_registry is not None:
            browser_endpoint = endpoint_registry.get(DEFAULT_ENDPOINT_ID)
        endpoint_id = (
            browser_endpoint.endpoint_id
            if browser_endpoint is not None
            else DEFAULT_ENDPOINT_ID
        )
        endpoint_device_id = str(
            getattr(browser_endpoint, "device_id", "")
            or HA_SOFTPHONE_DEVICE_ID
        )
        try:
            if (
                source_endpoint is not None
                and source_endpoint.kind is not EndpointKind.BROWSER
            ):
                registry.upsert(
                    invite.call_id,
                    state=CallState.RINGING.value,
                    owner="router",
                    caller=invite.caller,
                    callee=resolved_callee,
                    route_kind=decision.action.value,
                    source_endpoint_id=source_endpoint.endpoint_id,
                )
                registry.claim_endpoint(
                    invite.call_id,
                    source_endpoint.endpoint_id,
                    role="source",
                    adopt_transport=True,
                )
            _defer_invite_to_ha_softphone(
                invite,
                route_kind=decision.action.value,
                endpoint_id=endpoint_id,
                endpoint_device_id=endpoint_device_id,
                callee=resolved_callee,
                sip_uri=decision.sip_uri,
            )
        except EndpointBusyError:
            registry.finish_and_pop(
                invite.call_id,
                reason=TerminalReason.BUSY.value,
                state=CallState.BUSY.value,
            )
            return SipInviteResult(
                486,
                "Busy Here",
                to_tag="",
                decline_reason=TerminalReason.BUSY.value,
            )
        return SipInviteResult(180, "Ringing", to_tag="", defer_final=True)
    browser_endpoint = (
        target_endpoint
        if target_endpoint is not None
        and target_endpoint.kind is EndpointKind.BROWSER
        else (
            endpoint_registry.get(DEFAULT_ENDPOINT_ID)
            if endpoint_registry is not None
            else None
        )
    )
    endpoint_id = (
        browser_endpoint.endpoint_id
        if browser_endpoint is not None
        else DEFAULT_ENDPOINT_ID
    )
    endpoint_device_id = str(
        getattr(browser_endpoint, "device_id", "") or HA_SOFTPHONE_DEVICE_ID
    )
    if browser_endpoint is not None:
        if browser_endpoint.dnd or (
            browser_endpoint.active_call_id
            and browser_endpoint.active_call_id != invite.call_id
        ):
            return SipInviteResult(
                486,
                "Busy Here",
                to_tag="",
                decline_reason=TerminalReason.BUSY.value,
            )
        if browser_endpoint.availability is EndpointAvailability.UNAVAILABLE:
            return SipInviteResult(
                480,
                "Temporarily Unavailable",
                to_tag="",
                decline_reason=RouteReason.TARGET_UNREACHABLE.value,
            )
    registry.upsert(
        invite.call_id,
        state=CallState.CONNECTING.value,
        owner="ha_softphone",
        caller=invite.caller,
        callee=resolved_callee,
        route_kind=decision.action.value,
        endpoint_id=endpoint_id,
        session_device_id=endpoint_device_id,
        source_endpoint_id=(
            source_endpoint.endpoint_id
            if source_endpoint is not None
            and source_endpoint.kind is not EndpointKind.BROWSER
            else ""
        ),
    )
    try:
        if (
            source_endpoint is not None
            and source_endpoint.kind is not EndpointKind.BROWSER
        ):
            registry.claim_endpoint(
                invite.call_id,
                source_endpoint.endpoint_id,
                role="source",
                adopt_transport=True,
            )
        registry.claim_endpoint(
            invite.call_id,
            endpoint_id,
            role="destination",
        )
    except EndpointBusyError:
        registry.finish_and_pop(
            invite.call_id,
            reason=TerminalReason.BUSY.value,
            state=CallState.BUSY.value,
        )
        return SipInviteResult(
            486,
            "Busy Here",
            to_tag="",
            decline_reason=TerminalReason.BUSY.value,
        )
    media_reservation = None
    local_video_rtp_port = 0
    video_rtp_socket = None
    video_rtcp_socket = None
    video_failure_reason = ""
    endpoint_video_enabled = (
        browser_endpoint is None or browser_endpoint.supports("video")
    )
    if invite.video_format is not None and endpoint_video_enabled:
        try:
            (
                media_reservation,
                video_rtp_socket,
                video_rtcp_socket,
            ) = reserve_sip_video_media(hass)
            local_rtp_port, local_video_rtp_port = media_reservation.ports
        except (OSError, RuntimeError) as err:
            _LOGGER.warning(
                "SIP video socket unavailable, answering audio-only: %s", err
            )
            media_reservation = None
            video_failure_reason = "local_video_resources_unavailable"
            local_rtp_port = _allocate_sip_rtp_port(hass)
            local_video_rtp_port = 0
    else:
        local_rtp_port = _allocate_sip_rtp_port(hass)
    video_direction = (
        constrained_video_direction(
            invite.video_format.direction,
            # An automation-side answer has no browser permission or
            # per-card camera choice attached to it.  It may receive
            # video, but only the explicit answer/call actions carrying
            # send_video are allowed to advertise a camera direction.
            allow_send=False,
        )
        if invite.video_format is not None and endpoint_video_enabled
        else "inactive"
    )
    answer = build_answer_directional(
        local_ip,
        local_ip,
        local_rtp_port,
        invite.send_format,
        invite.recv_format,
        dtmf=_invite_dtmf_format(invite),
        remote_sdp=invite.remote_sdp,
        video_port=local_video_rtp_port,
        video_format=(
            invite.answer_video_format if endpoint_video_enabled else None
        ),
        video_direction=video_direction,
    )
    softphone_media = {
        "invite": invite,
        "local_rtp_port": local_rtp_port,
        "local_video_rtp_port": local_video_rtp_port,
        "video_direction": video_direction,
        "camera_send_authorized": False,
        "video_rtp_socket": video_rtp_socket,
        "video_rtcp_socket": video_rtcp_socket,
        "rtp_reservation": media_reservation,
        "endpoint_id": endpoint_id,
        "video_failure_reason": video_failure_reason,
    }
    registry.upsert(
        invite.call_id,
        state=CallState.IN_CALL.value,
        owner="ha_softphone",
        caller=invite.caller,
        callee=resolved_callee,
        route_kind=decision.action.value,
        endpoint_id=endpoint_id,
        session_device_id=endpoint_device_id,
    )
    registry.attach_media(invite.call_id, softphone_media)
    registry.add_leg(
        invite.call_id,
        invite.call_id,
        role="ha_softphone",
        state=CallState.IN_CALL.value,
    )
    video_active = bool(
        invite.video_format is not None
        and local_video_rtp_port
        and video_direction != "inactive"
    )
    _set_ha_softphone_call_state(
        hass,
        CallState.IN_CALL.value,
        endpoint_id=endpoint_id,
        session_device_id=endpoint_device_id,
        caller=invite.caller,
        callee=resolved_callee,
        peer_name=invite.caller,
        direction="incoming",
        call_id=invite.call_id,
        selected_tx_format=invite.send_format.audio_format.wire_token(),
        selected_rx_format=invite.recv_format.audio_format.wire_token(),
        selected_tx_rtp_format=invite.send_format.wire_token(),
        selected_rx_rtp_format=invite.recv_format.wire_token(),
        audio_direction=invite.local_audio_direction,
        audio_connection_held=invite.remote_audio_connection_held,
        video_active=video_active,
        video_requested=bool(invite.video_format is not None),
        video_negotiated=bool(
            invite.video_format is not None and local_video_rtp_port
        ),
        video_status=(
            "degraded"
            if video_failure_reason
            else "active"
            if video_active
            else "rejected"
            if invite.video_format is not None
            else "inactive"
        ),
        video_failure_reason=video_failure_reason,
        video_format=(
            invite.video_format.wire_token() if invite.video_format else ""
        ),
        video_send_format=(
            invite.send_video_format.wire_token()
            if invite.send_video_format is not None
            else ""
        ),
        video_receive_format=(
            invite.recv_video_format.wire_token()
            if invite.recv_video_format is not None
            else ""
        ),
        video_direction=(
            video_direction
            if invite.video_format is not None and local_video_rtp_port
            else "inactive"
        ),
        audio_mode="full_duplex",
        route_kind=decision.action.value,
        sip_uri=decision.sip_uri,
        sip_status_code=200,
        last_sip_event="SIP_RESPONSE",
    )
    return SipInviteResult(200, "OK", answer_sdp=answer, to_tag="")
