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

from .trunk_policy import EXTERNAL_CALL_BLOCKED_ENDPOINTS, external_call_denied, is_trunk_uri, trunk_available
from .core import sdp as sip_sdp
from .call_scope import pending_routes as _pending_routes
from .runtime_data import endpoint_directory, preferred_browser_phone, sip_trunk, registration_data
from .automation_routing import matches_call
from .const import (
    CONF_AUTOMATION_ROUTING_ENABLED,
    CONF_TRUNK_DTMF_ENABLED,
    CONF_TRUNK_DTMF_TIMEOUT_MS,
    CONF_TRUNK_INBOUND_DEFAULT_TARGET,
    CONF_TRUNK_INBOUND_MODE,
    TRUNK_INBOUND_MODE_DTMF,
)
from .endpoint_lifecycle import call_registry as _call_registry
from .endpoint_routing import (
    peer_for_target as _peer_for_target,
    roster_from_peers as _roster_from_peers,
    sip_target_rtp_audio_profile as _sip_target_rtp_audio_profile,
)
from .fsm import TerminalReason
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
from .inbound_routing.targets import (
    reject_route_decision,
    resolve_inbound_target,
    validate_target_endpoint,
)
from .inbound_routing.trunk import prepare_trunk_preanswer
from .pbx_routing import roster_entry_for_target as _roster_entry_for_target
from .phonebook_runtime import registered_roster_entries as _registered_roster_entries
from .router import RouteAction
from .sip_listener import SipInviteResult

if TYPE_CHECKING:
    from .sip_listener import SipInvite
    from .sip_registrar import SipRegistrar

_LOGGER = logging.getLogger(__name__)
MAX_PENDING_HA_INVITES = 64


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
    def _get_inbound_trunk_config(hass):
        return (_get_trunk_config(hass, trunk_id=invite.trunk_id)
                if invite.trunk_id else _get_trunk_config(hass))
    _trunk_enabled = runtime.trunk_enabled
    _is_trunk_invite = runtime.is_trunk_invite
    _is_ha_target = runtime.is_ha_target
    _ha_router_decision = runtime.ha_router_decision
    _inbound_route_decision = runtime.inbound_route_decision
    _async_build_peer_snapshot = runtime.build_peer_snapshot
    _defer_invite_to_ha_softphone = runtime.defer_invite_to_softphone
    peers = await _async_build_peer_snapshot(hass)
    caller_identity = invite.routing_caller
    caller_peer = _peer_for_target(caller_identity, peers)
    if caller_peer is not None and str(caller_peer.host) != str(invite.source_host):
        caller_peer = None
    caller_profile = _sip_target_rtp_audio_profile(caller_peer, None) if caller_peer is not None else None
    if caller_profile is not None:
        # Capability discovery is optional; the listener already negotiated
        # the actual offer when no additional peer constraints are known.
        send_candidates = list(caller_profile.send_formats)
        recv_candidates = list(caller_profile.recv_formats)
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
    application = _call_registry(hass).component("sip_application")
    gruu = dict(invite.request_uri.params).get("gr")
    local_instance = application is not None and gruu and gruu == application.gruu_id
    instance_contacts = (registrar.resolve_gruu(invite.request_uri)
                         if not local_instance and any(key.casefold() == "gr" for key, _value in invite.request_uri.params) else None)
    if instance_contacts is not None:
        if not instance_contacts:
            return SipInviteResult(404, "Not Found")
        invite = replace(invite, target_route=instance_contacts[0].username)
        registered_entries = registrar.registered_roster_entries(instance_uri=invite.request_uri)
    else:
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
    registry = _call_registry(hass)
    endpoint_registry = endpoint_directory(hass)
    source_endpoint_id = str(
        ((caller_roster_entry.metadata or {}).get("endpoint_id"))
        if caller_roster_entry is not None
        else ""
    ).strip()
    source_endpoint = (
        endpoint_registry.get(source_endpoint_id) if source_endpoint_id else None
    )
    identified_source = source_endpoint_id if not trunk_invite and (
        registered_source or caller_peer is not None or caller_is_known_roster_endpoint
    ) else ""
    from .esphome_origination import apply_correlated_invite, CorrelationError
    try:
        requested_trunk = apply_correlated_invite(registry, invite, identified_source)
    except CorrelationError as err:
        return SipInviteResult(err.status, "Forbidden" if err.status == 403 else "Call/Transaction Does Not Exist",
                              decline_reason=err.reason)
    if requested_trunk:
        invite = replace(invite, outbound_trunk_id=requested_trunk)
    decision = _inbound_route_decision(invite, peers, roster_entries)
    if getattr(decision, "trunk_id", ""):
        invite = replace(invite, outbound_trunk_id=decision.trunk_id)
    if not caller_is_trusted_endpoint and decision.action is RouteAction.TRUNK:
        _LOGGER.warning(
            "SIP unauthenticated trunk route rejected caller=%s source=%s:%s target=%s",
            caller_identity or "unknown",
            invite.source_host,
            invite.source_port,
            invite.routing_target,
        )
        return SipInviteResult(
            403,
            "Forbidden",
            to_tag="",
            decline_reason="unauthenticated_trunk",
        )
    if trunk_invite:
        trunk_cfg = _get_inbound_trunk_config(hass)
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
            # Keep the original called address for routing automations. Only an
            # unmatched destination uses the configured inbound fallback.
            if decision.entry is None and not _is_ha_target(invite.routing_target):
                fallback_invite = replace(
                    invite, target=default_target, target_route=default_target
                )
                decision = _inbound_route_decision(fallback_invite, peers, roster_entries)
            trunk_direct_preprocessed = True
    local_client = registry.sip_client_for(invite.call_id)
    local_session = registry.get_session(invite.call_id)
    if local_client is not None and local_session is not None and invite.source_host in {local_ip, "127.0.0.1", "::1"}:
        transport = getattr(local_client, "transport", None)
        address = transport.get_extra_info("sockname") if transport is not None else None
        if address and int(address[1]) == invite.source_port:
            identified_source = str(local_session.metadata.get("source_endpoint_id") or "")
    if identified_source:
        invite = replace(invite, identified_source_endpoint_id=identified_source)
    if invite.replaces is not None:
        from .call_replaces import prepare_replacement

        return await prepare_replacement(hass, invite, local_ip)
    route_bucket = _pending_routes(hass)
    pending = dict(registry.artifact_items("pending_invite"))
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
    if not trunk_direct_preprocessed and decision.action is RouteAction.PARKING:
        from .parking import route_parking_call
        return await route_parking_call(runtime, invite, decision, registry, source_endpoint)
    if not trunk_direct_preprocessed and decision.action is RouteAction.VOICEMAIL:
        from .voicemail import route_voicemail_call
        return await route_voicemail_call(runtime, invite, decision, registry, source_endpoint)
    if not trunk_direct_preprocessed and decision.action is RouteAction.AUTOMATION:
        from .automation_call import route_automation_call
        return await route_automation_call(runtime, invite, decision, registry, source_endpoint)
    if not trunk_direct_preprocessed and decision.action is RouteAction.ASSIST:
        called_extension = (
            str(decision.entry.extension or invite.routing_target)
            if decision.entry is not None
            else invite.routing_target
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
    if not trunk_direct_preprocessed and decision.action is RouteAction.GROUP:
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
            trunk_config=_get_inbound_trunk_config(hass),
            direct_route_preprocessed=trunk_direct_preprocessed,
            registry=registry,
        )
        if trunk_result is not None:
            return trunk_result
    native_override = any(matches_call({
        "caller": invite.caller,
        "callee": decision.target or invite.target,
        "destination": decision.entry.display_name if decision.entry is not None else invite.target,
        "called_extension": invite.routing_target if trunk_invite else (decision.entry.extension if decision.entry is not None else invite.target),
        "ingress": "trunk" if trunk_invite else "extension",
    }, options) for options in registration_data(hass).route_trigger_filters.values())
    automation_route = await request_route_override(
        runtime=runtime,
        invite=invite,
        decision=decision,
        registered_source=registered_source,
        native_override=native_override,
        caller_is_trusted_endpoint=caller_is_trusted_endpoint,
        automation_routing_enabled=native_override or bool(
            _get_inbound_trunk_config(hass).get(
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

    fallback_destination = decision.target or invite.routing_target
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

    if route_action != "answer_ha":
        target_resolution = resolve_inbound_target(
            invite=invite,
            decision=decision,
            endpoint_registry=endpoint_registry,
            roster_entries=roster_entries,
            route_resolver=_ha_router_decision,
            apply_policy=not bool(route_action in {"forward", "bridge"} and route_destination),
        )
        if target_resolution.failure is not None:
            return target_resolution.failure
        decision = target_resolution.decision
        forwarded_ids = getattr(target_resolution, "forwarded_endpoint_ids", ())
        if forwarded_ids:
            policy_session = registry.ensure_session(invite.call_id, event_only=True)
            policy_session.update_metadata(policy_endpoint_ids=tuple(sorted({
                *policy_session.metadata.get("policy_endpoint_ids", ()), *forwarded_ids,
            })))
        # Dispatch the selected destination after the automation window,
        # including a direct trunk's unchanged phonebook/fallback decision.
        if decision.action is RouteAction.PARKING:
            from .parking import route_parking_call
            return await route_parking_call(runtime, invite, decision, registry, source_endpoint)
        if decision.action is RouteAction.VOICEMAIL:
            from .voicemail import route_voicemail_call
            return await route_voicemail_call(runtime, invite, decision, registry, source_endpoint)
        if decision.action is RouteAction.AUTOMATION:
            from .automation_call import route_automation_call
            return await route_automation_call(runtime, invite, decision, registry, source_endpoint)
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

    force_ha_softphone = route_action == "answer_ha"
    if force_ha_softphone:
        target_endpoint = (
            endpoint_registry.get(automation_route.endpoint_id)
            if automation_route.endpoint_id
            else preferred_browser_phone(hass)
        )
        if target_endpoint is None:
            return SipInviteResult(480, "Temporarily Unavailable", to_tag="")
        resolved_callee = target_endpoint.name
    else:
        target_endpoint = target_resolution.endpoint
        resolved_callee = str(
            (
                decision.entry.display_name
                if decision.entry is not None
                else decision.target
            )
            or invite.target
        ).strip()

    selected_trunk_id = getattr(decision, "trunk_id", "") or invite.outbound_trunk_id
    trunk_cfg = _get_trunk_config(hass, trunk_id=selected_trunk_id) if selected_trunk_id else _get_trunk_config(hass)
    trunk = sip_trunk(hass, selected_trunk_id) if selected_trunk_id else sip_trunk(hass)
    trunk_ready = _trunk_enabled(trunk_cfg) and bool(
        trunk_available(trunk)
    )
    bridge_to_trunk = bool(
        not force_ha_softphone and decision.action is RouteAction.TRUNK and trunk_ready
    )
    if not force_ha_softphone and runtime.config.get(EXTERNAL_CALL_BLOCKED_ENDPOINTS) and external_call_denied(
        runtime.config, source_endpoint_id, session=registry.get_session(invite.call_id)
    ) and (
        decision.action is RouteAction.TRUNK
        or is_trunk_uri(decision.sip_uri, trunk_cfg, getattr(trunk, "active_registrar_target", None))
    ):
        return SipInviteResult(
            403, "Forbidden", to_tag="", decline_reason="external_calls_disabled"
        )
    if invalid_target := validate_target_endpoint(
        invite=invite,
        endpoint=target_endpoint,
    ):
        return invalid_target
    if not force_ha_softphone:
        route_session = registry.ensure_session(invite.call_id, event_only=True)
        if rejected_route := reject_route_decision(
            hass=hass,
            invite=invite,
            decision=decision,
            session=route_session,
        ):
            return rejected_route
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
            target_endpoint=target_endpoint or preferred_browser_phone(hass),
            resolved_callee=resolved_callee,
            trunk_invite=trunk_invite,
            registry=registry,
        )
        if bridge_result is not None:
            return bridge_result
    if not force_ha_softphone and decision.action is RouteAction.ANSWER_HA:
        return defer_browser_softphone_invite(
            hass=hass,
            registry=registry,
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
        invite=invite,
        decision=decision,
        resolved_callee=resolved_callee,
        source_endpoint=source_endpoint,
        target_endpoint=target_endpoint or preferred_browser_phone(hass),
        dtmf_format=sip_sdp.first_offered_dtmf_format(invite.remote_sdp),
    )
