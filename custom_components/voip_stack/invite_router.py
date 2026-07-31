"""Inbound SIP INVITE route selection and UAS orchestration.

The router chooses a destination and builds the provisional/final SIP result.
It does not own the dialog lifetime: the listener owns UAS transactions and
the PBX session owner owns calls, legs and teardown.  ``InviteRuntime`` makes
the remaining composition dependencies explicit so route-specific code cannot
quietly grow a second registry or cleanup path.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import logging
from typing import TYPE_CHECKING, Any, Callable

from homeassistant.core import HomeAssistant

from . import sdp as sip_sdp
from .call_scope import pending_routes as _pending_routes
from .const import (
    CONF_AUTOMATION_ROUTING_ENABLED,
    CONF_TRUNK_DTMF_ENABLED,
    CONF_TRUNK_DTMF_TIMEOUT_MS,
    CONF_TRUNK_INBOUND_DEFAULT_TARGET,
    CONF_TRUNK_INBOUND_MODE,
    DOMAIN,
    TRUNK_INBOUND_MODE_DTMF,
)
from .endpoint_lifecycle import call_registry as _call_registry
from .endpoint_routing import (
    peer_audio_formats as _peer_audio_formats,
    peer_for_target as _peer_for_target,
    roster_from_peers as _roster_from_peers,
    sip_target_audio_profile as _sip_target_audio_profile,
)
from .fsm import (
    CallState,
    TerminalReason,
)
from .inbound_routing.automation import (
    automation_rejection,
    request_route_override,
)
from .inbound_routing.bridge import route_sip_bridge
from .inbound_routing.local import route_local_assist, route_local_group
from .inbound_routing.softphone import (
    answer_inbound_ha_softphone,
    defer_browser_softphone_invite,
)
from .inbound_routing.trunk import prepare_trunk_preanswer
from .pbx_routing import roster_entry_for_target as _roster_entry_for_target
from .phone_endpoint import (
    EndpointAvailability,
    EndpointKind,
    OfflinePolicy,
)
from .phonebook_runtime import registered_roster_entries as _registered_roster_entries
from .router import RouteAction, RouteReason
from .sip_listener import SipInviteResult
from .websocket_api import (
    _set_sip_bridge_call_state,
)

if TYPE_CHECKING:
    from .sip_listener import SipInvite
    from .sip_registrar import SipRegistrar

_LOGGER = logging.getLogger(__name__)
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
    local_ip = runtime.local_ip
    registrar = runtime.registrar
    _get_trunk_config = runtime.get_trunk_config
    _trunk_enabled = runtime.trunk_enabled
    _is_trunk_invite = runtime.is_trunk_invite
    _is_ha_target = runtime.is_ha_target
    _ha_router_decision = runtime.ha_router_decision
    _inbound_route_decision = runtime.inbound_route_decision
    _async_build_peer_snapshot = runtime.build_peer_snapshot
    _defer_invite_to_ha_softphone = runtime.defer_invite_to_softphone
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
        invite = replace(invite, send_format=selected.send, recv_format=selected.recv)
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
    if not caller_is_trusted_endpoint and decision.action is RouteAction.TRUNK:
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
        dtmf_timeout_ms = max(0, int(trunk_cfg.get(CONF_TRUNK_DTMF_TIMEOUT_MS) or 0))
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
                str(trunk_cfg.get(CONF_TRUNK_INBOUND_DEFAULT_TARGET) or "HA").strip()
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
    if decision.action is RouteAction.ASSIST:
        called_extension = (
            str(decision.entry.extension or invite.target)
            if decision.entry is not None
            else invite.target
        )
        return await route_local_assist(
            runtime=runtime,
            invite=invite,
            decision=decision,
            roster_entries=roster_entries,
            source_endpoint=source_endpoint,
            registry=registry,
            source="sip",
            called_extension=called_extension,
        )
    if decision.action is RouteAction.GROUP:
        return await route_local_group(
            runtime=runtime,
            invite=invite,
            decision=decision,
            peers=peers,
            roster_entries=roster_entries,
            source_endpoint=source_endpoint,
            source_endpoint_id=source_endpoint_id,
            registry=registry,
        )
    if trunk_invite:
        trunk_result = prepare_trunk_preanswer(
            runtime=runtime,
            invite=invite,
            trunk_config=_get_trunk_config(hass),
            direct_route_preprocessed=trunk_direct_preprocessed,
            registry=registry,
        )
        if trunk_result is not None:
            return trunk_result
    automation_route = await request_route_override(
        runtime=runtime,
        invite=invite,
        decision=decision,
        route_bucket=route_bucket,
        registered_source=registered_source,
        caller_is_trusted_endpoint=caller_is_trusted_endpoint,
        automation_routing_enabled=bool(
            _get_trunk_config(hass).get(
                CONF_AUTOMATION_ROUTING_ENABLED,
                False,
            )
        ),
        trunk_invite=trunk_invite,
    )
    if rejected := automation_rejection(
        hass=hass,
        invite=invite,
        route=automation_route,
    ):
        return rejected
    route_action = automation_route.action
    route_destination = automation_route.destination

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
            called_extension = (
                str(decision.entry.extension or route_destination)
                if decision.entry is not None
                else route_destination
            )
            return await route_local_assist(
                runtime=runtime,
                invite=invite,
                decision=decision,
                roster_entries=roster_entries,
                source_endpoint=source_endpoint,
                registry=registry,
                source="trunk" if trunk_invite else "sip",
                called_extension=called_extension,
            )

        if decision.action is RouteAction.GROUP:
            routed_invite = replace(
                invite,
                target=decision.target or route_destination,
            )
            return await route_local_group(
                runtime=runtime,
                invite=routed_invite,
                decision=decision,
                peers=peers,
                roster_entries=roster_entries,
                source_endpoint=source_endpoint,
                source_endpoint_id=source_endpoint_id,
                registry=registry,
            )

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
            or candidate_endpoint.availability is EndpointAvailability.AVAILABLE
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
        (decision.entry.display_name if decision.entry is not None else decision.target)
        or invite.target
    ).strip()

    force_ha_softphone = route_action == "answer_ha"
    trunk_cfg = _get_trunk_config(hass)
    trunk = hass.data.get(DOMAIN, {}).get("sip_trunk")
    trunk_ready = _trunk_enabled(trunk_cfg) and bool(
        getattr(trunk, "registered", False)
    )
    bridge_to_trunk = bool(
        not force_ha_softphone and decision.action is RouteAction.TRUNK and trunk_ready
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
        bridge_result = await route_sip_bridge(
            runtime=runtime,
            invite=invite,
            decision=decision,
            peers=peers,
            trunk_config=trunk_cfg,
            bridge_to_trunk=bridge_to_trunk,
            source_endpoint=source_endpoint,
            target_endpoint=target_endpoint,
            resolved_callee=resolved_callee,
            trunk_invite=trunk_invite,
            registry=registry,
        )
        if bridge_result is not None:
            return bridge_result
    if not force_ha_softphone and decision.action is RouteAction.ANSWER_HA:
        return defer_browser_softphone_invite(
            registry=registry,
            endpoint_registry=endpoint_registry,
            invite=invite,
            decision=decision,
            resolved_callee=resolved_callee,
            source_endpoint=source_endpoint,
            target_endpoint=target_endpoint,
            defer_invite=_defer_invite_to_ha_softphone,
        )
    return answer_inbound_ha_softphone(
        hass=hass,
        local_ip=local_ip,
        registry=registry,
        endpoint_registry=endpoint_registry,
        invite=invite,
        decision=decision,
        resolved_callee=resolved_callee,
        source_endpoint=source_endpoint,
        target_endpoint=target_endpoint,
        dtmf_format=_invite_dtmf_format(invite),
    )
