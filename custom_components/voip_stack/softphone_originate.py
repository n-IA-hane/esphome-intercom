"""Outbound call orchestration for Home Assistant and ESP phone services."""

from __future__ import annotations

from dataclasses import dataclass, replace
import logging
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, ServiceCall

from .directory_runtime import routing_snapshot

from .trunk_policy import external_call_denied, is_any_trunk_uri, trunk_available
from .peer_media_profile import resolve_peer_media_profile
from .call_projection import publish_phone_projection
from .codec_settings import effective_codec_policy
from .counterpart_display import connected_display_name, counterpart_display
from .authorization import async_require_service_admin
from .config import (
    transport_config as _get_transport_config,
    trunk_enabled as _trunk_enabled,
)
from .const import (
    CONF_SIP_VIDEO,
    CONF_TRUNK_AUTH_USERNAME,
    CONF_TRUNK_OUTBOUND_PROXY,
    CONF_TRUNK_PASSWORD,
    CONF_TRUNK_USERNAME,
    CONF_VIDEO_CAMERA_SEND,
    HA_PEER_FALLBACK_NAME,
)
from .outbound_identity import outbound_identity
from .endpoint_lifecycle import call_registry as _call_registry, create_runtime_task
from .endpoint_termination import EndpointTerminationHandler
from .endpoint_session import TerminationInitiator, TerminationIntent
from .endpoint_registry import EndpointBusyError
from .endpoint_routing import (
    EndpointRouteResolver,
    peer_video_codec,
    sip_target_rtp_audio_profile as _sip_target_rtp_audio_profile,
)
from .esphome_actions import async_resolve_target_device as _resolve_target_device
from .fsm import (
    CallState,
    TerminalReason,
    sip_public_state as _sip_public_state,
)
from .media_ports import (
    allocate_sip_rtp_port as _allocate_sip_rtp_port,
    reserve_sip_video_media,
    bind_sip_rtp_socket,
)
from .outbound_lifecycle import (
    observe_outbound_call_result,
    attach_outbound_connected_identity_state,
    async_prepare_ha_outbound_call as _async_prepare_ha_outbound_call,
    async_track_outbound_sip_client as _track_outbound_sip_client,
)
from .peer_snapshot import async_advertise_host as _ha_advertise_host
from .phone_endpoint import (
    EndpointAvailability,
)
from .router import RouteAction, RouteReason, ha_uri_for
from .runtime_data import (
    call_runtime_artifacts,
    endpoint_directory,
    require_runtime_data,
    sip_registrar,
)
from .service_endpoints import (
    async_require_phone_service_control as _require_phone_service_control,
    browser_endpoint_name as _browser_endpoint_name,
)
from .service_errors import service_error as _service_error
from .sip_runtime import (
    enable_reused_tcp_connection as _enable_reused_sip_tcp_connection,
    uri_transport as _sip_uri_transport,
)
from .softphone_commands import (
    bind_service_call_controller as _bind_service_call_controller,
)
from .session_cleanup import async_wait_for_cleanup
from .browser_phone_data import _ha_softphone_store


if TYPE_CHECKING:
    from .core.sip import SipUri
    from .endpoint_session import EndpointCallSession
    from .pbx_runtime import SipEndpointRuntime
    from .phone_endpoint import PhoneEndpoint
    from .roster import RosterEntry
    from .router import RouteDecision
    from .sip_client import SipCallClient
    from .trunk_selection import TrunkSelection


_LOGGER = logging.getLogger(__name__)


def _local_video_receive_requested(call, endpoint) -> bool:
    requested = call.data.get("receive_video", True)
    return bool(requested and endpoint is not None and endpoint.supports("video"))


async def _mark_sip_account_unreachable(hass: HomeAssistant, username: str) -> None:
    """Drop a stale registrar Contact while keeping the account in the roster."""

    wanted = (username or "").strip().lower()
    if not wanted:
        return
    registrar = sip_registrar(hass)
    if registrar is None:
        return
    removed = False
    for key, registration in list(getattr(registrar, "registrations", {}).items()):
        reg_user = str(getattr(registration, "username", key) or key).strip().lower()
        if reg_user == wanted or str(key).strip().lower() == wanted:
            removed = True
    if removed:
        # Go through the registrar API so its observer also marks the logical
        # endpoint offline. Directly popping Contact state left HA's connected
        # binary sensor stale even though routing already returned 480.
        registrar.remove_registration(username)
        _LOGGER.info("SIP registrar contact marked unreachable user=%s", username)


def _ha_peer_name(hass: HomeAssistant) -> str:
    """Return the HA phonebook peer name.

    HA normally always has a configured location_name. The default is only for
    malformed/empty local config and avoids a hardcoded "Home Assistant" peer
    identity.
    """
    return (hass.config.location_name or "").strip() or HA_PEER_FALLBACK_NAME


async def _async_resolve_browser_destination(
    hass: HomeAssistant,
    *,
    route,
    target: str,
    source_endpoint_id: str,
):
    """Resolve a logical browser phone independently from card presence."""
    if route.action is not RouteAction.ANSWER_HA or route.entry is None:
        return route, target, None
    endpoint_id = str((route.entry.metadata or {}).get("endpoint_id") or "").strip()
    endpoint = endpoint_directory(hass).get(endpoint_id) if endpoint_id else None
    if endpoint is not None and not endpoint.kind.is_softphone:
        from .phone_endpoint import ha_client_endpoint
        endpoint = ha_client_endpoint(endpoint)
    if endpoint is None:
        return route, target, None
    if endpoint.endpoint_id == source_endpoint_id:
        raise _service_error(
            "a Home Assistant phone cannot call itself",
            "phone_self_target",
        )
    if endpoint.dnd or endpoint.active_call_id:
        raise _service_error(
            f"{endpoint.name} is busy",
            "phone_busy",
            phone=endpoint.name,
        )
    if endpoint.availability is EndpointAvailability.UNAVAILABLE:
        raise _service_error(
            f"{endpoint.name} is disabled",
            "phone_disabled",
            phone=endpoint.name,
        )

    # OFFLINE means that no browser currently owns media. The configured
    # logical phone still rings so automations can observe or reroute it, and
    # a card that connects during ringing can take over the existing call.
    return route, target, endpoint


def _logical_endpoint_for_route(hass: HomeAssistant, route):
    """Return the configured logical endpoint carried by a roster route."""
    entry = getattr(route, "entry", None)
    endpoint_id = str(
        ((getattr(entry, "metadata", None) or {}).get("endpoint_id")) or ""
    ).strip()
    return endpoint_directory(hass).get(endpoint_id) if endpoint_id else None


@dataclass(frozen=True, slots=True)
class _BrowserCallResolution:
    """Resolved routing facts; the existing registry retains any pre-call owner."""

    target: str
    dialed_target: str
    local_name: str
    source_device_id: str
    config: dict
    contacts: list[RosterEntry]
    destination_device: dict | None
    trunk_selection: TrunkSelection
    trunk_ready: bool
    route: RouteDecision
    browser_destination: PhoneEndpoint | None
    routing_call_id: str | None
    policy_endpoint_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _BrowserSipTarget:
    """A SIP destination after local bridges, groups and pickup have been dispatched."""

    resolution: _BrowserCallResolution
    route: RouteDecision
    endpoint: PhoneEndpoint | None
    local_ip: str
    route_uri: str
    use_trunk: bool


@dataclass(frozen=True, slots=True)
class _PreparedBrowserSipCall:
    """References to the one client and session registered by SIP preparation."""

    target: _BrowserSipTarget
    client: SipCallClient
    registry: SipEndpointRuntime
    session: EndpointCallSession
    uri: SipUri
    local_rtp_port: int
    target_device_id: str
    video_enabled: bool
    video_failure_reason: str


async def _async_resolve_browser_call_route(
    call: ServiceCall, *, endpoint_id: str, browser_endpoint,
    routing_scope: dict,
) -> _BrowserCallResolution:
    """Authorize the caller and resolve native automation, forwarding and policy."""
    hass: HomeAssistant = call.hass
    dest_device = await _resolve_target_device(hass, call)
    target = str(call.data.get("destination") or "").strip()
    if not target and dest_device is not None:
        target = str(dest_device.get("name") or "").strip()
    if not target:
        raise _service_error("destination is required", "destination_required")
    await _require_phone_service_control(
        hass,
        call,
        endpoint=browser_endpoint,
    )
    if (
        browser_endpoint is not None
        and browser_endpoint.availability is EndpointAvailability.UNAVAILABLE
    ):
        raise _service_error(
            f"{browser_endpoint.name} is disabled",
            "phone_disabled",
            phone=browser_endpoint.name,
        )
    local_name = _browser_endpoint_name(hass, endpoint_id, browser_endpoint)
    source_device_id = str(getattr(browser_endpoint, "device_id", ""))
    _ha_softphone_store(hass, endpoint_id)["device_id"] = source_device_id
    cfg = _get_transport_config(hass)
    from .trunk_selection import select_call_trunk

    trunk_selection = select_call_trunk(hass, requested=str(call.data.get("trunk_id") or ""), endpoint_id=endpoint_id)
    trunk = trunk_selection.client
    trunk_cfg = trunk_selection.config
    trunk_ready = _trunk_enabled(trunk_cfg) and bool(
        trunk_available(trunk)
    )
    from .sip_capabilities import wait_for_target_capabilities
    await wait_for_target_capabilities(hass, target)
    try:
        contacts = routing_snapshot(require_runtime_data(hass))
    except ValueError as error:
        raise _service_error("Phone directory is unavailable", "phone_unavailable", phone=target) from error
    resolve_route = EndpointRouteResolver(hass, str(cfg.get("advertise_host") or ""),
        int(cfg.get("sip_port") or 5060), source_endpoint_id=endpoint_id,
        trunk_id=trunk_selection.identity).route
    route = resolve_route(target, contacts)
    dialed_target = target
    routing_call_id = None
    explicit_route = False
    from .automation_routing import RouteRequest, has_native_route_window

    identity = RouteRequest("", local_name, target,
        source_account_username=str(getattr(browser_endpoint, "username", "") or ""),
        source_extension=str(getattr(browser_endpoint, "extension", "") or ""),
        caller_number=str(getattr(browser_endpoint, "extension", "") or getattr(browser_endpoint, "username", "") or local_name),
        caller_name=local_name, called_number=str(getattr(route.entry, "number", "") or target),
        outbound_trunk_id=trunk_selection.identity)
    if has_native_route_window(hass, identity, route):
        from .core.sip import make_call_id

        identity = replace(identity, call_id=make_call_id("ha"))
        from types import SimpleNamespace
        from .inbound_routing.automation import request_route_override

        if getattr(browser_endpoint, "active_call_id", ""):
            raise _service_error("this phone already has an active call", "phone_busy", phone=local_name)
        registry = _call_registry(hass)
        session = registry.upsert(identity.call_id, state=CallState.CONNECTING.value, owner="router",
            caller=local_name, callee=target, route_kind=route.action.value,
            endpoint_id=endpoint_id, source_endpoint_id=endpoint_id, session_device_id=source_device_id,
            caller_number=identity.caller_number, caller_name=local_name, called_number=identity.called_number,
            direction="outgoing", ingress="extension", dialed_target=dialed_target,
            outbound_trunk_id=trunk_selection.identity)
        routing_scope["session"] = session
        _bind_service_call_controller(registry, session.call_id, call)
        override = await request_route_override(runtime=SimpleNamespace(hass=hass, ha_peer_name=lambda _: local_name),
            invite=identity, decision=route, authenticated_account=True, caller_is_trusted_endpoint=True,
            automation_routing_enabled=True, trunk_invite=False, native_override=True)
        if override.action in {"decline", "busy", "cancel"}:
            raise _service_error("The routing automation declined this call", "phone_unavailable", phone=target)
        if override.trunk_id:
            trunk_selection = select_call_trunk(hass, requested=override.trunk_id, endpoint_id=endpoint_id)
            trunk, trunk_cfg = trunk_selection.client, trunk_selection.config
            trunk_ready = _trunk_enabled(trunk_cfg) and bool(trunk_available(trunk))
            resolve_route = EndpointRouteResolver(hass, str(cfg.get("advertise_host") or ""),
                int(cfg.get("sip_port") or 5060), source_endpoint_id=endpoint_id,
                trunk_id=trunk_selection.identity).route
        if override.action in {"forward", "bridge"} and override.destination:
            target = override.destination
            explicit_route = True
        elif override.action == "answer_ha" and override.endpoint_id:
            selected = endpoint_directory(hass).get(override.endpoint_id)
            if selected is None:
                raise _service_error("The selected phone is unavailable", "phone_unavailable", phone=target)
            target = selected.username or selected.sip_uri_user
            explicit_route = True
        route = replace(resolve_route(target, contacts), ringing_timeout=override.ringing_timeout)
        routing_call_id = session.call_id
    from .inbound_routing.targets import resolve_phone_target

    resolved = resolve_phone_target(
        decision=route,
        endpoint_registry=endpoint_directory(hass),
        roster_entries=contacts,
        route_resolver=resolve_route,
        call_id=routing_call_id or "", apply_policy=not explicit_route,
    )
    if resolved.failure is not None:
        raise _service_error(
            f"Phone routing failed: {resolved.failure.decline_reason}",
            "phone_unavailable",
            phone=target,
        )
    if resolved.decision != route:
        target = resolved.decision.target
    route = replace(resolved.decision, ringing_timeout=route.ringing_timeout)
    if route.reason is RouteReason.TRUNK_UNAVAILABLE:
        raise trunk_selection.unavailable_error()
    policy_endpoint_ids = getattr(resolved, "forwarded_endpoint_ids", ())
    route, target, browser_destination = await _async_resolve_browser_destination(
        hass,
        route=route,
        target=target,
        source_endpoint_id=endpoint_id,
    )
    if external_call_denied(cfg, endpoint_id, *policy_endpoint_ids) and (
        route.action is RouteAction.TRUNK
        or is_any_trunk_uri(require_runtime_data(hass), route.sip_uri)
    ):
        raise _service_error("External calls are disabled for this phone", "external_calls_disabled")
    return _BrowserCallResolution(
        target=target, dialed_target=dialed_target, local_name=local_name,
        source_device_id=source_device_id, config=cfg, contacts=contacts,
        destination_device=dest_device, trunk_selection=trunk_selection,
        trunk_ready=trunk_ready, route=route, browser_destination=browser_destination,
        routing_call_id=routing_call_id, policy_endpoint_ids=policy_endpoint_ids,
    )


async def _async_dispatch_browser_call_route(
    call: ServiceCall, resolved: _BrowserCallResolution, *, endpoint_id: str,
    browser_endpoint, force_ha_bridge: bool,
) -> _BrowserSipTarget | None:
    """Start local destinations, or return the exact selected SIP route."""
    from .core.sip import SipUri

    hass = call.hass
    cfg, contacts = resolved.config, resolved.contacts
    route, target = resolved.route, resolved.target
    local_name, source_device_id = resolved.local_name, resolved.source_device_id
    trunk_selection, trunk_ready = resolved.trunk_selection, resolved.trunk_ready
    trunk_cfg = trunk_selection.config
    routing_call_id, policy_endpoint_ids = resolved.routing_call_id, resolved.policy_endpoint_ids
    browser_destination = resolved.browser_destination
    if browser_destination is not None:
        from .local_softphone_bridge import LocalBridgeError
        from .local_softphone_runtime import start_local_softphone_call

        await _async_prepare_ha_outbound_call(hass, endpoint_id)
        try:
            snapshot = start_local_softphone_call(
                hass,
                endpoint_id,
                browser_destination.endpoint_id,
                request_video=_local_video_receive_requested(call, browser_endpoint),
                enable_caller_video_send=bool(
                    cfg.get(CONF_VIDEO_CAMERA_SEND, False)
                    and call.data.get("send_video", False)
                ),
                caller_owner_id=str(call.data.get("media_client_id") or ""),
                context=getattr(call, "context", None),
                **({"call_id": routing_call_id} if routing_call_id else {}),
                **({"policy_endpoint_ids": policy_endpoint_ids} if policy_endpoint_ids else {}),
                **({"outbound_trunk_id": trunk_selection.identity} if trunk_selection.identity else {}),
            )
        except LocalBridgeError as err:
            raise _service_error(
                str(err),
                "local_phone_operation_failed",
            ) from err
        _LOGGER.info(
            "HA local phone call started call_id=%s source=%s destination=%s video=%s",
            snapshot.call_id,
            endpoint_id,
            browser_destination.endpoint_id,
            snapshot.video_enabled,
        )
        return
    target_endpoint = _logical_endpoint_for_route(hass, route)
    if target_endpoint is not None and not target_endpoint.kind.is_softphone:
        if target_endpoint.dnd or target_endpoint.active_call_id:
            raise _service_error(
                f"{target_endpoint.name} is busy",
                "phone_busy",
                phone=target_endpoint.name,
            )
        if target_endpoint.availability is not EndpointAvailability.AVAILABLE:
            raise _service_error(
                f"{target_endpoint.name} is unavailable",
                "phone_unavailable",
                phone=target_endpoint.name,
            )
    # Browser-to-browser calls use the in-memory logical bridge and must not
    # depend on SIP network discovery.  Resolve the advertised address only
    # after that path has been exhausted, for conference or external SIP/RTP.
    local_ip = await _ha_advertise_host(hass)
    if not local_ip:
        raise _service_error(
            "HA advertise IP is unknown",
            "advertise_ip_unknown",
        )
    if route.reason is RouteReason.DIRECT_URI:
        # Entity CONTROL permits ordinary phone operation, but an ad-hoc SIP
        # URI also chooses an arbitrary network host/port.  Keep that SSRF and
        # port-probing capability behind HA administrator authentication;
        # roster contacts, registered endpoints and trunk numbers are still
        # available to normal controllers.
        await async_require_service_admin(hass, call)
    if route.action is RouteAction.PARKING and route.entry.metadata.get("parking_mode") in {"directed_pickup", "pickup"}:
        from .call_pickup import pickup_browser
        from .parking import pickup_browser_call

        registry = _call_registry(hass)
        preliminary = registry.get_session(routing_call_id or "")
        token = preliminary.token if preliminary is not None else None
        await _async_prepare_ha_outbound_call(hass, endpoint_id)
        pickup = pickup_browser if route.entry.metadata["parking_mode"] == "directed_pickup" else pickup_browser_call
        recovered = await pickup(call, browser_endpoint, route.entry.metadata["parking_slot"])
        # Pickup adopts the retained caller's session. Its preliminary native
        # route window must not survive as an empty, separately owned call.
        if token is not None and recovered.call_id != token.call_id:
            barrier = registry.request_termination(token.call_id,
                TerminationIntent(TerminalReason.FORWARDED.value, initiator=TerminationInitiator.ROUTING),
                generation=token.generation)
            if barrier is not None:
                await async_wait_for_cleanup(barrier)
        return
    if (
        force_ha_bridge or bool(call.data.get("ha_bridge", False))
    ) and route.action not in {
        RouteAction.ANSWER_HA,
        RouteAction.TRUNK,
        RouteAction.REJECT,
    }:
        if route.entry is not None and route.entry.metadata.get("registered"):
            bridge_uri = route.sip_uri
        else:
            bridge_uri = ha_uri_for(route.target or target, contacts)
        route = replace(route, action=RouteAction.BRIDGE, sip_uri=bridge_uri)
    use_trunk = route.action is RouteAction.TRUNK and trunk_ready
    if route.action is RouteAction.TRUNK and not use_trunk:
        raise trunk_selection.unavailable_error()
    from .account_dialing import account_dial_decision
    route = account_dial_decision(hass, route)
    if route.action is RouteAction.GROUP and route.entry is not None:
        group_type = str((route.entry.metadata or {}).get("group_type") or "")
        if group_type == "ring":
            start_ring_group = call_runtime_artifacts(hass).start_ring_group_from_ha
            if start_ring_group is None:
                raise _service_error(
                    f"{target} is not available yet",
                    "destination_not_ready",
                    destination=target,
                )
            await _async_prepare_ha_outbound_call(hass, endpoint_id)
            await start_ring_group(
                route.entry,
                ringing_timeout=route.ringing_timeout,
                **({"policy_endpoint_ids": policy_endpoint_ids} if policy_endpoint_ids else {}),
                **({"outbound_trunk_id": trunk_selection.identity} if trunk_selection.identity else {}),
                context=getattr(call, "context", None),
                endpoint_id=endpoint_id,
                media_client_id=str(call.data.get("media_client_id") or ""),
                **({"call_id": routing_call_id} if routing_call_id else {}),
                request_video=_local_video_receive_requested(call, browser_endpoint),
                enable_caller_video_send=bool(
                    cfg.get(CONF_VIDEO_CAMERA_SEND, False)
                    and call.data.get("send_video", False)
                ),
            )
            _LOGGER.info("HA softphone started ring group=%s", route.entry.display_name)
            return
        if group_type == "conference":
            room_name = route.entry.name or route.entry.id or target
            from .conference import conference_manager

            await _async_prepare_ha_outbound_call(hass, endpoint_id)
            manager = conference_manager(hass, local_ip=local_ip)
            joined = manager.start_ha_softphone(
                room_name,
                endpoint_id=endpoint_id,
                **({"call_id": routing_call_id} if routing_call_id else {}),
            )
            if joined is None:
                raise _service_error(
                    f"Conference {room_name} is full",
                    "conference_full",
                    conference=room_name,
                )
            call_id, queue = joined
            registry = _call_registry(hass)
            conference_media = {
                "conference_room": room_name,
                "conference_queue": queue,
                "endpoint_id": endpoint_id,
                "media_client_id": str(call.data.get("media_client_id") or ""),
            }
            session = registry.upsert(
                call_id,
                state=CallState.IN_CALL.value,
                owner="ha_softphone",
                caller=local_name,
                callee=room_name,
                route_kind="conference",
                endpoint_id=endpoint_id,
                source_endpoint_id=endpoint_id,
                policy_endpoint_ids=policy_endpoint_ids,
                outbound_trunk_id=trunk_selection.identity,
                session_device_id=source_device_id,
                media_client_id=str(call.data.get("media_client_id") or ""),
            )
            registry.attach_media(call_id, conference_media)
            _bind_service_call_controller(registry, call_id, call)
            registry.add_leg(
                call_id, call_id, role="ha_softphone", state=CallState.IN_CALL.value
            )
            publish_phone_projection(
                hass,
                session,
                endpoint_id, peer_name=room_name, direction="outgoing",
                    route_kind="conference", sip_status_code=200,
                    last_sip_event="LOCAL_CONFERENCE_JOIN",
                    selected_tx_format="16000:s16le:1:20",
                    selected_rx_format="16000:s16le:1:20",
                    selected_tx_rtp_format="pt=96:L16/16000/1/20ms",
                    selected_rx_rtp_format="pt=96:L16/16000/1/20ms",
                    scope="conference", room=room_name, target=target,
            )
            ring_members = call_runtime_artifacts(
                hass
            ).ring_conference_members_from_ha
            if ring_members is not None:
                create_runtime_task(
                    hass,
                    ring_members(route.entry, owner_call_id=call_id),
                )
            _LOGGER.info("HA softphone joined conference room=%s", room_name)
            return
    route_uri = route.sip_uri
    if route.action is RouteAction.GROUP:
        route_uri = ha_uri_for(route.target or target, contacts)
    elif route.action.is_local_application:
        # Use the existing SIP adapter for every PBX-local application.
        # Originate the browser media leg
        # to this integration's listener so the canonical inbound dispatcher
        # owns the application session and RTP pipeline; never fall through to the
        # external-trunk resolver merely because the roster entry has no
        # network address of its own.
        route_uri = str(SipUri(route.target or target, local_ip, int(cfg["sip_port"])))
        route = replace(route, action=RouteAction.BRIDGE, sip_uri=route_uri)
    if use_trunk:
        trunk_target = route.target or target
        from .trunk_selection import trunk_destination_uri
        route_uri = str(trunk_destination_uri(trunk_cfg, trunk_target))
    if not use_trunk and (
        route.action
        not in {
            RouteAction.DIRECT,
            RouteAction.FORWARD,
            RouteAction.BRIDGE,
            RouteAction.GROUP,
        }
        or not route_uri
    ):
        raise _service_error(
            f"cannot resolve SIP target: {target}",
            "sip_target_unresolved",
            destination=target,
        )
    return _BrowserSipTarget(resolution=resolved, route=route, endpoint=target_endpoint,
        local_ip=local_ip, route_uri=route_uri, use_trunk=use_trunk)


async def _async_prepare_browser_sip_call(
    call: ServiceCall, destination: _BrowserSipTarget, *, endpoint_id: str,
    browser_endpoint,
) -> _PreparedBrowserSipCall:
    """Prepare media and register the existing SIP client, session and endpoint claims."""
    from .core.sip import parse_sip_uri
    from .sip_client import SipCallClient

    hass = call.hass
    resolved = destination.resolution
    cfg = resolved.config
    route, route_uri = destination.route, destination.route_uri
    target, dialed_target = resolved.target, resolved.dialed_target
    target_endpoint, local_ip = destination.endpoint, destination.local_ip
    local_name, source_device_id = resolved.local_name, resolved.source_device_id
    trunk_selection, use_trunk = resolved.trunk_selection, destination.use_trunk
    trunk_cfg, trunk = trunk_selection.config, trunk_selection.client
    routing_call_id, policy_endpoint_ids = resolved.routing_call_id, resolved.policy_endpoint_ids
    await _async_prepare_ha_outbound_call(hass, endpoint_id)
    uri = parse_sip_uri(route_uri)
    entry_metadata = dict(route.entry.metadata or {}) if route.entry is not None else {}
    peer_profile = resolve_peer_media_profile(entry_metadata)
    codec_policy = effective_codec_policy(hass, endpoint_id=str(getattr(target_endpoint, "endpoint_id", "") or ""),
                                          fallback_endpoint_id=endpoint_id,
                                          trunk_id=trunk_selection.identity if use_trunk else None)
    rtp_audio_profile = _sip_target_rtp_audio_profile(
        route.entry,
        codec_policy=codec_policy,
        trunk=use_trunk,
    )
    sip_send_formats = list(rtp_audio_profile.send_formats)
    sip_recv_formats = list(rtp_audio_profile.recv_formats)
    target_device_id = str(
        getattr(target_endpoint, "device_id", "")
        or entry_metadata.get("device_id")
        or ""
    ).strip()
    # Unknown remote capabilities are negotiated by the same video owner
    # used for group and forwarded legs; explicit audio-only peers decline.
    source_video_enabled = browser_endpoint is None or browser_endpoint.supports("video")
    target_video_contract = peer_video_codec(None, route.entry)
    target_video_enabled = target_video_contract != ""
    video_capable = (
        bool(cfg.get(CONF_SIP_VIDEO, False))
        and codec_policy.video != ()
        and (not use_trunk or bool(trunk_cfg.get("trunk_video_codecs", ["H264"])))
        and source_video_enabled
        and target_video_enabled
    )
    camera_send_enabled = bool(
        video_capable
        and cfg.get(CONF_VIDEO_CAMERA_SEND, False)
        and call.data.get("send_video", False)
    )
    receive_video_enabled = bool(video_capable and
        call.data.get("receive_video", True))
    target_video_codec = str(
        target_video_contract
        or ""
    ).strip()
    from .core.sdp import plan_browser_video_offer

    offered_video_formats = plan_browser_video_offer(target_video_codec,
        camera_send=camera_send_enabled, receive=receive_video_enabled,
        peer_profile=peer_profile, trunk=use_trunk, codec_policy=codec_policy)
    video_enabled = bool(offered_video_formats)
    video_direction = ("sendrecv" if receive_video_enabled and camera_send_enabled else
                       "sendonly" if camera_send_enabled else "recvonly" if receive_video_enabled else "inactive")
    video_reservation = None
    video_rtp_socket = None
    video_rtcp_socket = None
    video_failure_reason = ""
    if video_enabled:
        try:
            (
                video_reservation,
                video_rtp_socket,
                video_rtcp_socket,
            ) = reserve_sip_video_media(hass)
            local_rtp_port, local_video_rtp_port = video_reservation.ports
        except (OSError, RuntimeError) as err:
            _LOGGER.warning(
                "SIP video socket unavailable, originating audio-only: %s", err
            )
            video_reservation = None
            video_failure_reason = "local_video_resources_unavailable"
            local_rtp_port = _allocate_sip_rtp_port(hass)
            local_video_rtp_port = 0
    else:
        local_rtp_port = _allocate_sip_rtp_port(hass)
        local_video_rtp_port = 0
    caller_user = browser_endpoint.sip_uri_user if browser_endpoint is not None else local_name
    identity = outbound_identity(
        hass, trunk_cfg, endpoint=browser_endpoint, caller_user=caller_user, caller_name=local_name,
    ) if use_trunk else None
    # Preserve the engine's complete RTP catalog through the SIP writer.
    client = SipCallClient(
        audio_rtcp_min_interval=float(cfg["audio_rtcp_min_interval"]),
        **({"call_id": routing_call_id} if routing_call_id else {}),
        codec_policy=codec_policy,
        local_ip=local_ip,
        local_name=identity.name if identity is not None else local_name,
        local_uri_user=(
            str(trunk_cfg.get(CONF_TRUNK_USERNAME) or caller_user)
            if use_trunk
            else (
                browser_endpoint.sip_uri_user
                if browser_endpoint is not None
                else local_name
            )
        ),
        local_identity_uri=identity.uri if identity is not None else "",
        fixed_local_identity=identity.fixed if identity is not None else False,
        local_sip_port=int(cfg["sip_port"]),
        local_rtp_port=local_rtp_port,
        supported_send_formats=sip_send_formats,
        supported_recv_formats=sip_recv_formats,
        supported_send_rtp_formats=rtp_audio_profile.send_rtp_formats,
        supported_recv_rtp_formats=rtp_audio_profile.recv_rtp_formats,
        signaling_transport=_sip_uri_transport(uri),
        auth_username=str(trunk_cfg.get(CONF_TRUNK_AUTH_USERNAME) or ""),
        username=str(trunk_cfg.get(CONF_TRUNK_USERNAME) or ""),
        password=str(trunk_cfg.get(CONF_TRUNK_PASSWORD) or "") if use_trunk else "",
        outbound_proxy=str(trunk_cfg.get(CONF_TRUNK_OUTBOUND_PROXY) or "")
        if use_trunk
        else "",
        include_dahua_pcm=rtp_audio_profile.include_dahua_pcm,
        match_received_codec=rtp_audio_profile.match_received_codec,
        include_common_codecs=False,
        peer_user_agent=str(entry_metadata.get("user_agent") or ""),
        local_video_rtp_port=local_video_rtp_port,
        video_formats=offered_video_formats if video_enabled else (),
        video_direction=video_direction,
        media_reservation=video_reservation,
        video_rtp_socket=video_rtp_socket,
        video_rtcp_socket=video_rtcp_socket,
    )

    from .browser_dialog_updates import attach_browser_dialog_updates
    attach_browser_dialog_updates(hass, client, endpoint_id, source_device_id)
    from .sip_runtime import configure_local_contact
    configure_local_contact(hass, client)
    if use_trunk and trunk is not None:
        from .trunk_signaling import reuse_registered_trunk_flow

        if reuse_registered_trunk_flow(trunk, client):
            _LOGGER.info("SIP trunk registered UDP flow reused for outbound call")
    else:
        from .sip_runtime import select_registered_outbound_flow
        _enable_reused_sip_tcp_connection(
            hass,
            client,
            uri,
            target=target,
            default_sip_port=int(cfg["sip_port"]),
            selected_flow=select_registered_outbound_flow(hass, uri, entry=route.entry),
        )
    registry = _call_registry(hass)
    session = registry.upsert(
        client.dialog_ids.call_id,
        state=CallState.CALLING.value,
        owner="ha_softphone",
        caller=local_name,
        callee=dialed_target,
        route_kind="direct",
        direction="outgoing",
        endpoint_id=endpoint_id,
        source_endpoint_id=endpoint_id,
        policy_endpoint_ids=policy_endpoint_ids,
        dialed_target=dialed_target,
        session_device_id=source_device_id,
        outbound_trunk_id=trunk_selection.identity,
        media_client_id=str(call.data.get("media_client_id") or ""),
        source_media_client_id=str(call.data.get("media_client_id") or ""),
        target_endpoint_id=(
            target_endpoint.endpoint_id if target_endpoint is not None else ""
        ),
    )
    from .dtmf_events import attach_direct_client_dtmf_events
    from .browser_playback import BrowserPlayback
    from .endpoint_session import CleanupStage

    async def prepare_browser_provisional_media(candidate):
        # Provisional SDP belongs to an early branch, never client.dialog.
        current = registry.get_session(session.call_id)
        if (current is not session or not current.live or current.owner != "ha_softphone"
                or registry.sip_client_for(session.call_id) is not client
                or client.dialog is not None
                or client.early_dialogs.get(candidate.remote_tag) is not candidate):
            return None
        selected = current.metadata.get("browser_early_media_tag")
        if selected and selected != candidate.remote_tag:
            return None
        from .sip_client import PreparedDialogMediaUpdate
        async def commit():
            if not registry.is_generation_current(session.call_id, session.generation):
                raise RuntimeError("provisional media belongs to a terminated browser call")
            from .audio_rtcp import async_prepare_browser_rtcp
            await async_prepare_browser_rtcp(hass, current, candidate, client=client)
            current.update_metadata(browser_early_media_tag=candidate.remote_tag)
            audio = require_runtime_data(hass).media.sessions_for("audio").get((session.call_id, endpoint_id))
            if audio is not None:
                from .media_session_updates import commit_audio_session_update
                commit_audio_session_update(audio, candidate,
                    dtmf_payload_type=candidate.dtmf_payload_type, dtmf_events=candidate.dtmf_events)
            publish_phone_projection(hass, current, endpoint_id,
                direction="outgoing", early_media=True,
                audio_direction=candidate.local_audio_direction,
                sip_status_code=client.last_sip_status_code, last_sip_event="SIP_RESPONSE")
        async def rollback():
            return None
        return PreparedDialogMediaUpdate(commit=commit, rollback=rollback)

    client.on_provisional_media = prepare_browser_provisional_media

    playback = BrowserPlayback()
    registry.own_resource(
        session.call_id, f"browser_playback:{session.call_id}", playback,
        playback.close, stage=CleanupStage.OBSERVER, generation=session.generation,
    )

    attach_direct_client_dtmf_events(
        hass,
        client,
        call_id=client.dialog_ids.call_id,
        caller=local_name,
        callee=dialed_target,
    )
    attach_outbound_connected_identity_state(
        hass,
        client,
        endpoint_id=endpoint_id,
    )
    try:
        registry.claim_endpoint(
            client.dialog_ids.call_id,
            endpoint_id,
            role="source",
        )
        if (
            target_endpoint is not None
            and not target_endpoint.kind.is_softphone
        ):
            registry.claim_endpoint(
                client.dialog_ids.call_id,
                target_endpoint.endpoint_id,
                role="destination",
            )
    except EndpointBusyError as err:
        await EndpointTerminationHandler(hass).terminate_reason(
            client.dialog_ids.call_id,
            TerminalReason.BUSY.value,
            TerminationInitiator.RUNTIME,
        )
        await client.close()
        raise _service_error(
            str(err),
            "phone_busy",
            phone=str(getattr(target_endpoint, "name", "") or dialed_target),
        ) from err
    _bind_service_call_controller(registry, client.dialog_ids.call_id, call)
    publish_phone_projection(
        hass,
        session,
        endpoint_id, peer_name=dialed_target, direction="outgoing",
            target_device_id=target_device_id,
            sip_transport=_sip_uri_transport(uri).lower(), last_sip_event="INVITE",
            sip_uri=route_uri, video_requested=video_enabled,
            video_negotiated=False,
            video_status="degraded" if video_failure_reason else "requested"
            if video_enabled else "inactive",
            video_failure_reason=video_failure_reason,
    )
    registry.attach_sip_client(
        client.dialog_ids.call_id,
        client.dialog_ids.call_id,
        client,
        role="ha_softphone",
        state=CallState.CALLING.value,
        trunk_id=trunk_selection.identity if use_trunk else "",
    )
    return _PreparedBrowserSipCall(target=destination, client=client, registry=registry,
        session=session, uri=uri, local_rtp_port=local_rtp_port,
        target_device_id=target_device_id, video_enabled=video_enabled,
        video_failure_reason=video_failure_reason)


async def _async_run_browser_sip_call(
    call: ServiceCall, prepared: _PreparedBrowserSipCall, *, endpoint_id: str,
) -> None:
    """Start signaling, publish its first result and then attach the final-response watcher."""
    from .core.sip import sip_default_port
    from .sip_client import SIP_TIMER_B

    hass = call.hass
    destination = prepared.target
    resolved = destination.resolution
    route, route_uri = destination.route, destination.route_uri
    target, dialed_target = resolved.target, resolved.dialed_target
    target_endpoint, use_trunk = destination.endpoint, destination.use_trunk
    local_name, source_device_id = resolved.local_name, resolved.source_device_id
    client, session, uri = prepared.client, prepared.session, prepared.uri
    local_rtp_port, target_device_id = prepared.local_rtp_port, prepared.target_device_id
    video_enabled, video_failure_reason = prepared.video_enabled, prepared.video_failure_reason
    registry = prepared.registry
    try:
        client.audio_rtp_socket = bind_sip_rtp_socket(local_rtp_port)
        if client.audio_rtcp_min_interval > 0:
            client.audio_rtcp_socket = bind_sip_rtp_socket(local_rtp_port + 1)
        result = await client.invite(
            target=uri.user,
            target_display_name=dialed_target,
            remote_host=uri.host,
            remote_sip_port=sip_default_port(uri),
            request_uri=str(uri),
            timeout=SIP_TIMER_B if use_trunk else 8.0,
        )
    except Exception as err:  # noqa: BLE001 - isolate one outbound SIP leg.
        await EndpointTerminationHandler(hass).terminate_reason(
            client.dialog_ids.call_id,
            TerminalReason.TRANSPORT_UNREACHABLE.value,
            TerminationInitiator.RUNTIME,
        )
        raise _service_error(
            f"could not start SIP call to {dialed_target}",
            "sip_call_start_failed",
            destination=dialed_target,
        ) from err
    if registry.sip_client_for(client.dialog_ids.call_id) is not client:
        await EndpointTerminationHandler(hass).terminate_reason(
            client.dialog_ids.call_id,
            TerminalReason.LOCAL_HANGUP.value,
            TerminationInitiator.LOCAL_USER,
        )
        return
    if (
        result == TerminalReason.TRANSPORT_UNREACHABLE.value
        and route.entry is not None
        and route.entry.metadata.get("registered")
    ):
        await _mark_sip_account_unreachable(hass, route.entry.id)
    public_result = CallState.REMOTE_RINGING.value if result == "ringing" else _sip_public_state(result)
    if public_result in {CallState.REMOTE_RINGING.value, CallState.IN_CALL.value}:
        session = observe_outbound_call_result(
            registry, client.dialog_ids.call_id,
            state=public_result,
            caller=local_name,
            callee=dialed_target,
            route_kind="direct",
        )
    # Publish the first result before starting the detached final-response
    # watcher. A fast peer can place 180 and 200 on the socket back-to-back:
    # if the watcher runs first it publishes IN_CALL, then this coroutine used
    # to regress the same call to REMOTE_RINGING from the earlier 180 result.
    # Keeping the signaling order here makes the backend snapshot monotonic;
    # the card remains a plain mirror of that authoritative state.
    if public_result == CallState.REMOTE_RINGING.value or result == "ringing":
        publish_phone_projection(
            hass,
            session,
            endpoint_id, peer_name=dialed_target, direction="outgoing",
                target_device_id=target_device_id, sip_status_code=180,
                last_sip_event="SIP_RESPONSE", sip_uri=route_uri,
                sip_transport=client.signaling_transport.lower(),
        )
    elif public_result == CallState.IN_CALL.value and client.dialog is not None:
        connected_party = counterpart_display(client.connected_party, fallback=dialed_target,
                                              endpoint=target_endpoint, entry=route.entry,
                                              display_name=connected_display_name(client))
        video_active = bool(
            client.dialog.video_format is not None
            and client.dialog.local_video_direction != "inactive"
        )
        video_status = (
            "degraded"
            if video_failure_reason
            else "active"
            if video_active
            else "rejected"
            if video_enabled
            else "inactive"
        )
        final_video_failure_reason = video_failure_reason or (
            "remote_video_rejected" if video_enabled and not video_active else ""
        )
        publish_phone_projection(
            hass,
            session,
            endpoint_id, peer_name=connected_party,
            connected_party=connected_party, direction="outgoing",
            target_device_id=target_device_id,
            selected_tx_format=client.dialog.send_format.audio_format.wire_token(),
            selected_rx_format=client.dialog.recv_format.audio_format.wire_token(),
            selected_tx_rtp_format=client.dialog.send_format.wire_token(),
            selected_rx_rtp_format=client.dialog.recv_format.wire_token(),
            audio_direction=client.dialog.local_audio_direction,
            audio_connection_held=client.dialog.remote_audio_connection_held,
            video_active=video_active,
            video_requested=video_enabled,
            video_negotiated=video_active,
            video_status=video_status,
            video_failure_reason=final_video_failure_reason,
            video_format=(
                client.dialog.video_format.wire_token()
                if client.dialog.video_format
                else ""
            ),
            video_send_format=(
                client.dialog.send_video_format.wire_token()
                if client.dialog.send_video_format is not None
                else ""
            ),
            video_receive_format=(
                client.dialog.recv_video_format.wire_token()
                if client.dialog.recv_video_format is not None
                else ""
            ),
            video_direction=client.dialog.local_video_direction,
            sip_status_code=200,
            last_sip_event="SIP_RESPONSE",
            sip_uri=route_uri,
            sip_transport=client.signaling_transport.lower(),
        )
    await _track_outbound_sip_client(
        hass,
        client=client,
        result=result,
        target=dialed_target,
        sip_uri=route_uri,
        endpoint_id=endpoint_id,
        local_name=local_name,
        session_device_id=source_device_id,
        target_device_id=target_device_id,
        video_requested=video_enabled,
        video_failure_reason=video_failure_reason,
    )
    _LOGGER.info("SIP call target=%s uri=%s result=%s", target, route_uri, result)


async def _async_originate_browser_call(
    call: ServiceCall, *, endpoint_id: str, browser_endpoint,
    force_ha_bridge: bool = False, routing_scope: dict,
) -> None:
    """Originate one browser call through the existing routing and session owners."""
    resolved = await _async_resolve_browser_call_route(call, endpoint_id=endpoint_id,
        browser_endpoint=browser_endpoint, routing_scope=routing_scope)
    destination = await _async_dispatch_browser_call_route(call, resolved,
        endpoint_id=endpoint_id, browser_endpoint=browser_endpoint, force_ha_bridge=force_ha_bridge)
    if destination is None:
        return
    prepared = await _async_prepare_browser_sip_call(call, destination,
        endpoint_id=endpoint_id, browser_endpoint=browser_endpoint)
    await _async_run_browser_sip_call(call, prepared, endpoint_id=endpoint_id)


async def async_originate_browser_call(call, *, endpoint_id, browser_endpoint, force_ha_bridge=False):
    """Retire the existing pre-call owner when routing cannot reach a call leg."""
    routing_scope = {}
    try:
        return await _async_originate_browser_call(call, endpoint_id=endpoint_id,
            browser_endpoint=browser_endpoint, force_ha_bridge=force_ha_bridge, routing_scope=routing_scope)
    except BaseException:
        session = routing_scope.get("session")
        if session is not None and session.live:
            await EndpointTerminationHandler(call.hass).terminate_reason(
                session.call_id, TerminalReason.CANCELLED.value, TerminationInitiator.ROUTING)
        raise
