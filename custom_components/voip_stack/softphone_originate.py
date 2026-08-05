"""Outbound call orchestration for Home Assistant and ESP phone services."""

from __future__ import annotations

from dataclasses import replace
import logging

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError

from .audio_format import HA_TRUNK_AUDIO_FORMATS
from .authorization import async_require_service_admin
from .config import (
    transport_config as _get_transport_config,
    trunk_config as _get_trunk_config,
    trunk_enabled as _trunk_enabled,
)
from .const import (
    CONF_SIP_VIDEO,
    CONF_TRUNK_AUTH_USERNAME,
    CONF_TRUNK_OUTBOUND_PROXY,
    CONF_TRUNK_PASSWORD,
    CONF_TRUNK_PORT,
    CONF_TRUNK_SERVER,
    CONF_TRUNK_TRANSPORT,
    CONF_TRUNK_USERNAME,
    CONF_VIDEO_CAMERA_SEND,
    DOMAIN,
    HA_PEER_FALLBACK_NAME,
    HA_SOFTPHONE_DEVICE_ID,
)
from .endpoint_lifecycle import call_registry as _call_registry, create_runtime_task
from .endpoint_registry import EndpointBusyError
from .endpoint_routing import (
    device_formats as _device_formats,
    roster_entry_formats as _roster_entry_formats,
    sip_target_audio_profile as _sip_target_audio_profile,
)
from .esphome_actions import async_resolve_target_device as _resolve_target_device
from .fsm import (
    CallState,
    TerminalReason,
    sip_public_state as _sip_public_state,
    sip_terminal_reason as _sip_terminal_reason,
)
from .media_ports import (
    allocate_sip_rtp_port as _allocate_sip_rtp_port,
    reserve_sip_video_media,
)
from .media_session_updates import (
    commit_audio_session_update,
    commit_video_session_update,
)
from .outbound_lifecycle import (
    HA_SOFTPHONE_ACTIVE_STATES,
    attach_outbound_connected_identity_state,
    async_prepare_ha_outbound_call as _async_prepare_ha_outbound_call,
    async_track_outbound_sip_client as _track_outbound_sip_client,
)
from .peer_snapshot import async_advertise_host as _ha_advertise_host
from .phone_endpoint import (
    DEFAULT_ENDPOINT_ID,
    EndpointAvailability,
    EndpointKind,
)
from .router import RouteAction, RouteReason, ha_uri_for, resolve_ha_router
from .service_endpoints import (
    async_require_phone_service_control as _require_phone_service_control,
    browser_endpoint_name as _browser_endpoint_name,
)
from .sip_runtime import (
    enable_reused_tcp_connection as _enable_reused_sip_tcp_connection,
    uri_transport as _sip_uri_transport,
)
from .softphone_commands import (
    bind_service_call_controller as _bind_service_call_controller,
)
from .websocket_api import (
    _fire_call_event,
    _ha_softphone_store,
    _set_ha_softphone_call_state,
)


_LOGGER = logging.getLogger(__name__)


async def _mark_sip_account_unreachable(hass: HomeAssistant, username: str) -> None:
    """Drop a stale registrar Contact while keeping the account in the roster."""

    wanted = (username or "").strip().lower()
    if not wanted:
        return
    registrar = hass.data.get(DOMAIN, {}).get("sip_registrar")
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


def _ha_softphone_has_active_call(
    hass: HomeAssistant,
    *,
    endpoint_id: str = DEFAULT_ENDPOINT_ID,
    ignore_call_id: str = "",
) -> bool:
    store = _ha_softphone_store(hass, endpoint_id)
    if ignore_call_id and str(store.get("call_id") or "") == ignore_call_id:
        return False
    state = str(store.get("state") or CallState.IDLE.value)
    return bool(store.get("session_device_id") or state in HA_SOFTPHONE_ACTIVE_STATES)


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
    endpoint_registry = hass.data.get(DOMAIN, {}).get("endpoint_registry")
    if endpoint_registry is None:
        return route, target, None

    if route.action is not RouteAction.ANSWER_HA or route.entry is None:
        return route, target, None
    endpoint_id = str((route.entry.metadata or {}).get("endpoint_id") or "").strip()
    endpoint = endpoint_registry.get(endpoint_id) if endpoint_id else None
    if endpoint is None or endpoint.kind is not EndpointKind.BROWSER:
        return route, target, None
    if endpoint.endpoint_id == source_endpoint_id:
        raise ServiceValidationError("a Home Assistant phone cannot call itself")
    if endpoint.dnd or endpoint.active_call_id:
        raise ServiceValidationError(f"{endpoint.name} is busy")
    if endpoint.availability is EndpointAvailability.UNAVAILABLE:
        raise ServiceValidationError(f"{endpoint.name} is disabled")

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
    registry = hass.data.get(DOMAIN, {}).get("endpoint_registry")
    return registry.get(endpoint_id) if registry is not None and endpoint_id else None


async def async_originate_browser_call(
    call: ServiceCall,
    *,
    endpoint_id: str,
    browser_endpoint,
    force_ha_bridge: bool = False,
) -> None:
    """Originate a standards SIP call from one Home Assistant browser phone."""
    from .roster import parse_roster_json
    from .sip import parse_sip_uri
    from .sip_client import SIP_TIMER_B, SipCallClient

    hass: HomeAssistant = call.hass
    dest_device = await _resolve_target_device(hass, call)
    target = str(call.data.get("destination") or "").strip()
    if not target and dest_device is not None:
        target = str(dest_device.get("name") or "").strip()
    if not target:
        raise ServiceValidationError("destination is required")
    await _require_phone_service_control(
        hass,
        call,
        endpoint=browser_endpoint,
    )
    if (
        browser_endpoint is not None
        and browser_endpoint.availability is EndpointAvailability.UNAVAILABLE
    ):
        raise ServiceValidationError(f"{browser_endpoint.name} is disabled")
    local_name = _browser_endpoint_name(hass, endpoint_id, browser_endpoint)
    source_device_id = str(
        getattr(browser_endpoint, "device_id", "") or HA_SOFTPHONE_DEVICE_ID
    )
    _ha_softphone_store(hass, endpoint_id)["device_id"] = source_device_id
    cfg = _get_transport_config(hass)
    trunk = hass.data.get(DOMAIN, {}).get("sip_trunk")
    trunk_cfg = _get_trunk_config(hass)
    trunk_ready = _trunk_enabled(trunk_cfg) and bool(
        getattr(trunk, "registered", False)
    )
    sensor = hass.states.get("sensor.voip_phonebook")
    roster_json = (
        str(sensor.attributes.get("roster_json") or "") if sensor is not None else ""
    )
    contacts = parse_roster_json(roster_json) if roster_json else []
    route = resolve_ha_router(target, contacts, trunk_ready=trunk_ready)
    route, target, browser_destination = await _async_resolve_browser_destination(
        hass,
        route=route,
        target=target,
        source_endpoint_id=endpoint_id,
    )
    if browser_destination is not None:
        from .local_softphone_bridge import LocalBridgeError
        from .local_softphone_runtime import start_local_softphone_call

        await _async_prepare_ha_outbound_call(hass, endpoint_id)
        try:
            snapshot = start_local_softphone_call(
                hass,
                endpoint_id,
                browser_destination.endpoint_id,
                request_video=bool(
                    browser_endpoint is not None and browser_endpoint.supports("video")
                ),
                enable_caller_video_send=bool(
                    cfg.get(CONF_VIDEO_CAMERA_SEND, False)
                    and call.data.get("send_video", False)
                ),
                caller_owner_id=str(call.data.get("media_client_id") or ""),
                context=getattr(call, "context", None),
            )
        except LocalBridgeError as err:
            raise ServiceValidationError(str(err)) from err
        _LOGGER.info(
            "HA local phone call started call_id=%s source=%s destination=%s video=%s",
            snapshot.call_id,
            endpoint_id,
            browser_destination.endpoint_id,
            snapshot.video_enabled,
        )
        return
    target_endpoint = _logical_endpoint_for_route(hass, route)
    if target_endpoint is not None and target_endpoint.kind is not EndpointKind.BROWSER:
        if target_endpoint.dnd or target_endpoint.active_call_id:
            raise ServiceValidationError(f"{target_endpoint.name} is busy")
        if target_endpoint.availability is not EndpointAvailability.AVAILABLE:
            raise ServiceValidationError(f"{target_endpoint.name} is unavailable")
    # Browser-to-browser calls use the in-memory logical bridge and must not
    # depend on SIP network discovery.  Resolve the advertised address only
    # after that path has been exhausted, for conference or external SIP/RTP.
    local_ip = await _ha_advertise_host(hass)
    if not local_ip:
        raise ServiceValidationError("HA advertise IP is unknown")
    if route.reason is RouteReason.DIRECT_URI:
        # Entity CONTROL permits ordinary phone operation, but an ad-hoc SIP
        # URI also chooses an arbitrary network host/port.  Keep that SSRF and
        # port-probing capability behind HA administrator authentication;
        # roster contacts, registered endpoints and trunk numbers are still
        # available to normal controllers.
        await async_require_service_admin(hass, call)
    display_target = route.entry.display_name if route.entry is not None else target
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
    use_registered_contact_codecs = bool(
        route.entry is not None
        and route.entry.sip_uri
        and route.entry.metadata.get("registered")
    )
    if route.action is RouteAction.TRUNK and not use_trunk:
        raise ServiceValidationError(f"{target} requires a registered SIP trunk")
    if route.action is RouteAction.GROUP and route.entry is not None:
        group_type = str((route.entry.metadata or {}).get("group_type") or "")
        if group_type == "ring":
            start_ring_group = hass.data.setdefault(DOMAIN, {}).get(
                "async_start_ring_group_from_ha"
            )
            if start_ring_group is None:
                raise ServiceValidationError(f"{target} is not available yet")
            await _async_prepare_ha_outbound_call(hass, endpoint_id)
            await start_ring_group(
                route.entry,
                context=getattr(call, "context", None),
                endpoint_id=endpoint_id,
                media_client_id=str(call.data.get("media_client_id") or ""),
                request_video=bool(
                    browser_endpoint is not None and browser_endpoint.supports("video")
                ),
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
            )
            if joined is None:
                raise ServiceValidationError(f"Conference {room_name} is full")
            call_id, queue = joined
            registry = _call_registry(hass)
            conference_media = {
                "conference_room": room_name,
                "conference_queue": queue,
                "endpoint_id": endpoint_id,
                "media_client_id": str(call.data.get("media_client_id") or ""),
            }
            registry.upsert(
                call_id,
                state=CallState.IN_CALL.value,
                owner="ha_softphone",
                caller=local_name,
                callee=room_name,
                route_kind="conference",
                endpoint_id=endpoint_id,
                source_endpoint_id=endpoint_id,
                media_client_id=str(call.data.get("media_client_id") or ""),
            )
            registry.attach_media(call_id, conference_media)
            _bind_service_call_controller(registry, call_id, call)
            registry.add_leg(
                call_id, call_id, role="ha_softphone", state=CallState.IN_CALL.value
            )
            _set_ha_softphone_call_state(
                hass,
                CallState.IN_CALL.value,
                endpoint_id=endpoint_id,
                session_device_id=source_device_id,
                caller=local_name,
                callee=room_name,
                peer_name=room_name,
                direction="outgoing",
                call_id=call_id,
                route_kind="conference",
                sip_status_code=200,
                last_sip_event="LOCAL_CONFERENCE_JOIN",
                selected_tx_format="16000:s16le:1:20",
                selected_rx_format="16000:s16le:1:20",
                selected_tx_rtp_format="pt=96:L16/16000/1/20ms",
                selected_rx_rtp_format="pt=96:L16/16000/1/20ms",
                scope="conference",
                room=room_name,
                target=target,
            )
            ring_members = hass.data.setdefault(DOMAIN, {}).get(
                "async_ring_conference_members"
            )
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
    elif route.action is RouteAction.ASSIST:
        # Assist is a PBX-local application.  Originate the browser media leg
        # to this integration's listener so the canonical inbound dispatcher
        # owns the Assist session and RTP pipeline; never fall through to the
        # external-trunk resolver merely because the roster entry has no
        # network address of its own.
        route_uri = ha_uri_for(route.target or target, contacts)
        route = replace(route, action=RouteAction.BRIDGE, sip_uri=route_uri)
    if use_trunk:
        trunk_target = route.target or target
        route_uri = (
            f"sip:{trunk_target}@{trunk_cfg[CONF_TRUNK_SERVER]}:{int(trunk_cfg[CONF_TRUNK_PORT])};"
            f"transport={str(trunk_cfg[CONF_TRUNK_TRANSPORT]).lower()}"
        )
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
        raise ServiceValidationError(f"cannot resolve SIP target: {target}")
    await _async_prepare_ha_outbound_call(hass, endpoint_id)
    uri = parse_sip_uri(route_uri)
    remote_tx_formats = _roster_entry_formats(
        route.entry, "tx_formats"
    ) or _device_formats(dest_device, "tx_formats")
    remote_rx_formats = _roster_entry_formats(
        route.entry, "rx_formats"
    ) or _device_formats(dest_device, "rx_formats")
    sip_send_formats, sip_recv_formats = _sip_target_audio_profile(
        remote_tx_formats=remote_tx_formats,
        remote_rx_formats=remote_rx_formats,
        target=target,
    )
    if use_trunk or use_registered_contact_codecs:
        sip_send_formats = list(HA_TRUNK_AUDIO_FORMATS)
        sip_recv_formats = list(HA_TRUNK_AUDIO_FORMATS)
    entry_metadata = dict(route.entry.metadata or {}) if route.entry is not None else {}
    target_device_id = str(
        getattr(target_endpoint, "device_id", "")
        or entry_metadata.get("device_id")
        or ""
    ).strip()
    native_audio_endpoint = bool(
        entry_metadata.get("local_ha")
        or entry_metadata.get("virtual_endpoint")
        or entry_metadata.get("group_type")
        or str(entry_metadata.get("endpoint_kind") or "").strip().lower()
        == EndpointKind.ESPHOME.value
    )
    esphome_sip_endpoint = (
        str(entry_metadata.get("endpoint_kind") or "").strip().lower()
        == EndpointKind.ESPHOME.value
    )
    # SIP video is HA/browser-owned.  Local HA/group endpoints stay outside
    # this path.  An ESPHome endpoint is allowed to negotiate video and remains
    # standards-compatible with audio-only firmware, which can reject the video
    # media line while accepting the audio line.
    # A phonebook entry may carry ESP-style audio capability metadata while
    # its resolved route still exits through the SIP trunk.  The final route,
    # not those contact hints, decides whether browser-owned video is valid.
    source_video_enabled = browser_endpoint is None or browser_endpoint.supports(
        "video"
    )
    video_enabled = (
        bool(cfg.get(CONF_SIP_VIDEO, False))
        and source_video_enabled
        and (use_trunk or not native_audio_endpoint or esphome_sip_endpoint)
    )
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
    from .sdp import (
        DEFAULT_VIDEO_FORMATS,
        browser_video_send_supported,
        video_formats_renegotiation_compatible,
    )

    # Camera transmission is opt-in for each call.  Keep offering a receive
    # path when it is off, but never advertise send capability solely because
    # the administrator enabled browser camera transmission globally.
    camera_send_enabled = (
        video_enabled
        and bool(cfg.get(CONF_VIDEO_CAMERA_SEND, False))
        and bool(call.data.get("send_video", False))
    )
    offered_video_formats = (
        tuple(
            item for item in DEFAULT_VIDEO_FORMATS if browser_video_send_supported(item)
        )
        if camera_send_enabled
        else DEFAULT_VIDEO_FORMATS
    )

    client = SipCallClient(
        local_ip=local_ip,
        local_name=local_name,
        local_uri_user=(
            str(trunk_cfg.get(CONF_TRUNK_USERNAME) or local_name)
            if use_trunk
            else (
                browser_endpoint.sip_uri_user
                if browser_endpoint is not None
                else local_name
            )
        ),
        local_sip_port=int(cfg["sip_port"]),
        local_rtp_port=local_rtp_port,
        supported_send_formats=sip_send_formats,
        supported_recv_formats=sip_recv_formats,
        signaling_transport=_sip_uri_transport(uri),
        auth_username=str(trunk_cfg.get(CONF_TRUNK_AUTH_USERNAME) or ""),
        username=str(trunk_cfg.get(CONF_TRUNK_USERNAME) or ""),
        password=str(trunk_cfg.get(CONF_TRUNK_PASSWORD) or "") if use_trunk else "",
        outbound_proxy=str(trunk_cfg.get(CONF_TRUNK_OUTBOUND_PROXY) or "")
        if use_trunk
        else "",
        include_common_codecs=use_trunk
        or use_registered_contact_codecs
        or video_enabled,
        peer_user_agent=(
            str(entry_metadata.get("user_agent") or "")
            if use_registered_contact_codecs
            else ""
        ),
        local_video_rtp_port=local_video_rtp_port,
        video_formats=offered_video_formats if video_enabled else (),
        video_direction=("sendrecv" if camera_send_enabled else "recvonly"),
        media_reservation=video_reservation,
        video_rtp_socket=video_rtp_socket,
        video_rtcp_socket=video_rtcp_socket,
    )

    async def _prepare_softphone_media_update(previous, updated, method):
        """Stage one peer re-offer for the live HA browser media owner."""

        call_id = client.dialog_ids.call_id
        session = registry.sessions.get(registry.resolve_session_id(call_id))
        if session is None:
            return None
        call_generation = session.generation
        bucket = hass.data.setdefault(DOMAIN, {})
        audio_session = bucket.setdefault("active_audio_sessions", {}).get(call_id)
        previous_video = previous.video_format
        updated_video = updated.video_format
        if (previous_video is None) != (updated_video is None):
            return None
        video_session = bucket.setdefault("active_video_sessions", {}).get(call_id)
        if previous_video is not None and updated_video is not None:
            if not (
                video_formats_renegotiation_compatible(
                    previous_video,
                    updated_video,
                )
                and video_formats_renegotiation_compatible(
                    previous.recv_video_format,
                    updated.recv_video_format,
                )
            ):
                return None

        async def _commit() -> None:
            if not registry.is_generation_current(call_id, call_generation):
                raise RuntimeError(
                    "SIP softphone media update belongs to a terminated call"
                )
            if audio_session is not None:
                commit_audio_session_update(
                    audio_session,
                    updated,
                    dtmf_payload_type=updated.dtmf_payload_type,
                    dtmf_events=updated.dtmf_events,
                )
            if video_session is not None and updated_video is not None:
                registry.clear_video_parameter_sets(call_id)
                commit_video_session_update(
                    video_session,
                    updated,
                    local_direction=updated.local_video_direction,
                )
            store = _ha_softphone_store(hass, endpoint_id)
            if str(store.get("call_id") or "") == call_id:
                store.update(
                    {
                        "audio_direction": updated.local_audio_direction,
                        "audio_connection_held": updated.remote_audio_connection_held,
                        "video_active": bool(
                            updated_video is not None
                            and updated.local_video_direction != "inactive"
                        ),
                        "video_requested": bool(updated_video is not None),
                        "video_negotiated": bool(updated_video is not None),
                        "video_status": (
                            "active"
                            if updated_video is not None
                            and updated.local_video_direction != "inactive"
                            else "inactive"
                        ),
                        "video_failure_reason": "",
                        "video_format": (
                            updated_video.wire_token() if updated_video else ""
                        ),
                        "video_send_format": (
                            updated.send_video_format.wire_token()
                            if updated.send_video_format is not None
                            else ""
                        ),
                        "video_receive_format": (
                            updated.recv_video_format.wire_token()
                            if updated.recv_video_format is not None
                            else ""
                        ),
                        "video_direction": updated.local_video_direction,
                        "video_connection_held": updated.remote_video_connection_held,
                        "last_sip_event": method,
                        "media_renegotiations": int(
                            store.get("media_renegotiations") or 0
                        )
                        + 1,
                    }
                )
                _fire_call_event(
                    hass,
                    dict(
                        store,
                        endpoint_id=endpoint_id,
                        device_id=source_device_id,
                    ),
                    "session",
                )

        return _commit

    client.on_media_update = _prepare_softphone_media_update
    if not use_trunk:
        _enable_reused_sip_tcp_connection(
            hass,
            client,
            uri,
            target=target,
            default_sip_port=int(cfg["sip_port"]),
        )
    registry = _call_registry(hass)
    registry.upsert(
        client.dialog_ids.call_id,
        state=CallState.CALLING.value,
        owner="ha_softphone",
        caller=local_name,
        callee=display_target,
        route_kind="direct",
        direction="outgoing",
        endpoint_id=endpoint_id,
        media_client_id=str(call.data.get("media_client_id") or ""),
        target_endpoint_id=(
            target_endpoint.endpoint_id if target_endpoint is not None else ""
        ),
    )
    from .dtmf_events import attach_direct_client_dtmf_events

    attach_direct_client_dtmf_events(
        hass,
        client,
        call_id=client.dialog_ids.call_id,
        caller=local_name,
        callee=display_target,
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
            and target_endpoint.kind is not EndpointKind.BROWSER
        ):
            registry.claim_endpoint(
                client.dialog_ids.call_id,
                target_endpoint.endpoint_id,
                role="destination",
            )
    except EndpointBusyError as err:
        registry.finish_and_pop(
            client.dialog_ids.call_id,
            reason=TerminalReason.BUSY.value,
            state=CallState.BUSY.value,
        )
        await client.close()
        raise ServiceValidationError(str(err)) from err
    _bind_service_call_controller(registry, client.dialog_ids.call_id, call)
    _set_ha_softphone_call_state(
        hass,
        CallState.CALLING.value,
        endpoint_id=endpoint_id,
        session_device_id=source_device_id,
        caller=local_name,
        callee=display_target,
        peer_name=display_target,
        direction="outgoing",
        call_id=client.dialog_ids.call_id,
        target_device_id=target_device_id,
        sip_transport=_sip_uri_transport(uri).lower(),
        last_sip_event="INVITE",
        sip_uri=route_uri,
        video_requested=video_enabled,
        video_negotiated=False,
        video_status=(
            "degraded"
            if video_failure_reason
            else "requested"
            if video_enabled
            else "inactive"
        ),
        video_failure_reason=video_failure_reason,
    )
    registry.attach_sip_client(
        client.dialog_ids.call_id,
        client.dialog_ids.call_id,
        client,
        role="ha_softphone",
        state=CallState.CALLING.value,
    )
    try:
        result = await client.invite(
            target=uri.user,
            target_display_name=display_target,
            remote_host=uri.host,
            remote_sip_port=uri.port or int(cfg["sip_port"]),
            request_uri=str(uri),
            timeout=SIP_TIMER_B if use_trunk else 8.0,
        )
    except Exception as err:  # noqa: BLE001 - isolate one outbound SIP leg.
        registry.detach_client(client.dialog_ids.call_id)
        await client.close()
        _set_ha_softphone_call_state(
            hass,
            CallState.TRANSPORT_UNREACHABLE.value,
            endpoint_id=endpoint_id,
            session_device_id=source_device_id,
            caller=local_name,
            callee=display_target,
            peer_name=display_target,
            direction="outgoing",
            call_id=client.dialog_ids.call_id,
            target_device_id=target_device_id,
            reason=TerminalReason.TRANSPORT_UNREACHABLE.value,
            terminal_reason=TerminalReason.TRANSPORT_UNREACHABLE.value,
            last_sip_event="INVITE_ERROR",
            sip_uri=route_uri,
        )
        registry.finish_and_pop(
            client.dialog_ids.call_id,
            reason=TerminalReason.TRANSPORT_UNREACHABLE.value,
            state=CallState.TRANSPORT_UNREACHABLE.value,
        )
        raise ServiceValidationError(
            f"could not start SIP call to {display_target}"
        ) from err
    if registry.sip_clients.get(client.dialog_ids.call_id) is not client:
        await client.close()
        return
    if (
        result == TerminalReason.TRANSPORT_UNREACHABLE.value
        and route.entry is not None
        and route.entry.metadata.get("registered")
    ):
        await _mark_sip_account_unreachable(hass, route.entry.id)
    public_result = _sip_public_state(result)
    # Publish the first result before starting the detached final-response
    # watcher. A fast peer can place 180 and 200 on the socket back-to-back:
    # if the watcher runs first it publishes IN_CALL, then this coroutine used
    # to regress the same call to REMOTE_RINGING from the earlier 180 result.
    # Keeping the signaling order here makes the backend snapshot monotonic;
    # the card remains a plain mirror of that authoritative state.
    if public_result == CallState.REMOTE_RINGING.value or result == "ringing":
        _set_ha_softphone_call_state(
            hass,
            CallState.REMOTE_RINGING.value,
            endpoint_id=endpoint_id,
            session_device_id=source_device_id,
            caller=local_name,
            callee=display_target,
            peer_name=display_target,
            direction="outgoing",
            call_id=client.dialog_ids.call_id,
            target_device_id=target_device_id,
            sip_status_code=180,
            last_sip_event="SIP_RESPONSE",
            sip_uri=route_uri,
        )
    elif public_result == CallState.IN_CALL.value and client.dialog is not None:
        connected_party = str(client.connected_party or display_target).strip()
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
        _set_ha_softphone_call_state(
            hass,
            CallState.IN_CALL.value,
            endpoint_id=endpoint_id,
            session_device_id=source_device_id,
            caller=local_name,
            callee=display_target,
            peer_name=connected_party,
            connected_party=connected_party,
            direction="outgoing",
            call_id=client.dialog_ids.call_id,
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
        )
    elif public_result not in {CallState.REMOTE_RINGING.value, CallState.IN_CALL.value}:
        terminal_reason = _sip_terminal_reason(result, public_result)
        _set_ha_softphone_call_state(
            hass,
            public_result,
            endpoint_id=endpoint_id,
            session_device_id=source_device_id,
            caller=local_name,
            callee=display_target,
            peer_name=display_target,
            direction="outgoing",
            call_id=client.dialog_ids.call_id,
            target_device_id=target_device_id,
            reason=terminal_reason,
            terminal_reason=terminal_reason,
            sip_status_code=client.last_sip_status_code,
            last_sip_event=client.last_sip_event or "SIP_RESPONSE",
            sip_uri=route_uri,
        )
    await _track_outbound_sip_client(
        hass,
        client=client,
        result=result,
        target=target,
        sip_uri=route_uri,
        endpoint_id=endpoint_id,
        local_name=local_name,
        session_device_id=source_device_id,
        target_device_id=target_device_id,
        video_requested=video_enabled,
        video_failure_reason=video_failure_reason,
    )
    _LOGGER.info("SIP call target=%s uri=%s result=%s", target, route_uri, result)
