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

from .trunk_policy import external_call_denied, is_trunk_uri, trunk_available
from .core import sdp as sip_sdp  # noqa: F401 - Preserve the routing adapter's public module export.
from .call_scope import pending_routes as _pending_routes
from .runtime_data import endpoint_directory, preferred_browser_phone, sip_trunk
from .automation_routing import has_native_route_window
from .const import (
    CONF_AUTOMATION_ROUTING_ENABLED,
)
from .endpoint_lifecycle import call_registry as _call_registry
from .endpoint_routing import (
    roster_from_peers as _roster_from_peers,
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
from .inbound_routing.targets import (
    reject_route_decision,
    resolve_inbound_target,
    validate_target_endpoint,
)
from .phonebook_runtime import registered_roster_entries as _registered_roster_entries
from .pbx_trace import trace_call
from .router import RouteAction, RouteDecision
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
    ring_conference_members: Callable[..., Any]
    run_ring_group_call: Callable[..., Any]
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
    trunk_invite = _is_trunk_invite(invite)
    direct_fallback_target = ""
    registry = _call_registry(hass)
    endpoint_registry = endpoint_directory(hass)
    source_endpoint_id = "" if trunk_invite else invite.identified_source_endpoint_id
    source_endpoint = endpoint_registry.get(source_endpoint_id) if source_endpoint_id else None
    registration_only = invite.authenticated_username if source_endpoint is None and not trunk_invite else ""
    if not trunk_invite and source_endpoint is None and not registration_only:
        return SipInviteResult(403, "Forbidden", decline_reason="unauthorized_caller")
    if invite.routing_target.startswith(("queue-login+", "queue-pause+")):
        from .queue_agent import route_queue_agent_call
        return await route_queue_agent_call(runtime, invite, registry,
                                            source_endpoint)
    from .trunk_selection import parse_public_trunk_destination

    requested_trunk, dial_target = (parse_public_trunk_destination(hass, invite.routing_target)
                                    if not trunk_invite else ("", invite.routing_target))
    if requested_trunk:
        invite = replace(invite, target_route=dial_target, target=dial_target,
                         outbound_trunk_id=requested_trunk)

        decision = RouteDecision(RouteAction.TRUNK, target=dial_target,
                                 trunk_id=requested_trunk, source="trunk")
    else:
        # The trusted trunk's configured DID owner already selected and
        # normalized its destination. An intentionally empty DID must not
        # fall back to the registration username in the raw Request-URI.
        decision = (_inbound_route_decision(invite, peers, roster_entries,
                                           lookup_target=invite.called_number)
                    if trunk_invite else _inbound_route_decision(invite, peers, roster_entries))
    if getattr(decision, "trunk_id", ""):
        invite = replace(invite, outbound_trunk_id=decision.trunk_id)
    if trunk_invite:
        trunk_cfg = _get_inbound_trunk_config(hass)
        from .trunk_routing import trunk_default_target

        default_target = trunk_default_target(trunk_cfg)
        # Keep the original called address for routing automations. Only an
        # unmatched destination uses the configured inbound fallback.
        if (default_target and decision.entry is None and not decision.action.is_local_application
                and not _is_ha_target(invite.called_number)):
            direct_fallback_target = default_target
            selected_target = default_target
            if _is_ha_target(default_target):
                from .roster import find_entry

                explicit = endpoint_registry.resolve(default_target)
                if explicit is None and find_entry(roster_entries, default_target) is None:
                    preferred = preferred_browser_phone(hass)
                    if preferred is not None:
                        selected_target = preferred.username or preferred.sip_uri_user
            decision = _ha_router_decision(selected_target, roster_entries)
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
    if trunk_invite:
        trace_call(
            _LOGGER, invite.call_id,
            "Trunk %s received number %s, normalized to %s from %s; routing mode=direct",
            trunk_cfg.get("name") or invite.trunk_id or "default",
            getattr(invite, "called_number_raw", "") or "not available",
            getattr(invite, "called_number", "") or "not available",
            getattr(invite, "called_number_source", "") or "none",
        )
        if direct_fallback_target:
            trace_call(
                _LOGGER, invite.call_id,
                "Trunk destination %s has no phonebook match; configured fallback is %s",
                invite.routing_target, direct_fallback_target,
            )
    native_override = has_native_route_window(hass, invite, decision, trunk_invite=trunk_invite)
    automation_route = await request_route_override(
        runtime=runtime,
        invite=invite,
        decision=decision,
        authenticated_account=bool(invite.authenticated_username) and not trunk_invite,
        native_override=native_override,
        caller_is_trusted_endpoint=bool(source_endpoint or registration_only or trunk_invite),
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
    if getattr(automation_route, "trunk_id", ""):
        from .endpoint_routing import EndpointRouteResolver

        invite = replace(invite, outbound_trunk_id=automation_route.trunk_id)
        _ha_router_decision = EndpointRouteResolver(hass, local_ip, int(runtime.config.get("sip_port") or 5060),
            source_endpoint_id=source_endpoint_id, trunk_id=automation_route.trunk_id).route
        decision = _ha_router_decision(route_destination or decision.target, roster_entries)

    if route_action in {"forward", "bridge"} and route_destination:
        decision = _ha_router_decision(route_destination, roster_entries)

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
        decision = replace(decision, ringing_timeout=automation_route.ringing_timeout)
        trace_call(
            _LOGGER, invite.call_id,
            "Target %s selects %s via %s; action=%s, reason=%s",
            route_destination or invite.routing_target,
            decision.entry.display_name if decision.entry is not None else decision.target,
            "routing action" if route_action in {"forward", "bridge"} and route_destination
            else "phone policy" if getattr(target_resolution, "forwarded_endpoint_ids", ()) else "central dialplan",
            decision.action.value, decision.reason,
        )
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
        if decision.action in {RouteAction.QUEUE, RouteAction.PAGING}:
            from .queue_call import route_queue_call
            from .paging import route_paging_call
            handler = route_queue_call if decision.action is RouteAction.QUEUE else route_paging_call
            return await handler(runtime, invite, decision, registry, source_endpoint)
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
        trace_call(_LOGGER, invite.call_id, "Routing action selects browser phone %s", resolved_callee)
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
    if not force_ha_softphone and (registration_only or external_call_denied(
        runtime.config, source_endpoint_id, session=registry.get_session(invite.call_id)
    )) and (
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
        from .account_dialing import account_dial_decision

        account_decision = account_dial_decision(hass, decision)
        if account_decision is not decision:
            return await route_local_group(
                runtime=runtime, invite=invite, decision=account_decision,
                peers=peers, roster_entries=roster_entries,
                source_endpoint=source_endpoint, source_endpoint_id=source_endpoint_id,
                registry=registry,
            )
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
            target_endpoint=target_endpoint,
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
    )
