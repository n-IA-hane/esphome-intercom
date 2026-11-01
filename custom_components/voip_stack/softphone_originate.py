"""Outbound call orchestration for Home Assistant and ESP phone services."""

from __future__ import annotations

from dataclasses import replace
from functools import partial
import logging
import traceback

from homeassistant.core import HomeAssistant, ServiceCall

from .entity_identity import phonebook_state

from .trunk_policy import external_call_denied, is_any_trunk_uri, trunk_available
from .peer_media_profile import resolve_peer_media_profile
from .call_projection import publish_phone_projection
from .core.audio_format import HA_TRUNK_AUDIO_FORMATS
from .authorization import async_require_service_admin
from .config import (
    transport_config as _get_transport_config,
    trunk_enabled as _trunk_enabled,
    trunk_identity_uri as _trunk_identity_uri,
    trunk_leg_identity as _trunk_leg_identity,
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
    HA_PEER_FALLBACK_NAME,
)
from .endpoint_lifecycle import call_registry as _call_registry, create_runtime_task
from .endpoint_termination import EndpointTerminationHandler
from .endpoint_session import TerminationInitiator
from .endpoint_registry import EndpointBusyError
from .endpoint_routing import (
    ha_router_decision,
    peer_video_codec,
    device_formats as _device_formats,
    roster_entry_formats as _roster_entry_formats,
    sip_target_audio_profile as _sip_target_audio_profile,
    sip_target_has_unspecified_audio,
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
    EndpointKind,
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
    enable_reused_signaling_flow as _enable_reused_signaling_flow,
    select_registered_outbound_flow,
    uri_transport as _sip_uri_transport,
)
from .softphone_commands import (
    bind_service_call_controller as _bind_service_call_controller,
)
from .websocket_api import (
    _ha_softphone_store,
)


_LOGGER = logging.getLogger(__name__)


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
    if endpoint is None or not endpoint.kind.is_softphone:
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


async def async_originate_browser_call(
    call: ServiceCall,
    *,
    endpoint_id: str,
    browser_endpoint,
    force_ha_bridge: bool = False,
) -> None:
    """Originate a standards SIP call from one Home Assistant browser phone."""
    from .roster import parse_roster_json
    from .core.sip import SipUri, parse_sip_uri, sip_default_port
    from .sip_client import SIP_TIMER_B, SipCallClient

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
    sensor = phonebook_state(hass)
    roster_json = (
        str(sensor.attributes.get("roster_json") or "") if sensor is not None else ""
    )
    contacts = parse_roster_json(roster_json) if roster_json else []
    resolve_route = partial(ha_router_decision, hass, source_endpoint_id=endpoint_id,
                            trunk_id=trunk_selection.identity)
    route = resolve_route(target, contacts)
    from .inbound_routing.targets import resolve_phone_target

    resolved = resolve_phone_target(
        decision=route,
        endpoint_registry=endpoint_directory(hass),
        roster_entries=contacts,
        route_resolver=resolve_route,
    )
    if resolved.failure is not None:
        raise _service_error(
            f"Phone routing failed: {resolved.failure.decline_reason}",
            "phone_unavailable",
            phone=target,
        )
    if resolved.decision != route:
        target = resolved.decision.target
    route = resolved.decision
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
                    browser_endpoint is not None
                    and browser_endpoint.supports("video")
                    and cfg.get(CONF_VIDEO_CAMERA_SEND, False)
                    and call.data.get("send_video", False)
                ),
                enable_caller_video_send=bool(
                    cfg.get(CONF_VIDEO_CAMERA_SEND, False)
                    and call.data.get("send_video", False)
                ),
                caller_owner_id=str(call.data.get("media_client_id") or ""),
                context=getattr(call, "context", None),
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
    display_target = route.entry.display_name if route.entry is not None else target
    if route.action is RouteAction.PARKING and route.entry.metadata.get("parking_mode") == "directed_pickup":
        from .call_pickup import pickup_browser
        await _async_prepare_ha_outbound_call(hass, endpoint_id)
        await pickup_browser(call, browser_endpoint, route.entry.metadata["parking_slot"])
        return
    if route.action is RouteAction.PARKING and route.entry.metadata.get("parking_mode") == "pickup":
        from .parking import pickup_browser_call

        await _async_prepare_ha_outbound_call(hass, endpoint_id)
        await pickup_browser_call(call, browser_endpoint, route.entry.metadata["parking_slot"])
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
                **({"policy_endpoint_ids": policy_endpoint_ids} if policy_endpoint_ids else {}),
                **({"outbound_trunk_id": trunk_selection.identity} if trunk_selection.identity else {}),
                context=getattr(call, "context", None),
                endpoint_id=endpoint_id,
                media_client_id=str(call.data.get("media_client_id") or ""),
                request_video=bool(
                    browser_endpoint is not None
                    and browser_endpoint.supports("video")
                    and cfg.get(CONF_VIDEO_CAMERA_SEND, False)
                    and call.data.get("send_video", False)
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
        raise _service_error(
            f"cannot resolve SIP target: {target}",
            "sip_target_unresolved",
            destination=target,
        )
    await _async_prepare_ha_outbound_call(hass, endpoint_id)
    uri = parse_sip_uri(route_uri)
    entry_metadata = dict(route.entry.metadata or {}) if route.entry is not None else {}
    peer_profile = resolve_peer_media_profile(entry_metadata)
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
    rtp_audio_profile = _sip_target_rtp_audio_profile(
        None,
        route.entry,
        dest_device,
    )
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
    generic_audio_profile = use_trunk or (peer_profile.is_dahua and not native_audio_endpoint)
    if generic_audio_profile:
        sip_send_formats = list(HA_TRUNK_AUDIO_FORMATS)
        sip_recv_formats = list(HA_TRUNK_AUDIO_FORMATS)
        rtp_audio_profile = None
    elif rtp_audio_profile is not None:
        sip_send_formats = list(rtp_audio_profile.send_formats)
        sip_recv_formats = list(rtp_audio_profile.recv_formats)
    target_device_id = str(
        getattr(target_endpoint, "device_id", "")
        or entry_metadata.get("device_id")
        or ""
    ).strip()
    # Unknown remote capabilities keep local video in the offer. A successful
    # capability description can optimize a known audio-only destination.
    source_video_enabled = browser_endpoint is None or browser_endpoint.supports(
        "video"
    )
    target_video_contract = peer_video_codec(None, route.entry)
    target_video_enabled = target_video_contract != ""
    video_capable = (
        bool(cfg.get(CONF_SIP_VIDEO, False))
        and (not use_trunk or bool(trunk_cfg.get("trunk_video_codecs", ["H264"])))
        and source_video_enabled
        and target_video_enabled
        and (use_trunk or not native_audio_endpoint or esphome_sip_endpoint)
    )
    camera_send_enabled = bool(
        video_capable
        and cfg.get(CONF_VIDEO_CAMERA_SEND, False)
        and call.data.get("send_video", False)
    )
    target_video_codec = str(
        entry_metadata.get("sip_video_codec")
        or ((dest_device or {}).get("sip_video_codec"))
        or ""
    ).strip()
    # Some Dahua door stations add H.264 even to an audio-only answer. Offer
    # an honest receive-only stream for the existing peer profile instead of
    # accepting an unsolicited m-line or requiring the user's camera.
    dahua_receive_only = bool(
        video_capable
        and not camera_send_enabled
        and (not use_trunk or peer_profile.explicit_dahua)
        and not native_audio_endpoint
        and peer_profile.is_dahua
        and target_video_codec.upper() in {"", "H264"}
        and (target_endpoint is None or target_endpoint.supports("video"))
    )
    video_enabled = video_capable
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
    from .core.sdp import (
        DEFAULT_H264_FORMAT,
        DEFAULT_VIDEO_FORMATS,
        browser_video_send_supported,
        video_offer_formats_for_target_codec,
    )

    # Camera permission controls transmission; capable phones can receive
    # video with their camera off.
    offered_video_formats = (
        tuple(
            item for item in DEFAULT_VIDEO_FORMATS if browser_video_send_supported(item)
        )
        if camera_send_enabled
        else DEFAULT_VIDEO_FORMATS
    )
    offered_video_formats = video_offer_formats_for_target_codec(
        target_video_codec,
        offered_video_formats,
    )
    if dahua_receive_only:
        # RFC 6184 defaults an absent fmtp to Baseline Level 1, mode 0.
        # Advertise our existing Level 3.1 receive envelope; the answer may
        # select a lower level. Mode 0 is supported by the receive bridge.
        offered_video_formats = (
            replace(
                DEFAULT_H264_FORMAT,
                payload_type=105,
                profile_level_id="42001f",
                packetization_mode=0,
                level_asymmetry_allowed=False,
            ),
        )

    provider_identity = (
        _trunk_leg_identity(trunk_cfg, local_name) if use_trunk else local_name
    )
    client = SipCallClient(
        local_ip=local_ip,
        local_name=provider_identity,
        local_uri_user=(
            provider_identity
            if use_trunk
            else (
                browser_endpoint.sip_uri_user
                if browser_endpoint is not None
                else local_name
            )
        ),
        local_identity_uri=_trunk_identity_uri(trunk_cfg) if use_trunk else "",
        local_sip_port=int(cfg["sip_port"]),
        local_rtp_port=local_rtp_port,
        supported_send_formats=sip_send_formats,
        supported_recv_formats=sip_recv_formats,
        supported_send_rtp_formats=(
            rtp_audio_profile.send_rtp_formats
            if rtp_audio_profile is not None
            else None
        ),
        supported_recv_rtp_formats=(
            rtp_audio_profile.recv_rtp_formats
            if rtp_audio_profile is not None
            else None
        ),
        signaling_transport=_sip_uri_transport(uri),
        auth_username=str(trunk_cfg.get(CONF_TRUNK_AUTH_USERNAME) or ""),
        username=str(trunk_cfg.get(CONF_TRUNK_USERNAME) or ""),
        password=str(trunk_cfg.get(CONF_TRUNK_PASSWORD) or "") if use_trunk else "",
        outbound_proxy=str(trunk_cfg.get(CONF_TRUNK_OUTBOUND_PROXY) or "")
        if use_trunk
        else "",
        include_dahua_pcm=peer_profile.include_dahua_pcm if not native_audio_endpoint else None,
        match_received_codec=peer_profile.match_received_codec,
        include_common_codecs=(peer_profile.is_dahua and not native_audio_endpoint)
        or use_trunk
        or video_enabled
        or sip_target_has_unspecified_audio(None, route.entry, dest_device),
        allow_directional_audio_payloads=not generic_audio_profile,
        peer_user_agent=str(entry_metadata.get("user_agent") or ""),
        local_video_rtp_port=local_video_rtp_port,
        video_formats=offered_video_formats if video_enabled else (),
        video_direction=("sendrecv" if camera_send_enabled else "recvonly"),
        media_reservation=video_reservation,
        video_rtp_socket=video_rtp_socket,
        video_rtcp_socket=video_rtcp_socket,
    )

    from .browser_dialog_updates import attach_browser_dialog_updates
    attach_browser_dialog_updates(hass, client, endpoint_id, source_device_id)
    if use_trunk and trunk is not None:
        from .trunk_signaling import reuse_registered_trunk_flow

        if reuse_registered_trunk_flow(trunk, client):
            _LOGGER.info("SIP trunk registered UDP flow reused for outbound call")
    else:
        from .sip_runtime import configure_local_contact
        configure_local_contact(hass, client)
        _enable_reused_signaling_flow(
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
        callee=display_target,
        route_kind="direct",
        direction="outgoing",
        endpoint_id=endpoint_id,
        source_endpoint_id=endpoint_id,
        policy_endpoint_ids=policy_endpoint_ids,
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
            phone=str(getattr(target_endpoint, "name", "") or display_target),
        ) from err
    _bind_service_call_controller(registry, client.dialog_ids.call_id, call)
    publish_phone_projection(
        hass,
        session,
        endpoint_id, peer_name=display_target, direction="outgoing",
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
    try:
        client.audio_rtp_socket = bind_sip_rtp_socket(local_rtp_port)
        result = await client.invite(
            target=uri.user,
            target_display_name=display_target,
            remote_host=uri.host,
            remote_sip_port=sip_default_port(uri),
            request_uri=str(uri),
            timeout=SIP_TIMER_B if use_trunk else 8.0,
        )
    except Exception as err:  # noqa: BLE001 - isolate one outbound SIP leg.
        _LOGGER.error(
            "SIP call setup failed type=%s stack=%s", type(err).__name__,
            " > ".join(f"{frame.filename.rsplit('/', 1)[-1]}:{frame.name}:{frame.lineno}"
                       for frame in traceback.extract_tb(err.__traceback__)),
        )
        await EndpointTerminationHandler(hass).terminate_reason(
            client.dialog_ids.call_id,
            TerminalReason.TRANSPORT_UNREACHABLE.value,
            TerminationInitiator.RUNTIME,
        )
        raise _service_error(
            f"could not start SIP call to {display_target}",
            "sip_call_start_failed",
            destination=display_target,
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
            callee=display_target,
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
            endpoint_id, peer_name=display_target, direction="outgoing",
                target_device_id=target_device_id, sip_status_code=180,
                last_sip_event="SIP_RESPONSE", sip_uri=route_uri,
                sip_transport=client.signaling_transport.lower(),
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
