"""Runtime SIP endpoint/B2BUA orchestration for VoIP Stack."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
import logging
import secrets
import time
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant

from . import sdp as sip_sdp
from .audio_format import (
    HA_SIP_PCM_FORMATS,
    HA_SIP_PCM_RX_FORMATS,
    HA_SIP_PCM_TX_FORMATS,
    HA_TRUNK_AUDIO_FORMATS,
)
from .automation_routing import (
    canonical_call_origin,
)
from .call_registry import TERMINAL_STATES
from .config import debug_mode as _debug_mode
from .config_entry_runtime import (
    async_refresh_and_push_phonebook as _refresh_and_push_phonebook,
)
from .const import (
    CONF_AUTOMATION_ROUTING_ENABLED,
    CONF_ASSIST_ADVANCED_CALL_CONTEXT,
    CONF_ASSIST_PIPELINE,
    CONF_SIP_VIDEO,
    CONF_REGISTRAR_ENABLED,
    CONF_VIDEO_CAMERA_SEND,
    CONF_VIDEO_TRANSCODING,
    CONF_TRUNK_AUTH_USERNAME,
    CONF_TRUNK_DTMF_ENABLED,
    CONF_TRUNK_DTMF_TERMINATOR,
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
from .endpoint_lifecycle import (
    async_stop_sip_endpoint,
    call_registry as _call_registry,
    create_runtime_task,
)
from .dtmf_events import (
    attach_dtmf_event_bridge as _attach_dtmf_event_bridge,
    publish_dtmf_event as _publish_dtmf_event,
)
from .endpoint_routing import (
    EndpointRouteResolver,
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
from .media_ports import (
    RtpPortReservation,
    allocate_sip_rtp_port as _allocate_sip_rtp_port,
    release_media_reservation as _release_media_reservation,
    release_video_media_reservation as _release_video_media_reservation,
    release_sip_rtp_port_pair as _release_sip_rtp_port_pair,
    reserve_sip_video_media,
    reserve_sip_video_relay_media,
)
from .media_renegotiation import async_prepare_media_update
from .outbound_attempts import (
    BrowserLeg,
    OutboundLeg,
    async_cancel_and_join_tasks as _cancel_and_join_tasks,
    async_cleanup_outbound_attempts as _cleanup_outbound_attempts,
    async_close_client_and_release as _close_client_and_release,
    async_close_outbound_leg as _close_outbound_leg,
)
from .endpoint_registry import EndpointBusyError
from .phone_endpoint import (
    DEFAULT_ENDPOINT_ID,
    EndpointAvailability,
    EndpointKind,
    OfflinePolicy,
)
from .pbx_routing import (
    browser_endpoint_can_ring as _browser_endpoint_can_ring,
    caller_matches_group_member as _caller_matches_member,
    dtmf_extension_routes as _dtmf_extension_routes,
    roster_entry_for_target as _roster_entry_for_target,
    unique_group_members as _unique_group_members,
)
from .phonebook_runtime import registered_roster_entries as _registered_roster_entries
from .router import (
    CallContext,
    RouteAction,
    RouteReason,
    route_inbound_trunk,
)
from .ring_group import (
    endpoint_is_esphome as _endpoint_is_esphome,
    endpoint_preflight_disposition as _endpoint_preflight_disposition,
    settle_browser_candidates as _settle_ring_browser_candidates,
)
from .session_cleanup import async_cleanup_sip_runtime, async_wait_for_cleanup
from .sip_bridge import (
    build_local_client_relay,
    build_pending_invite_video_relay,
    build_invite_client_relay,
    configure_answered_invite_video_relay,
    dialog_rtp_peer,
    dialog_video_rtp_peer,
)
from .store import sip_accounts as _sip_accounts
from .trunk_dtmf import collect_trunk_dtmf as _collect_trunk_dtmf
from .trunk_routing import (
    async_request_inbound_destination as _request_inbound_destination,
    trunk_default_target as _trunk_default_target,
)
from .websocket_api import (
    _ha_softphone_store,
    _release_ha_softphone_claim,
    _set_ha_softphone_call_state,
    _set_sip_bridge_call_state,
)

if TYPE_CHECKING:
    from .peer import Peer
    from .roster import RosterEntry

_LOGGER = logging.getLogger(__name__)
SIP_ROUTE_DECISION_TIMEOUT = 1.5
RING_GROUP_TIMEOUT_S = 30.0
MAX_RING_GROUP_ATTEMPTS = 16
MAX_TRUNK_INFO_DIGITS = 16
MAX_PENDING_HA_INVITES = 64


def _invite_dtmf_format(invite):
    formats = sip_sdp.offered_dtmf_formats(invite.remote_sdp)
    return formats[0] if formats else None


def _source_dialog_is_answered(early_media: dict | None) -> bool:
    """Return whether the inbound source already received a final 2xx."""
    return early_media is not None and bool(
        early_media.get("final_response_sent", True)
    )


async def async_start_sip_endpoint(hass: HomeAssistant) -> bool:
    """Bind the enabled SIP signaling listeners for HA softphone and bridge calls."""
    from .config import (
        transport_config as _get_transport_config,
        trunk_config as _get_trunk_config,
        trunk_enabled as _trunk_enabled,
    )
    from .softphone_termination import (
        async_terminate_sip_bridge_session as _terminate_sip_bridge,
    )
    from .websocket_api import _ha_peer_name
    from .call_scope import pending_routes as _pending_routes
    from .peer_snapshot import (
        async_advertise_host as _ha_advertise_host,
        async_build_peer_snapshot as _async_build_peer_snapshot,
    )
    from .sip_runtime import (
        enable_reused_tcp_connection as _enable_reused_sip_tcp_connection,
        send_bye as _sip_send_bye,
        send_final_response as _sip_send_final_response,
        uri_transport as _sip_uri_transport,
    )
    from .dtmf import parse_sip_info_digit
    from .dial_fork import (
        DialCandidate,
        DialDisposition,
        DialForkController,
        DialOutcome,
        LegCloseMode,
    )
    from .dial_plan import RingPolicy, build_sip_contact_targets
    from .sdp import (
        build_answer_directional,
        constrained_video_direction,
        video_formats_passthrough_compatible,
    )
    from .sip import parse_sip_uri, sip_endpoints_equal, sip_uri_targets_listener
    from .sip_client import SIP_TIMER_B, SipCallClient
    from .sip_endpoint import SipEndpointManager
    from .sip_listener import SipInvite, SipInviteResult
    from .pbx_runtime import SipEndpointRuntime
    from .sip_registrar import SipRegistrar
    from .conference import MAX_CONFERENCE_LEGS, conference_manager
    from .groups import GROUP_TYPE_CONFERENCE, GROUP_TYPE_RING

    if hass.data.get(DOMAIN, {}).get("sip_endpoint") is not None:
        _LOGGER.debug("Stopping existing SIP endpoint before rebinding listeners")
        await async_stop_sip_endpoint(hass)

    cfg = _get_transport_config(hass)
    local_ip = await _ha_advertise_host(hass)
    if not local_ip:
        _LOGGER.error("Cannot start SIP endpoint: HA announce IP is unknown")
        return False

    async def _on_conference_inbound_timeout(call_id: str, reason: str) -> None:
        """End a timed-out inbound UAS dialog and release its logical claim."""
        if not _sip_send_bye(hass, call_id):
            _LOGGER.warning(
                "Conference media timeout could not send SIP BYE call_id=%s",
                call_id,
            )
        registry = _call_registry(hass)
        session = registry.sessions.get(registry.resolve_session_id(call_id))
        _set_sip_bridge_call_state(
            hass,
            CallState.IDLE.value,
            caller=(session.caller if session is not None else ""),
            callee=(session.callee if session is not None else ""),
            peer_name=(session.caller if session is not None else ""),
            call_id=call_id,
            reason=reason,
            terminal_reason=reason,
            origin="self",
            last_sip_event="BYE",
            route_kind=GROUP_TYPE_CONFERENCE,
        )
        registry.finish_and_pop(
            call_id,
            reason=reason,
            state=CallState.IDLE.value,
        )

    def _on_registration_change(username: str, registered: bool) -> None:
        from .phone_endpoint import EndpointAvailability

        endpoint_registry = hass.data.get(DOMAIN, {}).get("endpoint_registry")
        endpoint = (
            endpoint_registry.by_username(username)
            if endpoint_registry is not None
            else None
        )
        if (
            endpoint is not None
            and endpoint.availability is not EndpointAvailability.UNAVAILABLE
        ):
            endpoint_registry.update(
                endpoint.endpoint_id,
                availability=(
                    EndpointAvailability.AVAILABLE
                    if registered
                    else EndpointAvailability.OFFLINE
                ),
            )
        create_runtime_task(hass, _refresh_and_push_phonebook(hass))

    registrar = SipRegistrar(
        enabled=bool(cfg.get(CONF_REGISTRAR_ENABLED, False)),
        accounts=_sip_accounts(hass),
        local_ip=local_ip,
        local_sip_port=int(cfg["sip_port"]),
        on_registration_change=_on_registration_change,
    )
    bucket = hass.data.setdefault(DOMAIN, {})
    bucket["sip_registrar"] = registrar
    registry = _call_registry(hass)
    pbx_runtime = SipEndpointRuntime(projection=registry)
    pbx_runtime.attach_component("registrar", registrar)
    route_resolver = EndpointRouteResolver(
        hass=hass,
        local_ip=local_ip,
        sip_port=int(cfg["sip_port"]),
    )
    _is_ha_target = route_resolver.is_ha_target
    _ha_router_decision = route_resolver.route
    _is_local_listener_uri = route_resolver.is_local_listener_uri
    _logical_endpoint_for_member = route_resolver.logical_endpoint

    def _attach_client_media_update(
        client: SipCallClient,
        relay,
        *,
        source_call_id: str,
    ) -> None:
        """Bind remote re-offers on an outbound dialog to its live relay leg."""

        async def _prepare(previous, updated, method):
            registry = _call_registry(hass)
            session = registry.sessions.get(
                registry.resolve_session_id(source_call_id)
            )
            if session is None:
                return None
            call_generation = session.generation
            try:
                previous_audio_peer = relay.right
                commit_audio = relay.prepare_peer_reconfiguration(
                    "right", dialog_rtp_peer(updated)
                )
            except (TypeError, ValueError):
                return None

            video_relay = getattr(relay, "video_relay", None)
            previous_video_peer = (
                video_relay.right if video_relay is not None else None
            )
            previous_video = previous.video_format
            updated_video = updated.video_format
            if (previous_video is None) != (updated_video is None):
                return None
            next_video_peer = None
            commit_video = None
            if updated_video is not None:
                next_video_peer = dialog_video_rtp_peer(updated)
                if (
                    video_relay is None
                    or not video_formats_passthrough_compatible(
                        video_relay.left.recv_format,
                        next_video_peer.send_format,
                    )
                    or not video_formats_passthrough_compatible(
                        next_video_peer.recv_format,
                        video_relay.left.send_format,
                    )
                    or updated.remote_video_rtp_port <= 0
                ):
                    return None
                commit_video = video_relay.prepare_peer_reconfiguration(
                    "right", next_video_peer
                )

            async def _commit() -> None:
                if not registry.is_generation_current(
                    source_call_id, call_generation
                ):
                    raise RuntimeError(
                        "SIP bridge media update belongs to a terminated call"
                    )
                if relay.right is not previous_audio_peer or (
                    video_relay is not None
                    and previous_video_peer is not None
                    and video_relay.right is not previous_video_peer
                ):
                    raise RuntimeError(
                        "SIP bridge media owner changed before commit"
                    )
                commit_audio()
                if commit_video is not None:
                    commit_video()
                _LOGGER.info(
                    "SIP bridge outbound %s committed source_call_id=%s dest_call_id=%s remote_rtp=%s:%s audio_direction=%s video_direction=%s",
                    method,
                    source_call_id,
                    client.dialog_ids.call_id,
                    updated.remote_rtp_host,
                    updated.remote_rtp_port,
                    updated.remote_audio_direction,
                    updated_video.direction
                    if updated_video is not None
                    else "inactive",
                )

            return _commit

        client.on_media_update = _prepare

    async def _on_register(request, addr, transport):
        result = await registrar.handle_register(request, addr, transport)
        if 200 <= int(result.status) < 300:
            await _refresh_and_push_phonebook(hass)
        return result

    async def _on_info(request, addr, transport) -> None:
        digit = parse_sip_info_digit(request.header("Content-Type"), request.body)
        if not digit:
            _LOGGER.info(
                "SIP INFO ignored call_id=%s content_type=%s",
                request.header("Call-ID"),
                request.header("Content-Type") or "-",
            )
            return
        call_id = request.header("Call-ID")
        queue = (
            hass.data.setdefault(DOMAIN, {})
            .setdefault("trunk_info_queues", {})
            .get(call_id)
        )
        if queue is None:
            registry = _call_registry(hass)
            relay = registry.relays.get(call_id)
            callback = getattr(relay, "on_dtmf", None)
            if callback is not None:
                callback("left", digit, "sip_info")
                _LOGGER.info(
                    "SIP in-call INFO DTMF RX call_id=%s digit=%s transport=%s",
                    call_id,
                    digit,
                    transport,
                )
                return
            if relay is not None or call_id in registry.softphone_media:
                session = registry.sessions.get(registry.resolve_session_id(call_id))
                _publish_dtmf_event(
                    hass,
                    call_id=call_id,
                    dest_call_id=registry.bridge_clients.get(call_id, ""),
                    caller=session.caller if session is not None else "",
                    callee=session.callee if session is not None else "",
                    side="left",
                    digit=digit,
                    transport="sip_info",
                )
                _LOGGER.info(
                    "SIP local in-call INFO DTMF RX call_id=%s digit=%s transport=%s",
                    call_id,
                    digit,
                    transport,
                )
                return
            _LOGGER.info(
                "SIP INFO DTMF arrived outside active call call_id=%s digit=%s",
                call_id,
                digit,
            )
            return
        if queue.full():
            _LOGGER.warning(
                "SIP INFO DTMF queue full call_id=%s; digit ignored", call_id
            )
            return
        queue.put_nowait(digit)
        _LOGGER.info(
            "SIP trunk INFO DTMF RX call_id=%s digit=%s transport=%s",
            call_id,
            digit,
            transport,
        )

    def _is_trunk_invite(invite: SipInvite) -> bool:
        trunk_cfg = _get_trunk_config(hass)
        trunk = hass.data.get(DOMAIN, {}).get("sip_trunk")
        return bool(
            _trunk_enabled(trunk_cfg)
            and invite.received_via_trunk
            and getattr(trunk, "registered", False)
        )

    async def _start_local_assist_bridge(
        invite: SipInvite,
        *,
        reservation: RtpPortReservation,
        local_rtp_port: int,
        roster_entries: list[RosterEntry],
        source: str,
        called_extension: str,
        release_reservation_on_failure: bool = True,
    ):
        from .assist_runtime import AssistMediaSession, build_call_connected_intent

        assist_cfg = hass.data.setdefault(DOMAIN, {}).get("assist_config", {})
        caller_entry = _roster_entry_for_target(invite.caller, roster_entries)
        if caller_entry is None and invite.caller_uri is not None:
            caller_entry = _roster_entry_for_target(
                invite.caller_uri.user, roster_entries
            )
        if caller_entry is None:
            caller_token = str(invite.caller or "").strip()
            caller_entry = next(
                (
                    entry
                    for entry in roster_entries
                    if caller_token and str(entry.number or "").strip() == caller_token
                ),
                None,
            )
        caller_id = str(
            (invite.caller_uri.user if invite.caller_uri is not None else "")
            or invite.caller
            or invite.source_host
            or "Unknown"
        ).strip()
        caller_name = (
            str(caller_entry.name or caller_entry.id).strip()
            if caller_entry is not None
            else str(invite.caller or caller_id or "Unknown").strip()
        )
        caller_uri = str(invite.caller_uri) if invite.caller_uri is not None else ""
        destination_name = str(assist_cfg.get("name") or "Assist").strip() or "Assist"
        assist_leg_id = f"assist:{invite.call_id}"
        registry = _call_registry(hass)
        existing_session = registry.sessions.get(
            registry.resolve_session_id(invite.call_id)
        )
        existing_metadata = (
            existing_session.metadata if existing_session is not None else {}
        )
        call_ingress = canonical_call_origin(
            existing_metadata.get("ingress")
            or existing_metadata.get("origin")
            or ("trunk" if invite.received_via_trunk or source == "trunk" else source),
            existing_session.route_kind if existing_session is not None else "",
        )

        async def _complete(reason: str) -> None:
            await _terminate_sip_bridge(
                hass,
                invite.call_id,
                terminal_reason=reason or TerminalReason.PROTOCOL_ERROR.value,
            )

        media = AssistMediaSession(
            hass,
            invite=invite,
            local_rtp_port=local_rtp_port,
            reservation=reservation,
            pipeline_id=str(assist_cfg.get(CONF_ASSIST_PIPELINE) or "preferred"),
            call_connected_intent=build_call_connected_intent(
                caller=caller_name,
                caller_id=caller_id,
                caller_in_phonebook=caller_entry is not None,
                source=source,
                called_extension=called_extension,
                include_advanced_context=bool(
                    assist_cfg.get(CONF_ASSIST_ADVANCED_CALL_CONTEXT, False)
                ),
            ),
            on_complete=_complete,
        )
        try:
            await media.start()
        except BaseException:
            if release_reservation_on_failure:
                reservation.release()
            raise

        registry.bridge_clients[invite.call_id] = assist_leg_id
        registry.upsert(
            invite.call_id,
            state=CallState.IN_CALL.value,
            owner="assist",
            caller=caller_name,
            callee=destination_name,
            route_kind=RouteAction.ASSIST.value,
            ingress=call_ingress,
            origin=call_ingress,
        )
        registry.attach_relay(invite.call_id, media)
        registry.add_leg(
            invite.call_id,
            invite.call_id,
            role="trunk" if call_ingress == "trunk" else "caller",
            state=CallState.IN_CALL.value,
        )
        registry.add_leg(
            invite.call_id,
            assist_leg_id,
            role="assist",
            state=CallState.IN_CALL.value,
        )
        _set_sip_bridge_call_state(
            hass,
            CallState.IN_CALL.value,
            caller=caller_name,
            callee=destination_name,
            peer_name=destination_name,
            call_id=invite.call_id,
            dest_call_id=assist_leg_id,
            direction="incoming",
            route_kind=RouteAction.ASSIST.value,
            ingress=call_ingress,
            origin=call_ingress,
            selected_tx_format=invite.send_format.audio_format.wire_token(),
            selected_rx_format=invite.recv_format.audio_format.wire_token(),
            selected_tx_rtp_format=invite.send_format.wire_token(),
            selected_rx_rtp_format=invite.recv_format.wire_token(),
            audio_direction=invite.local_audio_direction,
            audio_connection_held=invite.remote_audio_connection_held,
            sip_status_code=200,
            last_sip_event="ASSIST_PIPELINE",
            caller_uri=caller_uri,
            source=source,
        )
        return media

    def _sip_uri_for_member(member: str, peers: list[Peer], entries: list[RosterEntry]):
        peer = _peer_for_target(member, peers)
        if peer is not None and peer.host:
            sip_transport = str(
                (peer.device or {}).get("sip_transport") or "tcp"
            ).lower()
            if sip_transport not in {"tcp", "udp"}:
                sip_transport = "tcp"
            return (
                parse_sip_uri(
                    f"sip:{member}@{peer.host}:{peer.sip_port or cfg['sip_port']};transport={sip_transport}"
                ),
                peer,
                None,
            )
        entry = _roster_entry_for_target(member, entries)
        if entry is None:
            return None, None, None
        if entry.sip_uri:
            return parse_sip_uri(entry.sip_uri), None, entry
        if not entry.metadata.get("local_ha") and entry.address:
            bridge_port = int(
                entry.port
                or (entry.metadata or {}).get("port")
                or (entry.metadata or {}).get("sip_port")
                or cfg["sip_port"]
            )
            return (
                parse_sip_uri(f"sip:{entry.id}@{entry.address}:{bridge_port}"),
                None,
                entry,
            )
        return None, None, entry

    def _browser_leg_for_member(
        member: str,
        peers: list[Peer],
        entries: list[RosterEntry],
    ) -> BrowserLeg | None:
        endpoint = _logical_endpoint_for_member(member, peers, entries)
        if endpoint is not None:
            if endpoint.kind is not EndpointKind.BROWSER:
                return None
            return BrowserLeg(
                member=member,
                endpoint_id=endpoint.endpoint_id,
                name=endpoint.name,
                device_id=str(endpoint.device_id or HA_SOFTPHONE_DEVICE_ID),
            )
        # Preserve the pre-registry/YAML-only master-phone compatibility path.
        if _is_ha_target(member):
            return BrowserLeg(
                member=member,
                endpoint_id=DEFAULT_ENDPOINT_ID,
                name=_ha_peer_name(hass),
                device_id=HA_SOFTPHONE_DEVICE_ID,
            )
        return None

    def _prepare_outbound_leg(
        *,
        member: str,
        peers: list[Peer],
        roster_entries: list[RosterEntry],
        local_name: str,
        local_rtp_port_index: int,
        uri_override: str = "",
        endpoint_id_override: str = "",
        candidate_id: str = "",
        tier: int = 0,
        order: int = 0,
        invite: SipInvite | None = None,
    ) -> OutboundLeg | None:
        resolved_uri, peer_target, member_entry = _sip_uri_for_member(
            member, peers, roster_entries
        )
        uri = parse_sip_uri(uri_override) if uri_override else resolved_uri
        if uri is None or _is_local_listener_uri(uri):
            return None
        ports = RtpPortReservation.allocate(hass)
        try:
            remote_tx_formats = _peer_audio_formats(
                peer_target, "tx_formats"
            ) or _roster_entry_formats(member_entry, "tx_formats")
            remote_rx_formats = _peer_audio_formats(
                peer_target, "rx_formats"
            ) or _roster_entry_formats(member_entry, "rx_formats")
            sip_send_formats, sip_recv_formats = _sip_target_audio_profile(
                remote_tx_formats=remote_tx_formats,
                remote_rx_formats=remote_rx_formats,
                target=member,
            )
            bridge_to_softphone = bool(
                member_entry is not None
                and member_entry.sip_uri
                and member_entry.metadata.get("registered")
            )
            if bridge_to_softphone:
                sip_send_formats = list(HA_TRUNK_AUDIO_FORMATS)
                sip_recv_formats = list(HA_TRUNK_AUDIO_FORMATS)
            target_endpoint = _logical_endpoint_for_member(
                member, peers, roster_entries
            )
            video_relay = None
            video_failure_reason = ""
            if (
                invite is not None
                and invite.video_format is not None
                and bool(cfg.get(CONF_SIP_VIDEO, False))
                and (
                    target_endpoint is None
                    or target_endpoint.supports("video")
                )
            ):
                video_reservation = None
                sockets = ()
                try:
                    video_reservation, sockets = reserve_sip_video_relay_media(hass)
                    source_video_port, destination_video_port = (
                        video_reservation.ports
                    )
                    video_relay = build_pending_invite_video_relay(
                        invite,
                        remote_host=str(uri.host),
                        left_port=source_video_port,
                        right_port=destination_video_port,
                        sockets=sockets,
                        on_release=lambda reserved: _release_sip_rtp_port_pair(
                            hass, reserved
                        ),
                    )
                    # The relay now owns all reserved sockets and both ports.
                    video_reservation.detach()
                except (OSError, RuntimeError) as err:
                    for sock in sockets:
                        sock.close()
                    if video_reservation is not None:
                        video_reservation.release()
                    video_relay = None
                    video_failure_reason = "local_video_resources_unavailable"
                    _LOGGER.warning(
                        "SIP fork video reservation unavailable member=%s; "
                        "continuing audio-only: %s",
                        member,
                        err,
                    )
            client = SipCallClient(
                local_ip=local_ip,
                local_name=local_name,
                local_sip_port=int(cfg["sip_port"]),
                local_rtp_port=ports.ports[local_rtp_port_index],
                supported_send_formats=sip_send_formats,
                supported_recv_formats=sip_recv_formats,
                signaling_transport=_sip_uri_transport(uri),
                include_common_codecs=bridge_to_softphone,
                local_video_rtp_port=(
                    video_relay.right_port if video_relay is not None else 0
                ),
                video_formats=(
                    (invite.video_format,)
                    if video_relay is not None and invite is not None
                    else ()
                ),
                video_direction=(
                    invite.video_format.direction
                    if video_relay is not None and invite is not None
                    else "inactive"
                ),
                generic_video_relay=video_relay is not None,
            )
            _enable_reused_sip_tcp_connection(
                hass,
                client,
                uri,
                target=member,
                default_sip_port=int(cfg["sip_port"]),
            )
            return OutboundLeg(
                member=member,
                uri=uri,
                client=client,
                ports=ports,
                bridge_to_softphone=bridge_to_softphone,
                endpoint_id=str(
                    endpoint_id_override
                    or getattr(target_endpoint, "endpoint_id", "")
                    or ""
                ),
                candidate_id=candidate_id,
                tier=int(tier),
                order=int(order),
                video_relay=video_relay,
                video_failure_reason=video_failure_reason,
            )
        except Exception:
            if "video_relay" in locals() and video_relay is not None:
                # Construction runs inside the endpoint event loop; transfer
                # rollback to its tracked cleanup task.
                create_runtime_task(hass, video_relay.stop())
            ports.release()
            raise

    def _publish_pending_ha_softphone_ringing(
        invite: SipInvite,
        *,
        route_kind: str,
        endpoint_id: str,
        endpoint_device_id: str,
        callee: str,
        sip_uri: str | None = None,
        last_sip_event: str = "INVITE",
    ) -> None:
        """Project one pending SIP dialog onto its owning browser phone."""
        registry = _call_registry(hass)
        endpoint_registry = hass.data.get(DOMAIN, {}).get("endpoint_registry")
        endpoint = (
            endpoint_registry.get(endpoint_id)
            if endpoint_registry is not None
            else None
        )
        video_enabled = bool(
            invite.video_format is not None
            and (endpoint is None or endpoint.supports("video"))
        )
        _set_ha_softphone_call_state(
            hass,
            CallState.RINGING.value,
            endpoint_id=endpoint_id,
            session_device_id=endpoint_device_id,
            caller=invite.caller,
            callee=callee,
            peer_name=invite.caller,
            direction="incoming",
            call_id=invite.call_id,
            dialed_target=invite.target,
            selected_tx_format=invite.send_format.audio_format.wire_token(),
            selected_rx_format=invite.recv_format.audio_format.wire_token(),
            selected_tx_rtp_format=invite.send_format.wire_token(),
            selected_rx_rtp_format=invite.recv_format.wire_token(),
            audio_mode="full_duplex",
            route_kind=route_kind,
            sip_uri=sip_uri,
            sip_status_code=(
                200 if invite.call_id in registry.preanswered else 180
            ),
            last_sip_event=last_sip_event,
            video_offered=video_enabled,
            video_format=(
                invite.video_format.wire_token() if video_enabled else ""
            ),
            video_send_format=(
                invite.send_video_format.wire_token()
                if video_enabled and invite.send_video_format is not None
                else ""
            ),
            video_receive_format=(
                invite.recv_video_format.wire_token()
                if video_enabled and invite.recv_video_format is not None
                else ""
            ),
        )

    def _defer_invite_to_ha_softphone(
        invite: SipInvite,
        *,
        route_kind: str,
        endpoint_id: str = DEFAULT_ENDPOINT_ID,
        endpoint_device_id: str = HA_SOFTPHONE_DEVICE_ID,
        callee: str | None = None,
        sip_uri: str | None = None,
        last_sip_event: str = "INVITE",
    ) -> None:
        registry = _call_registry(hass)
        registry.pending_invites[invite.call_id] = invite
        session = registry.upsert(
            invite.call_id,
            state=CallState.RINGING.value,
            caller=invite.caller,
            callee=callee or invite.target,
            route_kind=route_kind,
            owner="ha_softphone",
            endpoint_id=endpoint_id,
            session_device_id=endpoint_device_id,
            dialed_target=invite.target,
            ingress="trunk" if invite.received_via_trunk else "extension",
            origin="trunk" if invite.received_via_trunk else "extension",
        )
        registry.claim_endpoint(
            invite.call_id,
            endpoint_id,
            role="destination",
        )
        registry.add_leg(
            invite.call_id,
            invite.call_id,
            role="ha_softphone",
            state=CallState.RINGING.value,
        )
        expected_revision = session.revision

        def _publish_ringing_if_current() -> None:
            if not registry.is_current(
                invite.call_id,
                revision=expected_revision,
                owner="ha_softphone",
            ):
                _LOGGER.debug(
                    "Ignoring stale HA ringing callback for call %s revision %s",
                    invite.call_id,
                    expected_revision,
                )
                return
            _publish_pending_ha_softphone_ringing(
                invite,
                route_kind=route_kind,
                endpoint_id=endpoint_id,
                endpoint_device_id=endpoint_device_id,
                callee=callee or invite.target,
                sip_uri=sip_uri,
                last_sip_event=last_sip_event,
            )

        hass.loop.call_soon(_publish_ringing_if_current)

    def _inbound_route_decision(
        invite: SipInvite, peers: list[Peer], entries: list[RosterEntry]
    ):
        # Once an INVITE reached HA, HA is the router. ESP-origin direct-vs-HA
        # decisions are made before dialing by the ESP phonebook mirror.
        # ``HA`` is the stable config-flow alias; the phonebook entry carries
        # the user-selected HA peer name (for example ``Casa``). Resolve the
        # alias before consulting the canonical phonebook dial plan.
        target = _ha_peer_name(hass) if _is_ha_target(invite.target) else invite.target
        return _ha_router_decision(target, entries)

    async def _run_trunk_inbound_route(
        invite: SipInvite,
        *,
        bridge_ports: RtpPortReservation,
    ) -> None:
        source_relay_port, dest_relay_port = bridge_ports.ports
        bucket = hass.data.setdefault(DOMAIN, {})
        registry = _call_registry(hass)
        trunk_cfg = _get_trunk_config(hass)
        dtmf_timeout_ms = max(0, int(trunk_cfg.get(CONF_TRUNK_DTMF_TIMEOUT_MS) or 0))
        destination = ""
        digits = ""
        automation_decision: dict = {}
        peers = await _async_build_peer_snapshot(hass)
        roster_entries = _roster_from_peers(
            hass, peers, _registered_roster_entries(hass)
        )
        routes = _dtmf_extension_routes(roster_entries)
        if trunk_cfg.get(CONF_TRUNK_DTMF_ENABLED) and dtmf_timeout_ms > 0:
            timeout = float(dtmf_timeout_ms) / 1000.0
            terminator = str(trunk_cfg.get(CONF_TRUNK_DTMF_TERMINATOR) or "")
            info_queue = bucket.setdefault("trunk_info_queues", {}).setdefault(
                invite.call_id,
                asyncio.Queue(maxsize=MAX_TRUNK_INFO_DIGITS),
            )
            selection = await _collect_trunk_dtmf(
                invite,
                info_queue=info_queue,
                source_rtp_port=source_relay_port,
                routes=routes,
                timeout=timeout,
                terminator=terminator,
            )
            digits = selection.digits
            destination = selection.destination
        # A source BYE must win before any no-digits automation window is
        # opened. Otherwise a cancelled pre-answer call can emit one stale
        # route_requested occurrence when its DTMF timer expires.
        if invite.call_id in bucket.get("trunk_closed_calls", set()):
            bucket["trunk_closed_calls"].discard(invite.call_id)
            bucket.setdefault("trunk_info_queues", {}).pop(invite.call_id, None)
            _LOGGER.info(
                "SIP trunk inbound call_id=%s closed during DTMF collection",
                invite.call_id,
            )
            bridge_ports.release()
            return

        # Explicit digits always select the canonical phonebook route. Only
        # the no-digits fallback may be overridden by an automation.
        if not digits and trunk_cfg.get(CONF_AUTOMATION_ROUTING_ENABLED):
            automation_decision = await _request_inbound_destination(
                hass,
                invite,
                trunk_config=trunk_cfg,
                timeout=SIP_ROUTE_DECISION_TIMEOUT,
            )
        bucket.setdefault("trunk_info_queues", {}).pop(invite.call_id, None)

        if invite.call_id in bucket.get("trunk_closed_calls", set()):
            bucket["trunk_closed_calls"].discard(invite.call_id)
            _LOGGER.info(
                "SIP trunk inbound call_id=%s closed before routing", invite.call_id
            )
            bridge_ports.release()
            return

        automation_action = str(automation_decision.get("action") or "").strip().lower()
        if automation_action in {"forward", "bridge"}:
            automation_destination = str(
                automation_decision.get("destination") or ""
            ).strip()
            _LOGGER.info(
                "Inbound route selected call_id=%s source=automation destination=%s fallback=%s",
                invite.call_id,
                automation_destination or "-",
                str(trunk_cfg.get(CONF_TRUNK_INBOUND_DEFAULT_TARGET) or "HA").strip()
                or "HA",
            )
            await _async_forward_existing_call(
                call_id=invite.call_id,
                destination=automation_destination,
                on_failure="resume",
                initial_selection=True,
            )
            return
        if automation_action in {"decline", "busy", "cancel"}:
            registry = _call_registry(hass)
            registry.pending_invites.pop(invite.call_id, None)
            preanswered = registry.take_media(invite.call_id, provisional=True)
            _release_media_reservation(preanswered)
            status = 486 if automation_action == "busy" else 603
            reason = (
                TerminalReason.BUSY.value
                if automation_action == "busy"
                else TerminalReason.CANCELLED.value
                if automation_action == "cancel"
                else TerminalReason.DECLINED.value
            )
            if bool((preanswered or {}).get("final_response_sent", True)):
                _sip_send_bye(hass, invite.call_id)
            else:
                _sip_send_final_response(hass, invite.call_id, status, "Busy Here" if status == 486 else "Decline")
            bridge_ports.release()
            _set_sip_bridge_call_state(
                hass,
                CallState.BUSY.value
                if automation_action == "busy"
                else CallState.DECLINED.value,
                caller=invite.caller,
                callee=invite.target,
                peer_name=invite.caller,
                call_id=invite.call_id,
                direction="incoming",
                reason=reason,
                terminal_reason=reason,
                origin="automation",
                sip_status_code=status,
                last_sip_event="BYE",
            )
            registry.finish_and_pop(invite.call_id, reason=reason)
            return

        default_target = _trunk_default_target(trunk_cfg)
        route_hint = destination or digits
        _LOGGER.info(
            "Inbound route selected call_id=%s source=%s destination=%s fallback=%s",
            invite.call_id,
            "dtmf" if digits else "fallback",
            route_hint or default_target,
            default_target,
        )
        decision = route_inbound_trunk(
            CallContext(
                call_id=invite.call_id,
                direction="inbound",
                origin="trunk",
                caller=invite.caller,
                route_hint=route_hint,
                source_host=invite.source_host,
            ),
            roster_entries,
            trunk_ready=False,
        )
        if decision.action is RouteAction.ANSWER_HA:
            # ANSWER_HA identifies the endpoint *kind*, not necessarily the
            # default browser phone. Preserve an explicit DTMF extension so a
            # second routing pass can select the corresponding logical HA
            # softphone (for example 667 -> Test).
            destination = route_hint or decision.target or default_target
        elif decision.action is RouteAction.REJECT:
            registry = _call_registry(hass)
            registry.pending_invites.pop(invite.call_id, None)
            preanswered = registry.take_media(invite.call_id, provisional=True)
            _release_media_reservation(preanswered)
            terminal_reason = RouteReason.ROUTE_NOT_FOUND.value
            _LOGGER.info(
                "SIP trunk route not found call_id=%s digits=%s hint=%s",
                invite.call_id,
                digits or "-",
                route_hint or "-",
            )
            if bool((preanswered or {}).get("final_response_sent", True)):
                _sip_send_bye(hass, invite.call_id)
            else:
                _sip_send_final_response(hass, invite.call_id, 404, "Not Found")
            _set_sip_bridge_call_state(
                hass,
                CallState.TRANSPORT_UNREACHABLE.value,
                caller=invite.caller,
                callee=route_hint or default_target,
                peer_name=invite.caller,
                call_id=invite.call_id,
                direction="incoming",
                reason=terminal_reason,
                terminal_reason=terminal_reason,
                origin="self",
                sip_status_code=404,
                last_sip_event="BYE",
            )
            bridge_ports.release()
            registry.finish_and_pop(
                invite.call_id,
                reason=terminal_reason,
                state=CallState.TRANSPORT_UNREACHABLE.value,
            )
            return
        else:
            destination = decision.target or route_hint or default_target
        _LOGGER.info(
            "SIP trunk inbound route call_id=%s caller=%s digits=%s destination=%s tx=%s rx=%s",
            invite.call_id,
            invite.caller or invite.source_host,
            digits or "-",
            destination,
            invite.send_format.wire_token(),
            invite.recv_format.wire_token(),
        )

        if decision.action is RouteAction.ASSIST:
            registry = _call_registry(hass)
            registry.pending_invites.pop(invite.call_id, None)
            preanswered = registry.take_media(invite.call_id, provisional=True)
            _release_video_media_reservation(preanswered)
            try:
                await _start_local_assist_bridge(
                    invite,
                    reservation=bridge_ports,
                    local_rtp_port=source_relay_port,
                    roster_entries=roster_entries,
                    source="trunk",
                    called_extension=digits or route_hint,
                )
                if not bool((preanswered or {}).get("final_response_sent", True)):
                    _sip_send_final_response(
                        hass,
                        invite.call_id,
                        200,
                        "OK",
                        answer_sdp=str((preanswered or {}).get("early_answer_sdp") or ""),
                    )
            except Exception as err:
                _LOGGER.exception(
                    "SIP trunk Assist bridge failed call_id=%s", invite.call_id
                )
                if bool((preanswered or {}).get("final_response_sent", True)):
                    _sip_send_bye(hass, invite.call_id)
                else:
                    _sip_send_final_response(
                        hass, invite.call_id, 488, "Not Acceptable Here"
                    )
                _set_sip_bridge_call_state(
                    hass,
                    CallState.MEDIA_INCOMPATIBLE.value,
                    caller=invite.caller,
                    callee=destination,
                    call_id=invite.call_id,
                    direction="incoming",
                    reason=str(err),
                    terminal_reason=TerminalReason.MEDIA_INCOMPATIBLE.value,
                    origin="self",
                    sip_status_code=488,
                    last_sip_event="BYE",
                )
            return

        if _is_ha_target(destination):
            _defer_invite_to_ha_softphone(
                invite,
                route_kind="trunk",
                endpoint_id=DEFAULT_ENDPOINT_ID,
                endpoint_device_id=HA_SOFTPHONE_DEVICE_ID,
                callee=_ha_peer_name(hass),
                last_sip_event="DTMF_ROUTE",
            )
            return

        decision = _ha_router_decision(
            destination,
            _roster_from_peers(hass, peers, _registered_roster_entries(hass)),
        )
        if decision.action is RouteAction.ANSWER_HA:
            roster = _roster_from_peers(
                hass, peers, _registered_roster_entries(hass)
            )
            target_endpoint = _logical_endpoint_for_member(
                decision.target or destination,
                peers,
                roster,
            )
            session = registry.sessions.get(
                registry.resolve_session_id(invite.call_id)
            )
            current_endpoint_id = str(
                ((session.metadata if session is not None else {}) or {}).get(
                    "endpoint_id"
                )
                or DEFAULT_ENDPOINT_ID
            ).strip()
            if (
                target_endpoint is not None
                and target_endpoint.kind is EndpointKind.BROWSER
                and target_endpoint.endpoint_id == current_endpoint_id
            ):
                # The pre-answered trunk dialog is initially parked on the
                # master HA phone. Routing 666/default back to that same phone
                # is an assignment, not a forward. Treating it as a forward
                # trips the loop guard and leaves the async route task failed
                # while the caller remains answered but unowned.
                _defer_invite_to_ha_softphone(
                    invite,
                    route_kind="trunk",
                    endpoint_id=target_endpoint.endpoint_id,
                    endpoint_device_id=(
                        target_endpoint.device_id or HA_SOFTPHONE_DEVICE_ID
                    ),
                    callee=target_endpoint.name,
                    last_sip_event="DTMF_ROUTE",
                )
                return
            # DTMF extensions are canonical phonebook destinations, including
            # additional browser softphones. Reuse the normal forwarding path
            # so endpoint ownership, offline policy and the selected device
            # are handled exactly like an automation forward.
            await _async_forward_existing_call(
                call_id=invite.call_id,
                destination=destination,
                on_failure="resume",
            )
            return
        registry = _call_registry(hass)
        registry.pending_invites.pop(invite.call_id, None)
        preanswered = registry.take_media(invite.call_id, provisional=True)
        _release_video_media_reservation(preanswered)
        peer_target = _peer_for_target(decision.target or destination, peers)
        bridge_uri = None
        try:
            if peer_target is not None and peer_target.host:
                sip_transport = str(
                    (peer_target.device or {}).get("sip_transport") or "tcp"
                ).lower()
                if sip_transport not in {"tcp", "udp"}:
                    sip_transport = "tcp"
                bridge_uri = parse_sip_uri(
                    f"sip:{decision.target or destination}@{peer_target.host}:{peer_target.sip_port or cfg['sip_port']};transport={sip_transport}"
                )
            elif decision.entry is not None and decision.entry.sip_uri:
                bridge_uri = parse_sip_uri(decision.entry.sip_uri)
            elif (
                decision.entry is not None
                and not decision.entry.metadata.get("local_ha")
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
            elif decision.sip_uri:
                bridge_uri = parse_sip_uri(decision.sip_uri)
        except Exception as err:
            _LOGGER.info(
                "SIP trunk route parse failed destination=%s: %s", destination, err
            )

        if bridge_uri is None or _is_local_listener_uri(bridge_uri):
            _LOGGER.info(
                "SIP trunk destination unresolved destination=%s route=%s",
                destination,
                decision.action.value,
            )
            _sip_send_bye(hass, invite.call_id)
            _set_sip_bridge_call_state(
                hass,
                CallState.TRANSPORT_UNREACHABLE.value,
                caller=invite.caller,
                callee=destination,
                peer_name=invite.caller,
                call_id=invite.call_id,
                reason=TerminalReason.TRANSPORT_UNREACHABLE.value,
                terminal_reason=TerminalReason.TRANSPORT_UNREACHABLE.value,
                origin="self",
                sip_status_code=404,
                last_sip_event="BYE",
            )
            bridge_ports.release()
            return

        peer_target = _peer_for_target(destination, peers)
        remote_tx_formats = _peer_audio_formats(
            peer_target, "tx_formats"
        ) or _roster_entry_formats(decision.entry, "tx_formats")
        remote_rx_formats = _peer_audio_formats(
            peer_target, "rx_formats"
        ) or _roster_entry_formats(decision.entry, "rx_formats")
        sip_send_formats, sip_recv_formats = _sip_target_audio_profile(
            remote_tx_formats=remote_tx_formats,
            remote_rx_formats=remote_rx_formats,
            target=destination,
        )
        client = SipCallClient(
            local_ip=local_ip,
            local_name=invite.caller or _ha_peer_name(hass),
            local_sip_port=int(cfg["sip_port"]),
            local_rtp_port=dest_relay_port,
            supported_send_formats=sip_send_formats,
            supported_recv_formats=sip_recv_formats,
            signaling_transport=_sip_uri_transport(bridge_uri),
        )
        _enable_reused_sip_tcp_connection(
            hass,
            client,
            bridge_uri,
            target=destination,
            default_sip_port=int(cfg["sip_port"]),
        )
        result = await client.invite(
            target=bridge_uri.user,
            remote_host=bridge_uri.host,
            remote_sip_port=bridge_uri.port or int(cfg["sip_port"]),
            request_uri=str(bridge_uri),
        )
        if result == "ringing":
            result = await client.wait_for_final()
        if result != "in_call" or client.dialog is None:
            _LOGGER.info(
                "SIP trunk destination failed destination=%s result=%s",
                destination,
                result,
            )
            await _close_client_and_release(client, bridge_ports)
            _sip_send_bye(hass, invite.call_id)
            public_result = _sip_public_state(result)
            terminal_reason = _sip_terminal_reason(result, public_result)
            _set_sip_bridge_call_state(
                hass,
                public_result,
                caller=invite.caller,
                callee=destination,
                peer_name=invite.caller,
                call_id=invite.call_id,
                dest_call_id=client.dialog_ids.call_id,
                reason=terminal_reason,
                terminal_reason=terminal_reason,
                origin="remote",
                sip_status_code=client.last_sip_status_code,
                last_sip_event=client.last_sip_event or "BYE",
            )
            return
        _LOGGER.info(
            "SIP trunk bridge media call_id=%s trunk_tx=%s trunk_rx=%s destination_tx=%s destination_rx=%s",
            invite.call_id,
            invite.send_format.wire_token(),
            invite.recv_format.wire_token(),
            client.dialog.send_format.wire_token(),
            client.dialog.recv_format.wire_token(),
        )

        try:
            relay = build_invite_client_relay(
                invite=invite,
                client=client,
                source_relay_port=source_relay_port,
                dest_relay_port=dest_relay_port,
                debug_capture=_debug_mode(hass),
                on_release=lambda ports: _release_sip_rtp_port_pair(hass, ports),
            )
            _attach_dtmf_event_bridge(
                hass,
                relay,
                call_id=invite.call_id,
                dest_call_id=client.dialog_ids.call_id,
                caller=invite.caller,
                callee=destination,
                client=client,
            )
            await relay.start()
        except Exception as err:
            _LOGGER.warning("SIP trunk RTP bridge unavailable: %s", err)
            await _close_client_and_release(client, bridge_ports, bye=True)
            _sip_send_bye(hass, invite.call_id)
            _set_sip_bridge_call_state(
                hass,
                CallState.MEDIA_INCOMPATIBLE.value,
                caller=invite.caller,
                callee=destination,
                peer_name=invite.caller,
                call_id=invite.call_id,
                dest_call_id=client.dialog_ids.call_id,
                reason=TerminalReason.MEDIA_INCOMPATIBLE.value,
                terminal_reason=TerminalReason.MEDIA_INCOMPATIBLE.value,
                origin="self",
                sip_status_code=488,
                last_sip_event="BYE",
            )
            return

        registry = _call_registry(hass)
        registry.register_bridge(
            source_call_id=invite.call_id,
            dest_call_id=client.dialog_ids.call_id,
            client=client,
            state=CallState.IN_CALL.value,
            caller=invite.caller,
            callee=destination,
            route_kind="trunk",
            source_role="trunk",
        )
        _LOGGER.info(
            "SIP bridge registered call_id=%s dest_call_id=%s target=%s",
            invite.call_id,
            client.dialog_ids.call_id,
            bridge_uri.user,
        )
        _attach_client_media_update(
            client,
            relay,
            source_call_id=invite.call_id,
        )
        registry.attach_relay(invite.call_id, relay)
        bridge_ports.detach()
        _set_sip_bridge_call_state(
            hass,
            CallState.IN_CALL.value,
            caller=invite.caller,
            callee=destination,
            peer_name=destination,
            call_id=invite.call_id,
            dest_call_id=client.dialog_ids.call_id,
            selected_tx_format=invite.send_format.audio_format.wire_token(),
            selected_rx_format=invite.recv_format.audio_format.wire_token(),
            selected_tx_rtp_format=invite.send_format.wire_token(),
            selected_rx_rtp_format=invite.recv_format.wire_token(),
            audio_mode="full_duplex",
            route_kind="trunk",
            sip_status_code=200,
            last_sip_event="SIP_RESPONSE",
            sip_uri=str(bridge_uri),
            scope="sip_trunk",
            dtmf_digits=digits,
        )

    async def _async_forward_existing_call(
        *,
        call_id: str,
        destination: str,
        on_failure: str = "resume",
        expected_state: str = "",
        expected_sequence: int = 0,
        initial_selection: bool = False,
    ) -> None:
        """Route or move one HA-owned pending/ringing call to a target.

        ``initial_selection`` is used only by the bounded ``route_requested``
        decision point.  Unlike a later forward, it must not exclude browser
        phones from a ring group merely because the pre-answered trunk dialog
        is temporarily anchored on the default HA phone.
        """
        from homeassistant.exceptions import ServiceValidationError

        call_id = str(call_id or "").strip()
        destination = str(destination or "").strip()
        on_failure = str(on_failure or "resume").strip().lower()
        if not call_id or not destination:
            raise ServiceValidationError("call_id and destination are required")
        if on_failure not in {"resume", "terminate", "busy"}:
            raise ServiceValidationError(
                "on_failure must be resume, terminate, or busy"
            )

        registry = _call_registry(hass)
        context = registry.event_context(call_id)
        expected_state = str(expected_state or "").strip().lower()
        if expected_state and (context is None or context.state != expected_state):
            actual = context.state if context is not None else "ended"
            raise ServiceValidationError(
                f"call_id {call_id} is {actual}, expected {expected_state}"
            )
        if expected_sequence and (
            context is None or context.sequence != int(expected_sequence)
        ):
            actual = context.sequence if context is not None else 0
            raise ServiceValidationError(
                f"call_id {call_id} sequence is {actual}, expected {expected_sequence}"
            )
        if context is not None and len(context.route_history) >= 8:
            raise ServiceValidationError(f"call_id {call_id} exceeded 8 routing hops")

        invite = registry.pending_invites.get(call_id)
        if invite is None:
            raise ServiceValidationError(
                f"call_id {call_id} is not a forwardable pending or ringing HA-owned call"
            )
        forward_tasks = hass.data.setdefault(DOMAIN, {}).setdefault("forward_tasks", {})
        forward_claims = hass.data.setdefault(DOMAIN, {}).setdefault(
            "forward_claims", set()
        )
        current_forward = forward_tasks.get(call_id)
        if current_forward is not None and not current_forward.done():
            current_context = registry.event_context(call_id)
            if (
                current_context is None
                or current_context.state != CallState.REMOTE_RINGING.value
            ):
                raise ServiceValidationError(
                    f"call_id {call_id} is already being forwarded"
                )
            current_forward.cancel()
            await asyncio.gather(current_forward, return_exceptions=True)
        if call_id in forward_claims:
            raise ServiceValidationError(
                f"call_id {call_id} is already being forwarded"
            )
        forward_claims.add(call_id)
        target_browser_endpoint = None
        try:
            peers = await _async_build_peer_snapshot(hass)
            if registry.pending_invites.get(call_id) is not invite:
                raise ServiceValidationError(
                    f"call_id {call_id} changed while the route was being resolved"
                )
            context = registry.event_context(call_id)
            if expected_state and (context is None or context.state != expected_state):
                actual = context.state if context is not None else "ended"
                raise ServiceValidationError(
                    f"call_id {call_id} is {actual}, expected {expected_state}"
                )
            if expected_sequence and (
                context is None or context.sequence != int(expected_sequence)
            ):
                actual = context.sequence if context is not None else 0
                raise ServiceValidationError(
                    f"call_id {call_id} sequence is {actual}, expected {expected_sequence}"
                )
            roster_entries = _roster_from_peers(
                hass,
                peers,
                _registered_roster_entries(hass),
            )
            decision = _ha_router_decision(destination, roster_entries)
            endpoint_registry = hass.data.get(DOMAIN, {}).get("endpoint_registry")
            if decision.action is RouteAction.ANSWER_HA:
                target_browser_endpoint = _logical_endpoint_for_member(
                    decision.target or destination,
                    peers,
                    roster_entries,
                )
                if (
                    target_browser_endpoint is None
                    or target_browser_endpoint.kind is not EndpointKind.BROWSER
                ):
                    raise ServiceValidationError(
                        f"destination {destination} is not a configured Home Assistant phone"
                    )
            if decision.action is RouteAction.REJECT:
                raise ServiceValidationError(
                    f"destination {destination} is not a forwardable SIP dial-plan target"
                )
            if (
                decision.action is RouteAction.GROUP
                and str(
                    (
                        (decision.entry.metadata if decision.entry is not None else {})
                        or {}
                    ).get("group_type")
                    or ""
                )
                != GROUP_TYPE_RING
            ):
                raise ServiceValidationError(
                    "forwarding an already-ringing call is currently limited to ring groups"
                )

            last_route = (
                context.route_history[-1] if context and context.route_history else {}
            )
            if not (
                last_route.get("action") in {"forward", "bridge"}
                and last_route.get("destination") == destination
            ):
                registry.record_route(
                    call_id,
                    action="forward",
                    destination=destination,
                    source="automation",
                )
            session = registry.sessions.get(registry.resolve_session_id(call_id))
            session_endpoint_id = str(
                (session.metadata if session is not None else {}).get("endpoint_id")
                or DEFAULT_ENDPOINT_ID
            ).strip()
            if (
                not initial_selection
                and target_browser_endpoint is not None
                and target_browser_endpoint.endpoint_id == session_endpoint_id
            ):
                raise ServiceValidationError(
                    "a Home Assistant phone cannot forward a call to itself"
                )
            session_endpoint = (
                endpoint_registry.get(session_endpoint_id)
                if endpoint_registry is not None
                else None
            )
            session_device_id = str(
                getattr(session_endpoint, "device_id", "")
                or HA_SOFTPHONE_DEVICE_ID
            )
            original_callee = session.callee if session is not None else invite.target
            original_route_kind = session.route_kind if session is not None else ""
            # A trunk call is already SIP-answered while its DTMF/automation
            # route is being selected.  It has not populated the HA softphone
            # store yet, but HA is still its default owner.  Preserve that
            # ownership so ``on_failure: resume`` can enter normal ringing
            # instead of leaving the answered caller on silent RTP.
            ha_claimed = (
                bool(session is not None and session.owner == "ha_softphone")
                or call_id in registry.preanswered
                or bool(
                    session is not None and session.metadata.get("automation_resume_ha")
                )
            )
            if session is not None:
                session.metadata["automation_resume_ha"] = ha_claimed
                route_already_claimed = bool(
                    session.state == CallState.CONNECTING.value
                    and session.owner == "router"
                    and session.callee == destination
                )
                if not route_already_claimed:
                    claimed = registry.transition(
                        call_id,
                        state=CallState.CONNECTING.value,
                        owner="router",
                        callee=destination,
                        route_kind=decision.action.value,
                        expected_revision=session.revision,
                        expected_owner=session.owner,
                        automation_resume_ha=ha_claimed,
                    )
                    if claimed is None:
                        raise ServiceValidationError(
                            f"call_id {call_id} changed while forwarding ownership was claimed"
                        )
            else:
                route_already_claimed = False
            _release_ha_softphone_claim(
                hass,
                call_id,
                destination=destination,
                endpoint_id=session_endpoint_id,
            )
            if not route_already_claimed:
                _set_sip_bridge_call_state(
                    hass,
                    CallState.CONNECTING.value,
                    caller=invite.caller,
                    callee=destination,
                    peer_name=destination,
                    call_id=call_id,
                    direction="incoming",
                    route_source="automation",
                    route_kind=decision.action.value,
                    event_type="forwarding",
                    last_sip_event="ROUTE_FORWARD",
                )
        except Exception:
            forward_claims.discard(call_id)
            raise

        async def _restore_or_terminate(reason: str) -> None:
            preanswered = registry.preanswered.get(call_id)
            if on_failure == "resume" and call_id not in hass.data.setdefault(
                DOMAIN, {}
            ).get("trunk_closed_calls", set()):
                if session is not None:
                    current = registry.sessions.get(
                        registry.resolve_session_id(call_id)
                    )
                    if current is None or current.owner not in {
                        "router",
                        "bridge",
                        "assist",
                    }:
                        return
                    resumed = registry.transition(
                        call_id,
                        state=CallState.RINGING.value,
                        owner="ha_softphone",
                        callee=original_callee,
                        route_kind=original_route_kind,
                        expected_revision=current.revision,
                        expected_owner=current.owner,
                    )
                    if resumed is None:
                        return
                if ha_claimed:
                    _publish_pending_ha_softphone_ringing(
                        invite,
                        route_kind=original_route_kind,
                        endpoint_id=session_endpoint_id,
                        endpoint_device_id=session_device_id,
                        callee=original_callee,
                        last_sip_event="ROUTE_RESUME",
                    )
                return

            registry.pending_invites.pop(call_id, None)
            preanswered = registry.take_media(call_id, provisional=True)
            if preanswered is not None:
                _release_media_reservation(preanswered)
            if _source_dialog_is_answered(preanswered):
                _sip_send_bye(hass, call_id)
            else:
                status = 486 if on_failure == "busy" else 480
                _sip_send_final_response(
                    hass,
                    call_id,
                    status,
                    "Busy Here" if status == 486 else "Temporarily Unavailable",
                decline_reason=reason,
            )

            terminal_state = (
                CallState.BUSY.value
                if on_failure == "busy"
                else CallState.TRANSPORT_UNREACHABLE.value
            )
            current = registry.sessions.get(registry.resolve_session_id(call_id))
            if current is not None:
                registry.transition(
                    call_id,
                    state=terminal_state,
                    owner="terminal",
                    outcome=reason,
                    expected_revision=current.revision,
                    expected_owner=current.owner,
                )
            _set_sip_bridge_call_state(
                hass,
                terminal_state,
                caller=invite.caller,
                callee=destination,
                call_id=call_id,
                reason=reason,
                terminal_reason=reason,
                origin="self",
                last_sip_event=(
                    "BYE"
                    if _source_dialog_is_answered(preanswered)
                    else "SIP_RESPONSE"
                ),
            )
            registry.finish_and_pop(call_id, reason=reason, state=terminal_state)

        async def _run_forward() -> None:
            client = None
            reservation = None
            reservation_from_preanswer = False
            video_relay = None
            dest_call_id = ""
            try:
                preanswered = registry.preanswered.get(call_id)
                if decision.action is RouteAction.ANSWER_HA:
                    endpoint = target_browser_endpoint
                    if endpoint is None or endpoint.kind is not EndpointKind.BROWSER:
                        raise RuntimeError("target Home Assistant phone disappeared")
                    if endpoint.dnd:
                        raise RuntimeError("target Home Assistant phone is in DND")
                    if endpoint.active_call_id and endpoint.active_call_id != call_id:
                        raise RuntimeError("target Home Assistant phone is busy")
                    if endpoint.availability is EndpointAvailability.UNAVAILABLE:
                        raise RuntimeError("target Home Assistant phone is disabled")
                    session_id = registry.resolve_session_id(call_id)
                    claims = registry.endpoint_claims.get(session_id, {})
                    target_was_claimed = endpoint.endpoint_id in claims
                    old_was_claimed = session_endpoint_id in claims
                    target_claimed = False
                    old_released = False
                    try:
                        registry.claim_endpoint(
                            call_id,
                            endpoint.endpoint_id,
                            role="destination",
                        )
                        target_claimed = not target_was_claimed
                        if session_endpoint_id != endpoint.endpoint_id:
                            old_released = registry.release_endpoint_claim(
                                call_id,
                                session_endpoint_id,
                            ) or old_was_claimed
                        _defer_invite_to_ha_softphone(
                            invite,
                            route_kind=decision.action.value,
                            endpoint_id=endpoint.endpoint_id,
                            endpoint_device_id=str(
                                endpoint.device_id or HA_SOFTPHONE_DEVICE_ID
                            ),
                            callee=endpoint.name,
                            sip_uri=decision.sip_uri,
                            last_sip_event="ROUTE_FORWARD",
                        )
                    except Exception:
                        if target_claimed:
                            registry.release_endpoint_claim(
                                call_id,
                                endpoint.endpoint_id,
                            )
                        if old_released and old_was_claimed:
                            registry.claim_endpoint(
                                call_id,
                                session_endpoint_id,
                                role="destination",
                            )
                        raise
                    return

                reservation = (preanswered or {}).get("rtp_reservation")
                reservation_from_preanswer = reservation is not None
                if reservation is None:
                    reservation = RtpPortReservation.allocate(hass)
                source_relay_port, dest_relay_port = reservation.ports

                if decision.action is RouteAction.GROUP:
                    entry = decision.entry
                    if entry is None:
                        raise RuntimeError("ring group has no roster entry")
                    members = _unique_group_members(entry.metadata.get("members"))
                    attempts: list[OutboundLeg] = []
                    browser_legs: list[BrowserLeg] = []
                    endpoint_registry = hass.data.get(DOMAIN, {}).get(
                        "endpoint_registry"
                    )
                    for member in members:
                        # A later forward moves a call away from HA and must
                        # not ring HA again.  Initial routing is different: the
                        # trunk dialog is only parked on HA while DTMF and
                        # automations choose its real destination, so browser
                        # members remain valid ring-group candidates.
                        browser_leg = _browser_leg_for_member(
                            member, peers, roster_entries
                        )
                        if browser_leg is not None:
                            if not initial_selection:
                                continue
                            endpoint = (
                                endpoint_registry.get(browser_leg.endpoint_id)
                                if endpoint_registry is not None
                                else None
                            )
                            if not _browser_endpoint_can_ring(endpoint):
                                continue
                            try:
                                registry.claim_endpoint(
                                    call_id,
                                    browser_leg.endpoint_id,
                                    role="group_candidate",
                                )
                            except EndpointBusyError:
                                continue
                            browser_legs.append(browser_leg)
                            continue
                        if _caller_matches_member(
                            invite.caller, invite.source_host, member, peers
                        ):
                            continue
                        if len(attempts) >= MAX_RING_GROUP_ATTEMPTS:
                            break
                        attempt = _prepare_outbound_leg(
                            member=member,
                            peers=peers,
                            roster_entries=roster_entries,
                            local_name=invite.caller or _ha_peer_name(hass),
                            local_rtp_port_index=1,
                        )
                        if attempt is not None:
                            attempts.append(attempt)
                    if not attempts and not browser_legs:
                        raise RuntimeError("ring group has no reachable members")

                    def _settle_browser_candidates(
                        state: str,
                        reason: str,
                        *,
                        keep_endpoint_id: str = "",
                    ) -> None:
                        for browser_leg in browser_legs:
                            if browser_leg.endpoint_id == keep_endpoint_id:
                                continue
                            registry.release_endpoint_claim(
                                call_id, browser_leg.endpoint_id
                            )
                            _set_ha_softphone_call_state(
                                hass,
                                state,
                                endpoint_id=browser_leg.endpoint_id,
                                session_device_id=browser_leg.device_id,
                                caller=invite.caller,
                                callee=entry.display_name,
                                peer_name=invite.caller,
                                direction="incoming",
                                call_id=call_id,
                                reason=reason,
                                terminal_reason=reason,
                                route_kind=GROUP_TYPE_RING,
                                last_sip_event="SIP_RESPONSE",
                            )

                    browser_route_future: asyncio.Future | None = None
                    if browser_legs:
                        browser_route_future = (
                            asyncio.get_running_loop().create_future()
                        )
                        registry.upsert(
                            call_id,
                            state=CallState.RINGING.value,
                            owner="router",
                            caller=invite.caller,
                            callee=entry.display_name,
                            route_kind=GROUP_TYPE_RING,
                            ring_endpoint_ids=tuple(
                                leg.endpoint_id for leg in browser_legs
                            ),
                        )
                        registry.pending_routes[call_id] = {
                            "invite": invite,
                            "future": browser_route_future,
                            "ring_group_endpoint_ids": tuple(
                                leg.endpoint_id for leg in browser_legs
                            ),
                            "declined_endpoint_ids": set(),
                        }
                        for browser_leg in browser_legs:
                            _publish_pending_ha_softphone_ringing(
                                invite,
                                route_kind=GROUP_TYPE_RING,
                                endpoint_id=browser_leg.endpoint_id,
                                endpoint_device_id=browser_leg.device_id,
                                callee=entry.display_name,
                                last_sip_event="ROUTE_FORWARD",
                            )
                    group_ringing_published = False

                    async def _dial_group_member(
                        attempt: OutboundLeg,
                    ) -> tuple[str, OutboundLeg]:
                        nonlocal group_ringing_published
                        result = await attempt.client.invite(
                            target=attempt.uri.user or attempt.member,
                            remote_host=attempt.uri.host,
                            remote_sip_port=attempt.uri.port or int(cfg["sip_port"]),
                            request_uri=str(attempt.uri),
                            timeout=8.0,
                        )
                        if result == "ringing":
                            if not group_ringing_published:
                                group_ringing_published = True
                                _set_sip_bridge_call_state(
                                    hass,
                                    CallState.REMOTE_RINGING.value,
                                    caller=invite.caller,
                                    callee=entry.display_name,
                                    peer_name=entry.display_name,
                                    call_id=call_id,
                                    direction="incoming",
                                    route_source="automation",
                                    route_kind=GROUP_TYPE_RING,
                                    last_sip_event="SIP_RESPONSE",
                                )
                            result = await attempt.client.wait_for_final(
                                timeout=RING_GROUP_TIMEOUT_S
                            )
                        return result, attempt

                    async def _wait_browser_group_member():
                        if browser_route_future is None:
                            return "timeout", None, {}
                        try:
                            browser_decision = await asyncio.wait_for(
                                browser_route_future,
                                timeout=RING_GROUP_TIMEOUT_S,
                            )
                        except asyncio.TimeoutError:
                            return "timeout", None, {}
                        action = str(
                            (browser_decision or {}).get("action") or ""
                        ).strip().lower()
                        endpoint_id = str(
                            (browser_decision or {}).get("endpoint_id") or ""
                        ).strip()
                        selected = next(
                            (
                                leg
                                for leg in browser_legs
                                if leg.endpoint_id == endpoint_id
                            ),
                            None,
                        )
                        if action in {"answer_ha", "default"} and selected is not None:
                            return "in_call_browser", selected, browser_decision
                        if action in {"forward", "bridge"}:
                            return "reroute", None, browser_decision
                        if action == "cancel":
                            return "cancelled", None, browser_decision
                        return "declined", selected, browser_decision

                    tasks = [
                        asyncio.create_task(_dial_group_member(attempt))
                        for attempt in attempts
                    ]
                    if browser_route_future is not None:
                        tasks.append(asyncio.create_task(_wait_browser_group_member()))
                    winner: OutboundLeg | BrowserLeg | None = None
                    browser_decision: dict = {}
                    reroute_decision: dict | None = None
                    failure = "timeout"
                    try:
                        deadline = (
                            asyncio.get_running_loop().time() + RING_GROUP_TIMEOUT_S
                        )
                        pending_tasks = set(tasks)
                        while pending_tasks and winner is None:
                            timeout = max(
                                0.0, deadline - asyncio.get_running_loop().time()
                            )
                            if timeout <= 0:
                                break
                            done, pending_tasks = await asyncio.wait(
                                pending_tasks,
                                timeout=timeout,
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if not done:
                                break
                            for task in tasks:
                                if task not in done:
                                    continue
                                try:
                                    task_result = task.result()
                                except Exception as err:  # noqa: BLE001
                                    failure = str(err or failure)
                                    continue
                                if len(task_result) == 3:
                                    result, attempt, decision_data = task_result
                                else:
                                    result, attempt = task_result
                                    decision_data = {}
                                if (
                                    result == "in_call"
                                    and isinstance(attempt, OutboundLeg)
                                    and attempt.client.dialog is not None
                                ):
                                    winner = attempt
                                    break
                                if (
                                    result == "in_call_browser"
                                    and isinstance(attempt, BrowserLeg)
                                ):
                                    winner = attempt
                                    browser_decision = dict(decision_data or {})
                                    break
                                if result == "reroute":
                                    reroute_decision = dict(decision_data or {})
                                    break
                                failure = result or failure
                            if reroute_decision is not None:
                                pending_tasks.clear()
                                break
                    except asyncio.CancelledError:
                        registry.pending_routes.pop(call_id, None)
                        _settle_browser_candidates(
                            CallState.CANCELLED.value,
                            TerminalReason.CANCELLED.value,
                        )
                        await _cleanup_outbound_attempts(tasks, attempts)
                        raise
                    finally:
                        await _cancel_and_join_tasks(tasks)

                    losers = [attempt for attempt in attempts if attempt is not winner]

                    async def _cancel_losing_sip_legs() -> None:
                        await asyncio.gather(
                            *(
                                _close_outbound_leg(attempt, cancel=True)
                                for attempt in losers
                            ),
                            return_exceptions=True,
                        )

                    # A browser answer is the fork commit point.  Do not make
                    # its final SIP response wait for remote losing legs to
                    # acknowledge CANCEL; mature PBX implementations cancel
                    # those branches as a consequence of winner selection.
                    if isinstance(winner, BrowserLeg):
                        create_runtime_task(hass, _cancel_losing_sip_legs())
                    else:
                        await _cancel_losing_sip_legs()
                    if reroute_decision is not None:
                        route = registry.pending_routes.pop(call_id, None) or {}
                        _settle_browser_candidates(
                            CallState.IDLE.value,
                            "forwarded",
                        )
                        handoff = route.get("forward_handoff")
                        if handoff is not None and not handoff.done():
                            handoff.set_result(dict(reroute_decision))
                        return
                    if winner is None:
                        registry.pending_routes.pop(call_id, None)
                        _settle_browser_candidates(
                            CallState.TRANSPORT_UNREACHABLE.value,
                            TerminalReason.TRANSPORT_UNREACHABLE.value,
                        )
                        raise RuntimeError(failure)

                    if isinstance(winner, BrowserLeg):
                        registry.pending_routes.pop(call_id, None)
                        _settle_browser_candidates(
                            CallState.CANCELLED.value,
                            TerminalReason.CANCELLED.value,
                            keep_endpoint_id=winner.endpoint_id,
                        )
                        registry.pending_invites[call_id] = invite
                        registry.upsert(
                            call_id,
                            state=CallState.RINGING.value,
                            owner="ha_softphone",
                            caller=invite.caller,
                            callee=entry.display_name,
                            route_kind=GROUP_TYPE_RING,
                            endpoint_id=winner.endpoint_id,
                            session_device_id=winner.device_id,
                        )
                        answer_commits = hass.data.setdefault(DOMAIN, {}).setdefault(
                            "ring_group_answer_commits", set()
                        )
                        answer_commits.add(call_id)
                        try:
                            await hass.services.async_call(
                                DOMAIN,
                                "answer",
                                {
                                    "call_id": call_id,
                                    # Service selectors are HA Device IDs; the
                                    # endpoint ID remains runtime metadata.
                                    "device_id": winner.device_id,
                                    "media_client_id": str(
                                        browser_decision.get("media_client_id") or ""
                                    ),
                                    "send_video": bool(
                                        browser_decision.get("send_video", False)
                                    ),
                                },
                                blocking=True,
                                context=registry.ha_context(call_id),
                            )
                        finally:
                            answer_commits.discard(call_id)
                        return

                    registry.pending_routes.pop(call_id, None)
                    _settle_browser_candidates(
                        CallState.CANCELLED.value,
                        TerminalReason.CANCELLED.value,
                    )

                    client = winner.client
                    dest_call_id = client.dialog_ids.call_id
                    dest_relay_port = winner.ports.ports[1]
                    registry.register_bridge(
                        source_call_id=call_id,
                        dest_call_id=dest_call_id,
                        client=client,
                        state=CallState.CONNECTING.value,
                        caller=invite.caller,
                        callee=entry.display_name,
                        route_kind=GROUP_TYPE_RING,
                        source_role="trunk" if preanswered is not None else "caller",
                        source_state=(
                            CallState.IN_CALL.value
                            if _source_dialog_is_answered(preanswered)
                            else CallState.CONNECTING.value
                        ),
                        dest_state=CallState.IN_CALL.value,
                    )

                    source_ports = reservation.ports
                    winner_ports = winner.ports.ports

                    def _release_group_ports(_ports) -> None:
                        _release_sip_rtp_port_pair(hass, source_ports)
                        _release_sip_rtp_port_pair(hass, winner_ports)

                    relay = build_invite_client_relay(
                        invite=invite,
                        client=client,
                        source_relay_port=source_relay_port,
                        dest_relay_port=dest_relay_port,
                        debug_capture=_debug_mode(hass),
                        on_release=_release_group_ports,
                    )
                    _attach_dtmf_event_bridge(
                        hass,
                        relay,
                        call_id=call_id,
                        dest_call_id=dest_call_id,
                        caller=invite.caller,
                        callee=winner.member,
                        client=client,
                    )
                    try:
                        await relay.start()
                    except Exception:
                        registry.bridge_clients.pop(call_id, None)
                        registry.take_sip_client(dest_call_id)
                        registry.take_client_watcher(dest_call_id)
                        registry.remove_leg(call_id, dest_call_id)
                        await _close_outbound_leg(winner, bye_or_cancel=True)
                        raise
                    reservation.detach()
                    winner.ports.detach()
                    _attach_client_media_update(
                        client,
                        relay,
                        source_call_id=call_id,
                    )
                    registry.attach_relay(call_id, relay)
                    registry.pending_invites.pop(call_id, None)
                    consumed_preanswer = registry.take_media(
                        call_id, provisional=True
                    )
                    # The audio reservation moved to Assist ownership above;
                    # any pre-answer video sockets did not and must be released.
                    _release_video_media_reservation(consumed_preanswer)
                    if preanswered is None or not bool(
                        preanswered.get("final_response_sent", True)
                    ):
                        answer = build_answer_directional(
                            local_ip,
                            local_ip,
                            source_relay_port,
                            invite.send_format,
                            invite.recv_format,
                            dtmf=_invite_dtmf_format(invite),
                            remote_sdp=invite.remote_sdp,
                        )
                        _sip_send_final_response(
                            hass, call_id, 200, "OK", answer_sdp=answer
                        )
                    connected_party = str(winner.member or "").strip()
                    _set_sip_bridge_call_state(
                        hass,
                        CallState.IN_CALL.value,
                        caller=invite.caller,
                        callee=entry.display_name,
                        peer_name=connected_party,
                        call_id=call_id,
                        dest_call_id=dest_call_id,
                        dialed_target=entry.display_name,
                        connected_party=connected_party,
                        answered_by=connected_party,
                        direction="incoming",
                        route_source="automation",
                        route_kind=GROUP_TYPE_RING,
                        sip_status_code=200,
                        last_sip_event="SIP_RESPONSE",
                        sip_uri=str(winner.uri),
                    )
                    current_task = asyncio.current_task()
                    if current_task is not None:
                        registry.attach_client_watcher(dest_call_id, current_task)
                    terminal = await client.wait_for_dialog_termination()
                    terminal_reason = (
                        TerminalReason.REMOTE_HANGUP.value
                        if terminal == "remote_hangup"
                        else _sip_terminal_reason(terminal, _sip_public_state(terminal))
                    )
                    await _terminate_sip_bridge(
                        hass, dest_call_id, terminal_reason=terminal_reason
                    )
                    return

                if decision.action is RouteAction.ASSIST:
                    current = registry.sessions.get(
                        registry.resolve_session_id(call_id)
                    )
                    if current is not None:
                        claimed_assist = registry.transition(
                            call_id,
                            state=CallState.CONNECTING.value,
                            owner="assist",
                            callee=destination,
                            expected_revision=current.revision,
                            expected_owner=current.owner,
                        )
                        if claimed_assist is None:
                            raise RuntimeError("Assist route ownership changed")
                    await _start_local_assist_bridge(
                        invite,
                        reservation=reservation,
                        local_rtp_port=source_relay_port,
                        roster_entries=roster_entries,
                        source="trunk" if preanswered is not None else "sip",
                        called_extension=str(
                            (
                                decision.entry.extension
                                if decision.entry is not None
                                else ""
                            )
                            or destination
                        ),
                        release_reservation_on_failure=preanswered is None,
                    )
                    if preanswered is None or not bool(
                        preanswered.get("final_response_sent", True)
                    ):
                        answer = build_answer_directional(
                            local_ip,
                            local_ip,
                            source_relay_port,
                            invite.send_format,
                            invite.recv_format,
                            remote_sdp=invite.remote_sdp,
                        )
                        _sip_send_final_response(
                            hass,
                            call_id,
                            200,
                            "OK",
                            answer_sdp=answer,
                        )
                    registry.pending_invites.pop(call_id, None)
                    registry.take_media(call_id, provisional=True)
                    current = registry.sessions.get(
                        registry.resolve_session_id(call_id)
                    )
                    if current is not None:
                        registry.transition(
                            call_id,
                            state=CallState.IN_CALL.value,
                            owner="assist",
                            callee=destination,
                            expected_revision=current.revision,
                            expected_owner=current.owner,
                        )
                    return

                bridge_to_trunk = decision.action is RouteAction.TRUNK
                bridge_uri = None
                peer_target = _peer_for_target(decision.target or destination, peers)
                if bridge_to_trunk:
                    trunk_cfg = _get_trunk_config(hass)
                    bridge_uri = parse_sip_uri(
                        f"sip:{decision.target or destination}@{trunk_cfg[CONF_TRUNK_SERVER]}:"
                        f"{int(trunk_cfg[CONF_TRUNK_PORT])};"
                        f"transport={str(trunk_cfg[CONF_TRUNK_TRANSPORT]).lower()}"
                    )
                else:
                    bridge_uri, _peer, member_entry = _sip_uri_for_member(
                        decision.target or destination,
                        peers,
                        roster_entries,
                    )
                    if bridge_uri is None and decision.sip_uri:
                        bridge_uri = parse_sip_uri(decision.sip_uri)
                    if bridge_uri is None:
                        raise RuntimeError(
                            f"destination {destination} has no reachable SIP URI"
                        )

                remote_tx_formats = _peer_audio_formats(
                    peer_target, "tx_formats"
                ) or _roster_entry_formats(
                    decision.entry,
                    "tx_formats",
                )
                remote_rx_formats = _peer_audio_formats(
                    peer_target, "rx_formats"
                ) or _roster_entry_formats(
                    decision.entry,
                    "rx_formats",
                )
                sip_send_formats, sip_recv_formats = _sip_target_audio_profile(
                    remote_tx_formats=remote_tx_formats,
                    remote_rx_formats=remote_rx_formats,
                    target=decision.target or destination,
                )
                bridge_to_registered = bool(
                    decision.entry is not None
                    and decision.entry.sip_uri
                    and decision.entry.metadata.get("registered")
                )
                if bridge_to_trunk or bridge_to_registered:
                    sip_send_formats = list(HA_TRUNK_AUDIO_FORMATS)
                    sip_recv_formats = list(HA_TRUNK_AUDIO_FORMATS)
                trunk_cfg = _get_trunk_config(hass)
                endpoint_registry = hass.data.get(DOMAIN, {}).get(
                    "endpoint_registry"
                )
                source_route_endpoint_id = str(
                    ((session.metadata if session is not None else {}) or {}).get(
                        "source_endpoint_id"
                    )
                    or ""
                ).strip()
                target_route_endpoint_id = str(
                    ((decision.entry.metadata if decision.entry is not None else {}) or {}).get(
                        "endpoint_id"
                    )
                    or ""
                ).strip()
                source_route_endpoint = (
                    endpoint_registry.get(source_route_endpoint_id)
                    if endpoint_registry is not None and source_route_endpoint_id
                    else None
                )
                target_route_endpoint = (
                    endpoint_registry.get(target_route_endpoint_id)
                    if endpoint_registry is not None and target_route_endpoint_id
                    else None
                )
                forward_video_enabled = bool(
                    preanswered is None
                    and cfg.get(CONF_SIP_VIDEO, False)
                    and invite.video_format is not None
                    and (
                        source_route_endpoint is None
                        or source_route_endpoint.supports("video")
                    )
                    and (
                        target_route_endpoint is None
                        or target_route_endpoint.supports("video")
                    )
                )
                video_dest_port = 0
                video_failure_reason = ""
                if forward_video_enabled:
                    video_reservation = None
                    sockets = ()
                    try:
                        video_reservation, sockets = reserve_sip_video_relay_media(hass)
                        source_video_port, video_dest_port = video_reservation.ports
                        video_relay = build_pending_invite_video_relay(
                            invite,
                            remote_host=str(bridge_uri.host),
                            left_port=source_video_port,
                            right_port=video_dest_port,
                            sockets=sockets,
                            on_release=lambda ports: _release_sip_rtp_port_pair(
                                hass, ports
                            ),
                        )
                        # The relay owns all four bound sockets from here.
                        video_reservation.detach()
                    except (OSError, RuntimeError) as err:
                        for sock in sockets:
                            sock.close()
                        if video_reservation is not None:
                            video_reservation.release()
                        video_relay = None
                        video_dest_port = 0
                        video_failure_reason = (
                            "local_video_resources_unavailable"
                        )
                        _LOGGER.warning(
                            "SIP forward video relay unavailable; continuing audio-only: %s",
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
                    signaling_transport=_sip_uri_transport(bridge_uri),
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
                    include_common_codecs=bridge_to_trunk or bridge_to_registered,
                    local_video_rtp_port=video_dest_port,
                    video_formats=(invite.video_format,) if video_relay is not None else (),
                    video_direction=(
                        invite.video_format.direction
                        if video_relay is not None
                        else "inactive"
                    ),
                    generic_video_relay=video_relay is not None,
                )
                if not bridge_to_trunk:
                    _enable_reused_sip_tcp_connection(
                        hass,
                        client,
                        bridge_uri,
                        target=decision.target or destination,
                        default_sip_port=int(cfg["sip_port"]),
                    )
                result = await client.invite(
                    target=bridge_uri.user,
                    remote_host=bridge_uri.host,
                    remote_sip_port=bridge_uri.port or int(cfg["sip_port"]),
                    request_uri=str(bridge_uri),
                    timeout=SIP_TIMER_B if bridge_to_trunk else 8.0,
                )
                bucket = hass.data.setdefault(DOMAIN, {})
                if call_id in bucket.get("trunk_closed_calls", set()):
                    bucket["trunk_closed_calls"].discard(call_id)
                    raise RuntimeError(TerminalReason.CANCELLED.value)
                if result not in {"ringing", "in_call"}:
                    raise RuntimeError(result)

                dest_call_id = client.dialog_ids.call_id
                registry.register_bridge(
                    source_call_id=call_id,
                    dest_call_id=dest_call_id,
                    client=client,
                    state=CallState.REMOTE_RINGING.value
                    if result == "ringing"
                    else CallState.CONNECTING.value,
                    caller=invite.caller,
                    callee=destination,
                    route_kind=decision.action.value,
                    source_role="trunk" if preanswered is not None else "caller",
                    source_state=(
                        CallState.IN_CALL.value
                        if _source_dialog_is_answered(preanswered)
                        else CallState.CONNECTING.value
                    ),
                    dest_state=result,
                )
                current_task = asyncio.current_task()
                if current_task is not None:
                    registry.attach_client_watcher(dest_call_id, current_task)
                if result == "ringing":
                    _set_sip_bridge_call_state(
                        hass,
                        CallState.REMOTE_RINGING.value,
                        caller=invite.caller,
                        callee=destination,
                        peer_name=destination,
                        call_id=call_id,
                        dest_call_id=dest_call_id,
                        direction="incoming",
                        route_source="automation",
                        last_sip_event="SIP_RESPONSE",
                    )
                    result = await client.wait_for_final()
                if result != "in_call" or client.dialog is None:
                    raise RuntimeError(result)

                selected_video = None
                selected_video_direction = "inactive"
                if video_relay is not None:
                    video_answer = configure_answered_invite_video_relay(
                        invite, client.dialog, video_relay
                    )
                    if video_answer is None:
                        _LOGGER.info(
                            "SIP forward video rejected: destination did not accept an exact codec call_id=%s",
                            call_id,
                        )
                        await video_relay.stop()
                        video_relay = None
                        video_failure_reason = "remote_video_rejected"
                    else:
                        selected_video = video_answer.video_format
                        selected_video_direction = video_answer.direction

                relay = build_invite_client_relay(
                    invite=invite,
                    client=client,
                    source_relay_port=source_relay_port,
                    dest_relay_port=dest_relay_port,
                    debug_capture=_debug_mode(hass),
                    on_release=lambda ports: _release_sip_rtp_port_pair(hass, ports),
                )
                _attach_dtmf_event_bridge(
                    hass,
                    relay,
                    call_id=call_id,
                    dest_call_id=dest_call_id,
                    caller=invite.caller,
                    callee=destination,
                    client=client,
                )
                if video_relay is not None:
                    relay.attach_video_relay(video_relay)
                await relay.start()
                reservation.detach()
                _attach_client_media_update(
                    client,
                    relay,
                    source_call_id=call_id,
                )
                registry.attach_relay(call_id, relay)
                registry.pending_invites.pop(call_id, None)
                registry.take_media(call_id, provisional=True)
                if preanswered is None or not bool(
                    preanswered.get("final_response_sent", True)
                ):
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
                        hass, call_id, 200, "OK", answer_sdp=answer
                    )
                registry.upsert(
                    call_id,
                    state=CallState.IN_CALL.value,
                    owner="bridge",
                    caller=invite.caller,
                    callee=destination,
                    route_kind=decision.action.value,
                )
                _set_sip_bridge_call_state(
                    hass,
                    CallState.IN_CALL.value,
                    caller=invite.caller,
                    callee=destination,
                    peer_name=destination,
                    call_id=call_id,
                    dest_call_id=dest_call_id,
                    direction="incoming",
                    route_source="automation",
                    answered_by=destination,
                    selected_tx_format=invite.send_format.audio_format.wire_token(),
                    selected_rx_format=invite.recv_format.audio_format.wire_token(),
                    selected_tx_rtp_format=invite.send_format.wire_token(),
                    selected_rx_rtp_format=invite.recv_format.wire_token(),
                    sip_status_code=200,
                    last_sip_event="SIP_RESPONSE",
                    route_kind=decision.action.value,
                    sip_uri=str(bridge_uri),
                    video_active=bool(video_relay is not None),
                    video_requested=forward_video_enabled,
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
                terminal = await client.wait_for_dialog_termination()
                terminal_reason = (
                    TerminalReason.REMOTE_HANGUP.value
                    if terminal == "remote_hangup"
                    else _sip_terminal_reason(terminal, _sip_public_state(terminal))
                )
                await _terminate_sip_bridge(
                    hass,
                    dest_call_id,
                    terminal_reason=terminal_reason,
                )
            except asyncio.CancelledError:
                if dest_call_id:
                    registry.bridge_clients.pop(call_id, None)
                    registry.take_sip_client(dest_call_id)
                    registry.take_client_watcher(dest_call_id)
                    registry.remove_leg(call_id, dest_call_id)
                if reservation is not None and not reservation_from_preanswer:
                    reservation.release()
                if video_relay is not None:
                    await video_relay.stop()
                    video_relay = None
                if client is not None:
                    await async_cleanup_sip_runtime(
                        client=client,
                        terminate_client=True,
                    )
                raise
            except Exception as err:  # noqa: BLE001 - convert route failures to policy.
                reason = str(err or TerminalReason.TRANSPORT_UNREACHABLE.value)
                _LOGGER.info(
                    "SIP automation forward failed call_id=%s destination=%s reason=%s",
                    call_id,
                    destination,
                    reason,
                )
                if dest_call_id:
                    registry.bridge_clients.pop(call_id, None)
                    registry.take_sip_client(dest_call_id)
                    registry.take_client_watcher(dest_call_id)
                    registry.remove_leg(call_id, dest_call_id)
                if reservation is not None and not reservation_from_preanswer:
                    reservation.release()
                if video_relay is not None:
                    await video_relay.stop()
                    video_relay = None
                if client is not None:
                    await async_cleanup_sip_runtime(
                        client=client,
                        terminate_client=True,
                    )
                await _restore_or_terminate(reason)
            finally:
                forward_tasks.pop(call_id, None)
                forward_claims.discard(call_id)

        task = create_runtime_task(hass, _run_forward())
        forward_tasks[call_id] = task

    async def _run_trunk_inbound_route_guarded(
        invite: SipInvite,
        *,
        bridge_ports: RtpPortReservation,
    ) -> None:
        """Fail one detached trunk route closed and release all ownership."""

        try:
            await _run_trunk_inbound_route(invite, bridge_ports=bridge_ports)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - detached call boundary.
            _LOGGER.exception(
                "SIP trunk inbound routing failed call_id=%s", invite.call_id
            )
            registry = _call_registry(hass)
            registry.pending_invites.pop(invite.call_id, None)
            preanswered = registry.take_media(invite.call_id, provisional=True)
            _release_media_reservation(preanswered)
            bridge_ports.release()
            _sip_send_bye(hass, invite.call_id)
            _set_sip_bridge_call_state(
                hass,
                CallState.TRANSPORT_UNREACHABLE.value,
                caller=invite.caller,
                callee=invite.target,
                peer_name=invite.caller,
                call_id=invite.call_id,
                direction="incoming",
                reason=str(err),
                terminal_reason=RouteReason.TARGET_UNREACHABLE.value,
                origin="self",
                sip_status_code=500,
                last_sip_event="BYE",
            )
            registry.finish_and_pop(
                invite.call_id,
                reason=RouteReason.TARGET_UNREACHABLE.value,
                state=CallState.TRANSPORT_UNREACHABLE.value,
            )

    async def _run_ring_group_call(
        invite: SipInvite,
        entry: RosterEntry,
        peers: list[Peer],
        roster_entries: list[RosterEntry],
        *,
        origin_endpoint_id: str = "",
        origin_media_client_id: str = "",
        request_video: bool = False,
        enable_caller_video_send: bool = False,
    ) -> None:
        registry = _call_registry(hass)
        origin_endpoint_id = str(origin_endpoint_id or "").strip()
        endpoint_registry = hass.data.get(DOMAIN, {}).get("endpoint_registry")
        origin_endpoint = (
            endpoint_registry.get(origin_endpoint_id)
            if endpoint_registry is not None and origin_endpoint_id
            else None
        )
        origin_device_id = str(
            getattr(origin_endpoint, "device_id", "") or HA_SOFTPHONE_DEVICE_ID
        )
        origin_name = str(
            getattr(origin_endpoint, "name", "") or _ha_peer_name(hass)
        ).strip()
        ha_origin = bool(origin_endpoint_id)
        call_ingress = "trunk" if invite.received_via_trunk else "extension"
        members = _unique_group_members(entry.metadata.get("members"))
        try:
            ring_policy = RingPolicy.from_metadata(entry.metadata)
        except (TypeError, ValueError) as err:
            _LOGGER.error(
                "SIP ring group has invalid policy call_id=%s group=%s: %s",
                invite.call_id,
                entry.display_name,
                err,
            )
            _sip_send_final_response(
                hass,
                invite.call_id,
                500,
                "Server Internal Error",
                decline_reason=TerminalReason.PROTOCOL_ERROR.value,
            )
            registry.finish_and_pop(
                invite.call_id,
                reason=TerminalReason.PROTOCOL_ERROR.value,
                state=CallState.TRANSPORT_UNREACHABLE.value,
            )
            return
        attempts: list[OutboundLeg] = []
        browser_legs: list[BrowserLeg] = []
        preflight_failures: list[tuple[str, str, DialDisposition, int, int]] = []
        session = registry.sessions.get(registry.resolve_session_id(invite.call_id))
        if session is None or not registry.is_generation_current(
            invite.call_id,
            session.generation,
        ):
            _LOGGER.info(
                "SIP ring group did not start for a terminated source call_id=%s",
                invite.call_id,
            )
            return
        call_generation = session.generation

        def _call_is_current() -> bool:
            return registry.is_generation_current(
                invite.call_id,
                call_generation,
            )

        source_endpoint_id = str(
            origin_endpoint_id
            or ((session.metadata if session is not None else {}) or {}).get(
                "source_endpoint_id"
            )
            or ""
        ).strip()

        def _settle_browser_candidates(
            state: str,
            reason: str,
            *,
            keep_endpoint_id: str = "",
        ) -> None:
            """Release and publish every browser candidate except the winner."""
            _settle_ring_browser_candidates(
                hass,
                registry,
                browser_legs,
                call_id=invite.call_id,
                caller=invite.caller,
                callee=entry.display_name,
                state=state,
                reason=reason,
                route_kind=GROUP_TYPE_RING,
                keep_endpoint_id=keep_endpoint_id,
            )
        async def _prepare_candidates() -> None:
            for member_order, member in enumerate(members):
                if _caller_matches_member(
                    invite.caller,
                    invite.source_host,
                    member,
                    peers,
                    source_endpoint_id=source_endpoint_id,
                ):
                    continue
                browser_leg = _browser_leg_for_member(
                    member, peers, roster_entries
                )
                if browser_leg is not None:
                    if browser_leg.endpoint_id == source_endpoint_id:
                        continue
                    endpoint = (
                        endpoint_registry.get(browser_leg.endpoint_id)
                        if endpoint_registry is not None
                        else None
                    )
                    disposition = _endpoint_preflight_disposition(
                        endpoint,
                        call_id=invite.call_id,
                        browser=True,
                    )
                    if disposition is not None:
                        preflight_failures.append(
                            (
                                f"preflight:{member_order}:{disposition.value}:{browser_leg.endpoint_id}",
                                browser_leg.endpoint_id,
                                disposition,
                                ring_policy.member_tiers.get(member.casefold(), 0),
                                member_order * 1000,
                            )
                        )
                        continue
                    try:
                        registry.claim_endpoint(
                            invite.call_id,
                            browser_leg.endpoint_id,
                            role="group_candidate",
                        )
                    except EndpointBusyError:
                        preflight_failures.append(
                            (
                                f"preflight:{member_order}:busy:{browser_leg.endpoint_id}",
                                browser_leg.endpoint_id,
                                DialDisposition.BUSY,
                                ring_policy.member_tiers.get(member.casefold(), 0),
                                member_order * 1000,
                            )
                        )
                        continue
                    browser_legs.append(browser_leg)
                    continue
                logical_endpoint = _logical_endpoint_for_member(
                    member, peers, roster_entries
                )
                logical_endpoint_id = str(
                    getattr(logical_endpoint, "endpoint_id", "") or ""
                ).strip()
                if logical_endpoint_id == source_endpoint_id:
                    continue
                disposition = _endpoint_preflight_disposition(
                    logical_endpoint,
                    call_id=invite.call_id,
                    browser=False,
                )
                if disposition is not None:
                    preflight_failures.append(
                        (
                            f"preflight:{member_order}:{disposition.value}:{logical_endpoint_id}",
                            logical_endpoint_id,
                            disposition,
                            ring_policy.member_tiers.get(member.casefold(), 0),
                            member_order * 1000,
                        )
                    )
                    continue
                contact_targets = build_sip_contact_targets(
                    (member,),
                    roster_entries,
                    policy=ring_policy,
                    exclude_endpoint_id=source_endpoint_id,
                )
                target_specs = contact_targets or (None,)
                for contact_order, target_spec in enumerate(target_specs):
                    if len(attempts) >= MAX_RING_GROUP_ATTEMPTS:
                        _LOGGER.warning(
                            "SIP ring group %s has more than %d dialable contacts; "
                            "excess contacts were skipped",
                            entry.display_name,
                            MAX_RING_GROUP_ATTEMPTS,
                        )
                        return
                    try:
                        leg = _prepare_outbound_leg(
                            member=member,
                            peers=peers,
                            roster_entries=roster_entries,
                            local_name=invite.caller or _ha_peer_name(hass),
                            local_rtp_port_index=1,
                            uri_override=(
                                target_spec.uri if target_spec is not None else ""
                            ),
                            endpoint_id_override=(
                                target_spec.endpoint_id
                                if target_spec is not None
                                else ""
                            ),
                            candidate_id=(
                                target_spec.candidate_id
                                if target_spec is not None
                                else f"sip:{member_order}:{contact_order}:{member}"
                            ),
                            tier=(
                                target_spec.tier
                                if target_spec is not None
                                else ring_policy.member_tiers.get(
                                    member.casefold(), 0
                                )
                            ),
                            order=(
                                target_spec.order
                                if target_spec is not None
                                else member_order * 1000 + contact_order
                            ),
                            invite=invite,
                        )
                    except RuntimeError as err:
                        _LOGGER.warning(
                            "SIP ring group RTP port allocation failed member=%s: %s",
                            member,
                            err,
                        )
                        return
                    if leg is None:
                        continue
                    if leg.endpoint_id == source_endpoint_id:
                        await _close_outbound_leg(leg)
                        continue
                    try:
                        if leg.endpoint_id:
                            registry.claim_endpoint(
                                invite.call_id,
                                leg.endpoint_id,
                                role="group_candidate",
                                adopt_transport=_endpoint_is_esphome(
                                    logical_endpoint
                                ),
                            )
                    except EndpointBusyError:
                        await _close_outbound_leg(leg)
                        preflight_failures.append(
                            (
                                f"preflight:{leg.candidate_id}:busy",
                                leg.endpoint_id,
                                DialDisposition.BUSY,
                                leg.tier,
                                leg.order,
                            )
                        )
                        continue
                    attempts.append(leg)

        try:
            await _prepare_candidates()
            if not _call_is_current():
                _settle_browser_candidates(
                    CallState.CANCELLED.value,
                    TerminalReason.CANCELLED.value,
                )
                await _cleanup_outbound_attempts([], attempts)
                return
        except asyncio.CancelledError:
            _settle_browser_candidates(
                CallState.CANCELLED.value,
                TerminalReason.CANCELLED.value,
            )
            await _cleanup_outbound_attempts([], attempts)
            registry.finish_and_pop(
                invite.call_id,
                reason=TerminalReason.CANCELLED.value,
                state=CallState.CANCELLED.value,
            )
            raise
        except Exception as err:
            _LOGGER.exception(
                "SIP ring group candidate preparation failed call_id=%s: %s",
                invite.call_id,
                err,
            )
            _settle_browser_candidates(
                CallState.TRANSPORT_UNREACHABLE.value,
                TerminalReason.PROTOCOL_ERROR.value,
            )
            await _cleanup_outbound_attempts([], attempts)
            _sip_send_final_response(
                hass,
                invite.call_id,
                500,
                "Server Internal Error",
                decline_reason=TerminalReason.PROTOCOL_ERROR.value,
            )
            registry.finish_and_pop(
                invite.call_id,
                reason=TerminalReason.PROTOCOL_ERROR.value,
                state=CallState.TRANSPORT_UNREACHABLE.value,
            )
            return
        route_future: asyncio.Future = asyncio.get_running_loop().create_future()
        _pending_routes(hass)[invite.call_id] = {
            "invite": invite,
            "future": route_future,
            "ring_group_endpoint_ids": tuple(
                leg.endpoint_id for leg in browser_legs
            ),
            "declined_endpoint_ids": set(),
        }
        try:
            if browser_legs:
                registry.upsert(
                    invite.call_id,
                    state=CallState.RINGING.value,
                    owner="ha_softphone",
                    caller=invite.caller,
                    callee=entry.display_name,
                    route_kind=GROUP_TYPE_RING,
                    endpoint_id=(origin_endpoint_id if ha_origin else ""),
                    source_endpoint_id=source_endpoint_id,
                    ring_endpoint_ids=tuple(
                        leg.endpoint_id for leg in browser_legs
                    ),
                    media_client_id=origin_media_client_id,
                )
                for browser_leg in browser_legs:
                    registry.add_leg(
                        invite.call_id,
                        f"browser:{browser_leg.endpoint_id}",
                        role="ha_softphone",
                        state=CallState.RINGING.value,
                    )
                    _set_ha_softphone_call_state(
                        hass,
                        CallState.RINGING.value,
                        endpoint_id=browser_leg.endpoint_id,
                        session_device_id=browser_leg.device_id,
                        caller=invite.caller,
                        callee=entry.display_name,
                        peer_name=invite.caller,
                        direction="incoming",
                        call_id=invite.call_id,
                        selected_tx_format=(
                            invite.send_format.audio_format.wire_token()
                        ),
                        selected_rx_format=(
                            invite.recv_format.audio_format.wire_token()
                        ),
                        selected_tx_rtp_format=invite.send_format.wire_token(),
                        selected_rx_rtp_format=invite.recv_format.wire_token(),
                        audio_mode="full_duplex",
                        route_kind=GROUP_TYPE_RING,
                        sip_status_code=180,
                        last_sip_event="INVITE",
                    )
        except asyncio.CancelledError:
            _pending_routes(hass).pop(invite.call_id, None)
            _settle_browser_candidates(
                CallState.CANCELLED.value,
                TerminalReason.CANCELLED.value,
            )
            await _cleanup_outbound_attempts([], attempts)
            registry.finish_and_pop(
                invite.call_id,
                reason=TerminalReason.CANCELLED.value,
                state=CallState.CANCELLED.value,
            )
            raise
        except Exception as err:
            _LOGGER.exception(
                "SIP ring group state publication failed call_id=%s: %s",
                invite.call_id,
                err,
            )
            _pending_routes(hass).pop(invite.call_id, None)
            _settle_browser_candidates(
                CallState.TRANSPORT_UNREACHABLE.value,
                TerminalReason.PROTOCOL_ERROR.value,
            )
            await _cleanup_outbound_attempts([], attempts)
            _sip_send_final_response(
                hass,
                invite.call_id,
                500,
                "Server Internal Error",
                decline_reason=TerminalReason.PROTOCOL_ERROR.value,
            )
            registry.finish_and_pop(
                invite.call_id,
                reason=TerminalReason.PROTOCOL_ERROR.value,
                state=CallState.TRANSPORT_UNREACHABLE.value,
            )
            return
        if not attempts and not browser_legs and not preflight_failures:
            _pending_routes(hass).pop(invite.call_id, None)
            if ha_origin:
                _set_ha_softphone_call_state(
                    hass,
                    CallState.TRANSPORT_UNREACHABLE.value,
                    endpoint_id=origin_endpoint_id,
                    session_device_id=origin_device_id,
                    caller=origin_name,
                    callee=entry.display_name,
                    peer_name=entry.display_name,
                    direction="outgoing",
                    call_id=invite.call_id,
                    reason=TerminalReason.TRANSPORT_UNREACHABLE.value,
                    terminal_reason=TerminalReason.TRANSPORT_UNREACHABLE.value,
                    origin="remote",
                    sip_status_code=480,
                    last_sip_event="SIP_RESPONSE",
                    route_kind=GROUP_TYPE_RING,
                )
            _sip_send_final_response(
                hass,
                invite.call_id,
                480,
                "Temporarily Unavailable",
                decline_reason=TerminalReason.TRANSPORT_UNREACHABLE.value,
            )
            registry.finish_and_pop(
                invite.call_id,
                reason=TerminalReason.TRANSPORT_UNREACHABLE.value,
                state=CallState.TRANSPORT_UNREACHABLE.value,
            )
            return

        async def _dial(attempt: OutboundLeg) -> tuple[str, OutboundLeg]:
            client = attempt.client
            uri = attempt.uri
            result = await client.invite(
                target=uri.user or attempt.member,
                remote_host=uri.host,
                remote_sip_port=uri.port or int(cfg["sip_port"]),
                request_uri=str(uri),
                timeout=8.0,
            )
            if result == "ringing":
                result = await client.wait_for_final(timeout=RING_GROUP_TIMEOUT_S)
            return result, attempt

        browser_decision: dict[str, Any] = {}

        async def _wait_browser() -> tuple[str, BrowserLeg | dict]:
            try:
                decision = await asyncio.wait_for(
                    route_future, timeout=RING_GROUP_TIMEOUT_S
                )
            except asyncio.TimeoutError:
                return "timeout", {"member": "__browser__", "browser": True}
            action = str((decision or {}).get("action") or "").strip().lower()
            browser_decision.update(decision or {})
            selected_endpoint_id = str(
                (decision or {}).get("endpoint_id") or ""
            ).strip()
            selected = next(
                (
                    leg
                    for leg in browser_legs
                    if leg.endpoint_id == selected_endpoint_id
                ),
                None,
            )
            if action in {"answer_ha", "default"}:
                if selected is None:
                    return "declined", {
                        "member": "__browser__",
                        "browser": True,
                    }
                return "in_call_browser", selected
            if action in {"forward", "bridge"}:
                return "reroute", dict(decision or {})
            if action == "busy":
                return "busy", selected or {
                    "member": "__browser__",
                    "browser": True,
                }
            if action == "cancel":
                return "cancelled", selected or {
                    "member": "__caller__",
                    "caller_control": True,
                }
            return "declined", selected or {
                "member": "__browser__",
                "browser": True,
            }

        async def _wait_caller_cancel() -> tuple[str, dict]:
            try:
                decision = await asyncio.wait_for(
                    route_future, timeout=RING_GROUP_TIMEOUT_S
                )
            except asyncio.TimeoutError:
                return "timeout", {"member": "__caller__", "caller_control": True}
            action = str((decision or {}).get("action") or "").strip().lower()
            return (
                "cancelled" if action == "cancel" else "ignored",
                {"member": "__caller__", "caller_control": True},
            )

        # DialForkController owns every branch task and its loser cleanup
        # barrier.  Keep this compatibility list empty for later rollback
        # helpers, which may still close the selected branch after media setup
        # fails but must never own the fork tasks themselves.
        tasks: list[asyncio.Task] = []

        def _outcome(result: str) -> DialOutcome:
            disposition = {
                "in_call": DialDisposition.ANSWERED,
                "in_call_browser": DialDisposition.ANSWERED,
                "busy": DialDisposition.BUSY,
                "dnd": DialDisposition.DND,
                "declined": DialDisposition.DECLINED,
                "timeout": DialDisposition.TIMEOUT,
                "media_incompatible": DialDisposition.MEDIA_INCOMPATIBLE,
                "auth_required_unsupported": DialDisposition.AUTH_FAILED,
                "proxy_auth_required_unsupported": DialDisposition.AUTH_FAILED,
                "cancelled": DialDisposition.CANCELLED,
                "reroute": DialDisposition.REROUTE,
            }.get(result, DialDisposition.UNAVAILABLE)
            return DialOutcome(disposition, reason=result)

        candidate_payloads: dict[str, OutboundLeg | BrowserLeg | dict] = {}
        fork_candidates: list[DialCandidate] = []
        for candidate_id, endpoint_id, disposition, tier, order in preflight_failures:
            async def _dial_preflight(
                result: DialDisposition = disposition,
            ) -> DialOutcome:
                return DialOutcome(result)

            async def _close_preflight(_mode: LegCloseMode) -> None:
                return None

            fork_candidates.append(
                DialCandidate(
                    candidate_id,
                    _dial_preflight,
                    _close_preflight,
                    tier=tier,
                    order=order,
                    endpoint_id=endpoint_id,
                )
            )
        for attempt in attempts:
            candidate_id = attempt.candidate_id or (
                f"sip:{attempt.client.dialog_ids.call_id}"
            )
            candidate_payloads[candidate_id] = attempt

            async def _dial_sip(
                outbound: OutboundLeg = attempt,
            ) -> DialOutcome:
                result, _attempt = await _dial(outbound)
                if result == "in_call" and outbound.client.dialog is None:
                    return DialOutcome(
                        DialDisposition.PROTOCOL_ERROR,
                        500,
                        "protocol_error",
                    )
                return _outcome(result)

            async def _close_sip(
                mode: LegCloseMode,
                outbound: OutboundLeg = attempt,
            ) -> None:
                await _close_outbound_leg(
                    outbound,
                    bye_or_cancel=mode
                    in {LegCloseMode.CANCEL_OR_BYE, LegCloseMode.BYE},
                )

            fork_candidates.append(
                DialCandidate(
                    candidate_id,
                    _dial_sip,
                    _close_sip,
                    tier=attempt.tier,
                    order=attempt.order,
                    endpoint_id=attempt.endpoint_id,
                )
            )

        control_tier = min(
            (candidate.tier for candidate in fork_candidates),
            default=0,
        )
        if browser_legs:
            browser_candidate_id = "browser:route-control"

            async def _dial_browser() -> DialOutcome:
                result, selected = await _wait_browser()
                candidate_payloads[browser_candidate_id] = selected
                if result == "cancelled":
                    return DialOutcome(
                        DialDisposition.SOURCE_CANCELLED,
                        487,
                        result,
                    )
                return _outcome(result)

            async def _close_browser(_mode: LegCloseMode) -> None:
                return None

            fork_candidates.append(
                DialCandidate(
                    browser_candidate_id,
                    _dial_browser,
                    _close_browser,
                    tier=control_tier,
                    order=-2,
                    control=True,
                )
            )
        else:
            caller_candidate_id = "caller:route-control"

            async def _dial_caller_control() -> DialOutcome:
                result, selected = await _wait_caller_cancel()
                candidate_payloads[caller_candidate_id] = selected
                if result == "cancelled":
                    return DialOutcome(
                        DialDisposition.SOURCE_CANCELLED,
                        487,
                        result,
                    )
                return _outcome(result)

            async def _close_caller_control(_mode: LegCloseMode) -> None:
                return None

            fork_candidates.append(
                DialCandidate(
                    caller_candidate_id,
                    _dial_caller_control,
                    _close_caller_control,
                    tier=control_tier,
                    order=-2,
                    control=True,
                )
            )

        async def _cleanup_ring_resources(reason: str) -> None:
            """Tear down every ownership layer after an aborted group call."""
            _pending_routes(hass).pop(invite.call_id, None)
            (
                _source_call_id,
                _dest_call_id,
                relay,
                bridge_client,
                watcher,
                _called_by_dest,
            ) = registry.detach_bridge(invite.call_id)
            if relay is not None or bridge_client is not None:
                current = asyncio.current_task()
                cleanup = asyncio.create_task(
                    async_cleanup_sip_runtime(
                        relay=relay,
                        client=bridge_client,
                        watcher=(watcher if watcher is not current else None),
                        terminate_client=True,
                        relay_first=True,
                    ),
                    name=f"voip-ring-group-bridge-cleanup-{invite.call_id}",
                )
                await async_wait_for_cleanup(cleanup)
            remaining_attempts = [
                attempt
                for attempt in attempts
                if attempt.client is not bridge_client
            ]
            await _cleanup_outbound_attempts(tasks, remaining_attempts)
            active_media = registry.take_media(invite.call_id)
            _release_media_reservation(active_media)

            # HA-to-HA ring groups switch to the transport-neutral local bridge
            # after selection.  If answer publication then fails, terminate
            # that newly-created call as part of the same rollback boundary.
            from .local_softphone_runtime import local_softphone_bridge

            local_bridge = local_softphone_bridge(hass)
            local_call = (
                local_bridge.get_call(invite.call_id)
                if local_bridge is not None
                else None
            )
            if local_call is not None:
                with contextlib.suppress(Exception):
                    local_bridge.hangup(
                        invite.call_id,
                        local_call.caller_endpoint_id,
                    )
            registry.finish_and_pop(
                invite.call_id,
                reason=reason,
                state=(
                    CallState.CANCELLED.value
                    if reason == TerminalReason.CANCELLED.value
                    else CallState.TRANSPORT_UNREACHABLE.value
                ),
            )

        async def _abort_stale_ring_group() -> bool:
            """Close every fork if the source generation lost ownership."""

            if _call_is_current():
                return False
            _settle_browser_candidates(
                CallState.CANCELLED.value,
                TerminalReason.CANCELLED.value,
            )
            await _cleanup_ring_resources(TerminalReason.CANCELLED.value)
            return True

        winner: OutboundLeg | BrowserLeg | dict | None = None
        browser_winner = False
        reroute_decision: dict[str, Any] | None = None
        final_result = "timeout"
        try:
            pbx_runtime = hass.data.get(DOMAIN, {}).get("pbx_runtime")
            authoritative_session = (
                pbx_runtime.get_session(
                    invite.call_id,
                    generation=call_generation,
                )
                if pbx_runtime is not None
                else None
            )
            if authoritative_session is None:
                await _cleanup_ring_resources(TerminalReason.CANCELLED.value)
                return
            fork_result = await DialForkController(
                authoritative_session,
                fork_candidates,
                strategy=ring_policy.strategy,
                tier_strategies=ring_policy.tier_strategies,
                overall_timeout=ring_policy.overall_timeout,
                step_timeout=ring_policy.step_timeout,
            ).run(
                lambda _candidate, _dial_outcome: _call_is_current()
            )
            if fork_result.winner is not None:
                winner = candidate_payloads.get(
                    fork_result.winner.candidate_id
                )
                browser_winner = isinstance(winner, BrowserLeg)
            elif fork_result.outcome.disposition is DialDisposition.REROUTE:
                reroute_decision = dict(browser_decision)
            final_result = {
                DialDisposition.BUSY: "busy",
                DialDisposition.DND: "dnd",
                DialDisposition.DECLINED: "declined",
                DialDisposition.TIMEOUT: "timeout",
                DialDisposition.MEDIA_INCOMPATIBLE: "media_incompatible",
                DialDisposition.AUTH_FAILED: "auth_required_unsupported",
                DialDisposition.CANCELLED: "cancelled",
                DialDisposition.SOURCE_CANCELLED: "cancelled",
                DialDisposition.PROTOCOL_ERROR: "protocol_error",
                DialDisposition.UNAVAILABLE: "transport_unreachable",
            }.get(fork_result.outcome.disposition, final_result)
            if await _abort_stale_ring_group():
                return
            winner_endpoint_id = str(
                getattr(winner, "endpoint_id", "") or ""
            )
            for losing_endpoint_id in {
                attempt.endpoint_id
                for attempt in attempts
                if attempt.endpoint_id
                and attempt.endpoint_id != winner_endpoint_id
            }:
                registry.release_endpoint_claim(
                    invite.call_id,
                    losing_endpoint_id,
                )
            if await _abort_stale_ring_group():
                return
            if reroute_decision is not None:
                route = _pending_routes(hass).pop(invite.call_id, None) or {}
                _settle_browser_candidates(
                    CallState.IDLE.value,
                    "forwarded",
                )
                handoff = route.get("forward_handoff")
                if handoff is not None and not handoff.done():
                    handoff.set_result(dict(reroute_decision))
                return
            if winner is not None:
                candidate_state = CallState.CANCELLED.value
                candidate_reason = TerminalReason.CANCELLED.value
            else:
                (
                    _candidate_status,
                    _candidate_sip_reason,
                    candidate_reason,
                    candidate_state,
                ) = _sip_failure_response(final_result)
            _settle_browser_candidates(
                candidate_state,
                candidate_reason,
                keep_endpoint_id=(
                    winner.endpoint_id
                    if browser_winner and isinstance(winner, BrowserLeg)
                    else ""
                ),
            )
            if winner is None:
                _pending_routes(hass).pop(invite.call_id, None)
                status_code, sip_reason, terminal_reason, public_state = (
                    _sip_failure_response(final_result)
                )
                if ha_origin:
                    _set_ha_softphone_call_state(
                        hass,
                        public_state,
                        endpoint_id=origin_endpoint_id,
                        session_device_id=origin_device_id,
                        caller=origin_name,
                        callee=entry.display_name,
                        peer_name=entry.display_name,
                        direction="outgoing",
                        call_id=invite.call_id,
                        reason=terminal_reason,
                        terminal_reason=terminal_reason,
                        origin="remote",
                        sip_status_code=status_code,
                        last_sip_event="SIP_RESPONSE",
                        route_kind=GROUP_TYPE_RING,
                    )
                _sip_send_final_response(
                    hass,
                    invite.call_id,
                    status_code,
                    sip_reason,
                    decline_reason=terminal_reason,
                )
                _set_sip_bridge_call_state(
                    hass,
                    public_state,
                    caller=invite.caller,
                    callee=invite.target,
                    peer_name=invite.target,
                    call_id=invite.call_id,
                    reason=terminal_reason,
                    terminal_reason=terminal_reason,
                    origin="remote",
                    sip_status_code=status_code,
                    last_sip_event="SIP_RESPONSE",
                    route_kind=GROUP_TYPE_RING,
                )
                registry.finish_and_pop(
                    invite.call_id,
                    reason=terminal_reason,
                    state=public_state,
                )
                return
            if browser_winner and isinstance(winner, BrowserLeg):
                if await _abort_stale_ring_group():
                    return
                _pending_routes(hass).pop(invite.call_id, None)
                connected_party = winner.name
                winner_media_client_id = str(
                    browser_decision.get("media_client_id") or ""
                ).strip()
                if ha_origin:
                    from .local_softphone_runtime import (
                        local_softphone_bridge,
                        start_local_softphone_call,
                    )

                    original_context = registry.ha_context(invite.call_id)
                    if await _abort_stale_ring_group():
                        return
                    registry.finish_and_pop(
                        invite.call_id,
                        reason="local_group_selected",
                        state=CallState.IDLE.value,
                    )
                    snapshot = start_local_softphone_call(
                        hass,
                        origin_endpoint_id,
                        winner.endpoint_id,
                        call_id=invite.call_id,
                        request_video=request_video,
                        enable_caller_video_send=enable_caller_video_send,
                        caller_owner_id=origin_media_client_id,
                        context=original_context,
                    )
                    bridge = local_softphone_bridge(hass)
                    if bridge is None:
                        raise RuntimeError("local softphone bridge is unavailable")
                    bridge.answer(
                        snapshot.call_id,
                        winner.endpoint_id,
                        winner_media_client_id,
                        enable_video_send=bool(
                            browser_decision.get("send_video", False)
                        ),
                    )
                    return
                local_rtp_port = _allocate_sip_rtp_port(hass)
                answer = build_answer_directional(
                    local_ip,
                    local_ip,
                    local_rtp_port,
                    invite.send_format,
                    invite.recv_format,
                    dtmf=_invite_dtmf_format(invite),
                    remote_sdp=invite.remote_sdp,
                )
                committed = registry.transition(
                    invite.call_id,
                    state=CallState.IN_CALL.value,
                    owner="ha_softphone",
                    caller=invite.caller,
                    callee=entry.display_name,
                    route_kind=GROUP_TYPE_RING,
                    expected_generation=call_generation,
                    endpoint_id=winner.endpoint_id,
                    dest_endpoint_id=winner.endpoint_id,
                    media_client_id=winner_media_client_id,
                )
                if committed is None:
                    await _cleanup_ring_resources(TerminalReason.CANCELLED.value)
                    return
                media = {
                    "invite": invite,
                    "local_rtp_port": local_rtp_port,
                    "endpoint_id": winner.endpoint_id,
                    "media_client_id": winner_media_client_id,
                }
                registry.pending_invites.pop(invite.call_id, None)
                registry.attach_media(invite.call_id, media)
                registry.add_leg(
                    invite.call_id,
                    f"browser:{winner.endpoint_id}",
                    role="ha_softphone",
                    state=CallState.IN_CALL.value,
                )
                if not _sip_send_final_response(
                    hass, invite.call_id, 200, "OK", answer_sdp=answer
                ):
                    await _cleanup_ring_resources(
                        TerminalReason.CANCELLED.value
                        if not _call_is_current()
                        else TerminalReason.PROTOCOL_ERROR.value
                    )
                    return
                _set_ha_softphone_call_state(
                    hass,
                    CallState.IN_CALL.value,
                    endpoint_id=winner.endpoint_id,
                    session_device_id=winner.device_id,
                    caller=invite.caller,
                    callee=entry.display_name,
                    peer_name=invite.caller,
                    direction="incoming",
                    call_id=invite.call_id,
                    selected_tx_format=invite.send_format.audio_format.wire_token(),
                    selected_rx_format=invite.recv_format.audio_format.wire_token(),
                    selected_tx_rtp_format=invite.send_format.wire_token(),
                    selected_rx_rtp_format=invite.recv_format.wire_token(),
                    audio_mode="full_duplex",
                    route_kind=GROUP_TYPE_RING,
                    sip_status_code=200,
                    last_sip_event="SIP_RESPONSE",
                )
                # Mirror the same established-call contract used when a SIP
                # endpoint wins: retain the group as dialed target and expose
                # the HA softphone as the party that actually answered.
                _set_sip_bridge_call_state(
                    hass,
                    CallState.IN_CALL.value,
                    caller=invite.caller,
                    callee=entry.display_name,
                    peer_name=connected_party,
                    call_id=invite.call_id,
                    dialed_target=entry.display_name,
                    connected_party=connected_party,
                    answered_by=connected_party,
                    selected_tx_format=invite.send_format.audio_format.wire_token(),
                    selected_rx_format=invite.recv_format.audio_format.wire_token(),
                    selected_tx_rtp_format=invite.send_format.wire_token(),
                    selected_rx_rtp_format=invite.recv_format.wire_token(),
                    sip_status_code=200,
                    last_sip_event="SIP_RESPONSE",
                    route_kind=GROUP_TYPE_RING,
                )
                return
            _pending_routes(hass).pop(invite.call_id, None)
            if not isinstance(winner, OutboundLeg):
                _LOGGER.error(
                    "SIP ring group selected an invalid winner for call_id=%s",
                    invite.call_id,
                )
                _sip_send_final_response(
                    hass,
                    invite.call_id,
                    500,
                    "Server Internal Error",
                    decline_reason=TerminalReason.PROTOCOL_ERROR.value,
                )
                if ha_origin:
                    _set_ha_softphone_call_state(
                        hass,
                        CallState.TRANSPORT_UNREACHABLE.value,
                        endpoint_id=origin_endpoint_id,
                        session_device_id=origin_device_id,
                        caller=origin_name,
                        callee=entry.display_name,
                        peer_name=entry.display_name,
                        direction="outgoing",
                        call_id=invite.call_id,
                        reason=TerminalReason.PROTOCOL_ERROR.value,
                        terminal_reason=TerminalReason.PROTOCOL_ERROR.value,
                        origin="self",
                        sip_status_code=500,
                        last_sip_event="SIP_RESPONSE",
                        route_kind=GROUP_TYPE_RING,
                    )
                registry.finish_and_pop(
                    invite.call_id,
                    reason=TerminalReason.PROTOCOL_ERROR.value,
                    state=CallState.TRANSPORT_UNREACHABLE.value,
                )
                return
            client = winner.client
            source_relay_port, dest_relay_port = winner.ports.ports
            video_answer = None
            if winner.video_relay is not None and client.dialog is not None:
                video_answer = configure_answered_invite_video_relay(
                    invite,
                    client.dialog,
                    winner.video_relay,
                )
                if video_answer is None:
                    _LOGGER.info(
                        "SIP ring group video rejected by winning branch "
                        "call_id=%s member=%s",
                        invite.call_id,
                        winner.member,
                    )
                    await winner.video_relay.stop()
                    winner.video_relay = None
                    winner.video_failure_reason = "remote_video_rejected"
            bridge_session = registry.register_bridge(
                source_call_id=invite.call_id,
                dest_call_id=client.dialog_ids.call_id,
                client=client,
                state=CallState.CONNECTING.value,
                caller=invite.caller,
                callee=invite.target,
                route_kind=GROUP_TYPE_RING,
                ingress=call_ingress,
                origin=call_ingress,
                source_state=CallState.CONNECTING.value,
                dest_state=CallState.IN_CALL.value,
                expected_generation=call_generation,
            )
            if bridge_session is None:
                await _close_outbound_leg(winner, bye_or_cancel=True)
                return
            try:
                if ha_origin:
                    relay = build_local_client_relay(
                        client=client,
                        local_host=local_ip,
                        local_to_relay_format=invite.recv_format,
                        relay_to_local_format=invite.send_format,
                        source_relay_port=source_relay_port,
                        dest_relay_port=dest_relay_port,
                        capture_name=f"{invite.call_id}_{client.dialog_ids.call_id}",
                        debug_capture=_debug_mode(hass),
                        on_release=lambda ports: _release_sip_rtp_port_pair(
                            hass, ports
                        ),
                    )
                else:
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
                    callee=str(winner.member or invite.target),
                    client=client,
                )
                if winner.video_relay is not None:
                    relay.attach_video_relay(winner.video_relay)
                await relay.start()
            except Exception as err:
                _LOGGER.warning("SIP ring group media bridge unavailable: %s", err)
                _sip_send_final_response(
                    hass,
                    invite.call_id,
                    488,
                    "Not Acceptable Here",
                    decline_reason=TerminalReason.MEDIA_INCOMPATIBLE.value,
                )
                if ha_origin:
                    _set_ha_softphone_call_state(
                        hass,
                        CallState.MEDIA_INCOMPATIBLE.value,
                        endpoint_id=origin_endpoint_id,
                        session_device_id=origin_device_id,
                        caller=origin_name,
                        callee=entry.display_name,
                        peer_name=str(winner.member or entry.display_name),
                        direction="outgoing",
                        call_id=invite.call_id,
                        reason=TerminalReason.MEDIA_INCOMPATIBLE.value,
                        terminal_reason=TerminalReason.MEDIA_INCOMPATIBLE.value,
                        origin="self",
                        sip_status_code=488,
                        last_sip_event="SIP_RESPONSE",
                        route_kind=GROUP_TYPE_RING,
                    )
                registry.discard_bridge_session(
                    invite.call_id,
                    client.dialog_ids.call_id,
                    reason=TerminalReason.MEDIA_INCOMPATIBLE.value,
                    state=CallState.MEDIA_INCOMPATIBLE.value,
                )
                await _close_outbound_leg(winner)
                return
            if not _call_is_current():
                await relay.stop()
                await _cleanup_ring_resources(TerminalReason.CANCELLED.value)
                return
            winner.ports.detach()
            if winner.video_relay is not None:
                # The audio relay now owns and tears down the video relay.
                winner.video_relay = None
            _attach_client_media_update(
                client,
                relay,
                source_call_id=invite.call_id,
            )
            dialed_target = entry.display_name or invite.target
            connected_party = str(winner.member or "").strip() or invite.target
            if ha_origin:
                # The synthetic HA caller has no SIP/RTP socket of its own.
                # Feed the already-running source side of the relay from the
                # authenticated browser websocket via a local UDP endpoint.
                softphone_media = {
                    "rtp_loopback": True,
                    "remote_rtp_host": local_ip,
                    "remote_rtp_port": source_relay_port,
                    "send_format": invite.recv_format,
                    "recv_format": invite.send_format,
                    "local_ssrc": secrets.randbelow(0xFFFFFFFF) + 1,
                    "endpoint_id": origin_endpoint_id,
                }
            else:
                softphone_media = None
            committed = registry.transition(
                invite.call_id,
                state=CallState.IN_CALL.value,
                owner="ha_softphone",
                caller=invite.caller,
                callee=dialed_target,
                route_kind=GROUP_TYPE_RING,
                endpoint_id=origin_endpoint_id if ha_origin else "",
                source_endpoint_id=source_endpoint_id,
                dest_endpoint_id=winner.endpoint_id,
                media_client_id=origin_media_client_id,
                expected_generation=call_generation,
            )
            if committed is None:
                await relay.stop()
                await _close_outbound_leg(winner, bye_or_cancel=True)
                return
            registry.attach_relay(invite.call_id, relay)
            if softphone_media is not None:
                registry.attach_media(invite.call_id, softphone_media)
            if not ha_origin:
                answer = build_answer_directional(
                    local_ip,
                    local_ip,
                    source_relay_port,
                    invite.send_format,
                    invite.recv_format,
                    dtmf=_invite_dtmf_format(invite),
                    remote_sdp=invite.remote_sdp,
                    video_port=(
                        relay.video_relay.left_port
                        if relay.video_relay is not None
                        else 0
                    ),
                    video_format=(
                        video_answer.video_format
                        if video_answer is not None
                        else None
                    ),
                    video_direction=(
                        video_answer.direction
                        if video_answer is not None
                        else "inactive"
                    ),
                )
                if not _sip_send_final_response(
                    hass,
                    invite.call_id,
                    200,
                    "OK",
                    answer_sdp=answer,
                ):
                    await _cleanup_ring_resources(
                        TerminalReason.CANCELLED.value
                        if not _call_is_current()
                        else TerminalReason.PROTOCOL_ERROR.value
                    )
                    return
            _set_sip_bridge_call_state(
                hass,
                CallState.IN_CALL.value,
                caller=invite.caller,
                callee=dialed_target,
                peer_name=connected_party,
                call_id=invite.call_id,
                dest_call_id=client.dialog_ids.call_id,
                dialed_target=dialed_target,
                connected_party=connected_party,
                answered_by=connected_party,
                selected_tx_format=invite.send_format.audio_format.wire_token(),
                selected_rx_format=invite.recv_format.audio_format.wire_token(),
                selected_tx_rtp_format=invite.send_format.wire_token(),
                selected_rx_rtp_format=invite.recv_format.wire_token(),
                sip_status_code=200,
                last_sip_event="SIP_RESPONSE",
                route_kind=GROUP_TYPE_RING,
                sip_uri=str(winner.uri),
            )
            if ha_origin:
                _set_ha_softphone_call_state(
                    hass,
                    CallState.IN_CALL.value,
                    endpoint_id=origin_endpoint_id,
                    session_device_id=origin_device_id,
                    caller=invite.caller,
                    callee=dialed_target,
                    peer_name=connected_party,
                    direction="outgoing",
                    call_id=invite.call_id,
                    dest_call_id=client.dialog_ids.call_id,
                    dialed_target=dialed_target,
                    connected_party=connected_party,
                    answered_by=connected_party,
                    selected_tx_format=invite.recv_format.audio_format.wire_token(),
                    selected_rx_format=invite.send_format.audio_format.wire_token(),
                    selected_tx_rtp_format=invite.recv_format.wire_token(),
                    selected_rx_rtp_format=invite.send_format.wire_token(),
                    audio_mode="full_duplex",
                    route_kind=GROUP_TYPE_RING,
                    sip_status_code=200,
                    last_sip_event="SIP_RESPONSE",
                    sip_uri=str(winner.uri),
                )
            current_task = asyncio.current_task()
            if current_task is not None:
                registry.attach_client_watcher(
                    client.dialog_ids.call_id,
                    current_task,
                )
            terminal = await client.wait_for_dialog_termination()
            terminal_reason = (
                TerminalReason.REMOTE_HANGUP.value
                if terminal == "remote_hangup"
                else _sip_terminal_reason(terminal, _sip_public_state(terminal))
            )
            await _terminate_sip_bridge(
                hass,
                client.dialog_ids.call_id,
                endpoint_id=(origin_endpoint_id if ha_origin else DEFAULT_ENDPOINT_ID),
                session_device_id=(
                    origin_device_id if ha_origin else HA_SOFTPHONE_DEVICE_ID
                ),
                terminal_reason=terminal_reason,
            )
        except asyncio.CancelledError:
            _settle_browser_candidates(
                CallState.CANCELLED.value,
                TerminalReason.CANCELLED.value,
            )
            if ha_origin:
                with contextlib.suppress(Exception):
                    _set_ha_softphone_call_state(
                        hass,
                        CallState.CANCELLED.value,
                        endpoint_id=origin_endpoint_id,
                        session_device_id=origin_device_id,
                        caller=origin_name,
                        callee=entry.display_name,
                        peer_name=entry.display_name,
                        direction="outgoing",
                        call_id=invite.call_id,
                        reason=TerminalReason.CANCELLED.value,
                        terminal_reason=TerminalReason.CANCELLED.value,
                        origin="self",
                        last_sip_event="CANCEL",
                        route_kind=GROUP_TYPE_RING,
                    )
            await _cleanup_ring_resources(TerminalReason.CANCELLED.value)
            raise
        except Exception as err:
            _LOGGER.exception(
                "SIP ring group runtime failed call_id=%s: %s",
                invite.call_id,
                err,
            )
            _settle_browser_candidates(
                CallState.TRANSPORT_UNREACHABLE.value,
                TerminalReason.PROTOCOL_ERROR.value,
            )
            if ha_origin:
                with contextlib.suppress(Exception):
                    _set_ha_softphone_call_state(
                        hass,
                        CallState.TRANSPORT_UNREACHABLE.value,
                        endpoint_id=origin_endpoint_id,
                        session_device_id=origin_device_id,
                        caller=origin_name,
                        callee=entry.display_name,
                        peer_name=entry.display_name,
                        direction="outgoing",
                        call_id=invite.call_id,
                        reason=TerminalReason.PROTOCOL_ERROR.value,
                        terminal_reason=TerminalReason.PROTOCOL_ERROR.value,
                        origin="self",
                        sip_status_code=500,
                        last_sip_event="SIP_RESPONSE",
                        route_kind=GROUP_TYPE_RING,
                    )
            _sip_send_final_response(
                hass,
                invite.call_id,
                500,
                "Server Internal Error",
                decline_reason=TerminalReason.PROTOCOL_ERROR.value,
            )
            await _cleanup_ring_resources(TerminalReason.PROTOCOL_ERROR.value)

    async def _ring_conference_members(
        *,
        room_name: str,
        caller: str,
        source_host: str,
        entry: RosterEntry,
        peers: list[Peer],
        roster_entries: list[RosterEntry],
        owner_call_id: str = "",
    ) -> None:
        manager = conference_manager(
            hass,
            local_ip=local_ip,
            on_inbound_timeout=_on_conference_inbound_timeout,
        )
        registry = _call_registry(hass)
        endpoint_registry = hass.data.get(DOMAIN, {}).get("endpoint_registry")
        owner_session = registry.sessions.get(
            registry.resolve_session_id(str(owner_call_id or "").strip())
        )
        source_endpoint_id = str(
            ((owner_session.metadata if owner_session is not None else {}) or {}).get(
                "source_endpoint_id"
            )
            or ((owner_session.metadata if owner_session is not None else {}) or {}).get(
                "endpoint_id"
            )
            or ""
        ).strip()
        room = manager.rooms.get(str(room_name or "").strip())
        available_legs = max(
            0,
            MAX_CONFERENCE_LEGS
            - (len(room.legs) if room is not None and not room._closed else 0),
        )
        members = _unique_group_members(entry.metadata.get("ring_members"))
        attempts: list[OutboundLeg] = []
        browser_endpoint_ids: list[str] = []
        for member in members:
            if _caller_matches_member(
                caller,
                source_host,
                member,
                peers,
                source_endpoint_id=source_endpoint_id,
            ):
                continue
            browser_leg = _browser_leg_for_member(member, peers, roster_entries)
            if browser_leg is not None:
                if (
                    browser_leg.endpoint_id != source_endpoint_id
                    and browser_leg.endpoint_id not in browser_endpoint_ids
                    and len(browser_endpoint_ids) + len(attempts) < available_legs
                ):
                    browser_endpoint_ids.append(browser_leg.endpoint_id)
                continue
            if len(browser_endpoint_ids) + len(attempts) >= available_legs:
                _LOGGER.warning(
                    "SIP conference %s has no capacity for additional ring members; excess members were skipped",
                    room_name,
                )
                break
            try:
                leg = _prepare_outbound_leg(
                    member=member,
                    peers=peers,
                    roster_entries=roster_entries,
                    local_name=room_name,
                    local_rtp_port_index=0,
                )
            except RuntimeError as err:
                _LOGGER.warning(
                    "SIP conference member RTP port allocation failed member=%s: %s",
                    member,
                    err,
                )
                break
            if leg is not None:
                if leg.endpoint_id == source_endpoint_id:
                    await _close_outbound_leg(leg)
                    continue
                endpoint = (
                    endpoint_registry.get(leg.endpoint_id)
                    if endpoint_registry is not None and leg.endpoint_id
                    else None
                )
                if endpoint is not None and (
                    endpoint.dnd
                    or endpoint.availability
                    is not EndpointAvailability.AVAILABLE
                ):
                    await _close_outbound_leg(leg)
                    continue
                leg_call_id = leg.client.dialog_ids.call_id
                registry.upsert(
                    leg_call_id,
                    state=CallState.CALLING.value,
                    owner="bridge",
                    caller=room_name,
                    callee=member,
                    route_kind=GROUP_TYPE_CONFERENCE,
                    source_call_id=owner_call_id,
                    dest_endpoint_id=leg.endpoint_id,
                )
                try:
                    if leg.endpoint_id:
                        registry.claim_endpoint(
                            leg_call_id,
                            leg.endpoint_id,
                            role="conference_member",
                            adopt_transport=(
                                endpoint is not None
                                and endpoint.kind is EndpointKind.ESPHOME
                            ),
                        )
                except EndpointBusyError:
                    registry.finish_and_pop(
                        leg_call_id,
                        reason=TerminalReason.BUSY.value,
                        state=CallState.BUSY.value,
                    )
                    await _close_outbound_leg(leg)
                    continue
                attempts.append(leg)

        if browser_endpoint_ids:
            manager.ring_ha_endpoints(
                room_name,
                tuple(browser_endpoint_ids),
                caller=caller,
            )

        async def _dial(attempt: OutboundLeg) -> None:
            client = attempt.client
            uri = attempt.uri
            owned_by_room = False
            cleanup_reason = TerminalReason.TRANSPORT_UNREACHABLE.value
            try:
                result = await client.invite(
                    target=uri.user or attempt.member,
                    remote_host=uri.host,
                    remote_sip_port=uri.port or int(cfg["sip_port"]),
                    request_uri=str(uri),
                    timeout=8.0,
                )
                if result == "ringing":
                    result = await client.wait_for_final(timeout=RING_GROUP_TIMEOUT_S)
                if result != "in_call" or client.dialog is None:
                    return
                owned_by_room = await manager.add_client_leg(
                    room_name,
                    call_id=client.dialog_ids.call_id,
                    caller=attempt.member,
                    client=client,
                    port_reservation=attempt.ports,
                    role="auto_invited",
                )
                if not owned_by_room:
                    return
                terminal = await client.wait_for_dialog_termination()
                terminal_reason = (
                    TerminalReason.REMOTE_HANGUP.value
                    if terminal == "remote_hangup"
                    else _sip_terminal_reason(terminal, _sip_public_state(terminal))
                )
                cleanup_reason = terminal_reason
                await manager.leave_call(
                    client.dialog_ids.call_id, reason=terminal_reason
                )
                registry.finish_and_pop(
                    client.dialog_ids.call_id,
                    reason=terminal_reason,
                    state=CallState.IDLE.value,
                )
            except asyncio.CancelledError:
                cleanup_reason = TerminalReason.CANCELLED.value
                raise
            except Exception as err:
                _LOGGER.debug(
                    "SIP conference member invite failed member=%s: %s",
                    attempt.member,
                    err,
                )
            finally:
                if owned_by_room:
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        await manager.leave_call(
                            client.dialog_ids.call_id,
                            reason=cleanup_reason,
                        )
                    registry.finish_and_pop(
                        client.dialog_ids.call_id,
                        reason=cleanup_reason,
                        state=CallState.IDLE.value,
                    )
                else:
                    with contextlib.suppress(Exception):
                        await _close_outbound_leg(attempt, bye_or_cancel=True)
                    registry.finish_and_pop(
                        attempt.client.dialog_ids.call_id,
                        reason=TerminalReason.TRANSPORT_UNREACHABLE.value,
                        state=CallState.TRANSPORT_UNREACHABLE.value,
                    )

        await asyncio.gather(
            *(_dial(attempt) for attempt in attempts), return_exceptions=True
        )

    async def _ring_conference_members_from_ha(
        entry: RosterEntry,
        *,
        owner_call_id: str = "",
    ) -> None:
        peers = await _async_build_peer_snapshot(hass)
        roster_entries = _roster_from_peers(
            hass, peers, _registered_roster_entries(hass)
        )
        room_name = str(entry.name or entry.id or "")
        await _ring_conference_members(
            room_name=room_name,
            caller=_ha_peer_name(hass),
            source_host=local_ip,
            entry=entry,
            peers=peers,
            roster_entries=roster_entries,
            owner_call_id=owner_call_id,
        )

    async def _start_ring_group_from_ha(
        entry: RosterEntry,
        *,
        context: Any | None = None,
        endpoint_id: str = DEFAULT_ENDPOINT_ID,
        media_client_id: str = "",
        request_video: bool = False,
        enable_caller_video_send: bool = False,
    ) -> str:
        endpoint_id = str(endpoint_id or DEFAULT_ENDPOINT_ID).strip() or DEFAULT_ENDPOINT_ID
        endpoint_registry = hass.data.get(DOMAIN, {}).get("endpoint_registry")
        browser_endpoint = (
            endpoint_registry.get(endpoint_id)
            if endpoint_registry is not None
            else None
        )
        if (
            browser_endpoint is not None
            and browser_endpoint.kind is not EndpointKind.BROWSER
        ):
            raise ValueError(f"endpoint {endpoint_id!r} is not a browser phone")
        local_name = str(
            getattr(browser_endpoint, "name", "") or _ha_peer_name(hass)
        ).strip()
        endpoint_device_id = str(
            getattr(browser_endpoint, "device_id", "") or HA_SOFTPHONE_DEVICE_ID
        )
        group_name = str(entry.name or entry.id or "")
        # A timestamp is not a dialog identifier: two phones can start in the
        # same millisecond.  Use cryptographic entropy just like the normal SIP
        # client path so concurrent HA callers cannot alias one registry entry.
        call_id = f"ha-{secrets.token_hex(16)}"
        send_format = next(
            fmt
            for fmt in HA_SIP_PCM_TX_FORMATS
            if fmt.channels == 1 and fmt.nominal_frame_bytes <= 1200
        )
        recv_format = next(
            fmt
            for fmt in HA_SIP_PCM_RX_FORMATS
            if fmt.channels == 1 and fmt.nominal_frame_bytes <= 1200
        )
        invite = SipInvite(
            source_host=local_ip,
            source_port=int(cfg["sip_port"]),
            request_uri=parse_sip_uri(
                f"sip:{group_name.replace(' ', '_')}@{local_ip};transport=tcp"
            ),
            caller_uri=parse_sip_uri(
                f"sip:{local_name.replace(' ', '_')}@{local_ip};transport=tcp"
            ),
            target=group_name,
            caller=local_name,
            call_id=call_id,
            cseq="1 INVITE",
            remote_sdp=b"",
            send_format=sip_sdp.audio_format_to_rtp(send_format, 96),
            recv_format=sip_sdp.audio_format_to_rtp(recv_format, 96),
            remote_rtp_host=local_ip,
            remote_rtp_port=0,
        )
        registry = _call_registry(hass)
        registry.upsert(
            call_id,
            state=CallState.RINGING.value,
            owner="ha_softphone",
            caller=local_name,
            callee=group_name,
            route_kind=GROUP_TYPE_RING,
            endpoint_id=endpoint_id,
            session_device_id=endpoint_device_id,
            source_endpoint_id=endpoint_id,
            media_client_id=str(media_client_id or "").strip(),
        )
        try:
            registry.claim_endpoint(call_id, endpoint_id, role="source")
        except EndpointBusyError:
            registry.finish_and_pop(
                call_id,
                reason=TerminalReason.BUSY.value,
                state=CallState.BUSY.value,
            )
            raise
        registry.bind_controller(
            call_id,
            context=context,
            endpoint_id=endpoint_id,
        )
        registry.add_leg(
            call_id, call_id, role="ha_softphone", state=CallState.REMOTE_RINGING.value
        )
        _set_ha_softphone_call_state(
            hass,
            CallState.REMOTE_RINGING.value,
            endpoint_id=endpoint_id,
            session_device_id=endpoint_device_id,
            caller=local_name,
            callee=group_name,
            peer_name=group_name,
            direction="outgoing",
            call_id=call_id,
            route_kind=GROUP_TYPE_RING,
            sip_status_code=180,
            last_sip_event="LOCAL_RING_GROUP",
        )
        try:
            peers = await _async_build_peer_snapshot(hass)
            roster_entries = _roster_from_peers(
                hass, peers, _registered_roster_entries(hass)
            )
        except Exception:
            _set_ha_softphone_call_state(
                hass,
                CallState.TRANSPORT_UNREACHABLE.value,
                endpoint_id=endpoint_id,
                session_device_id=endpoint_device_id,
                call_id=call_id,
                caller=local_name,
                callee=group_name,
                peer_name=group_name,
                direction="outgoing",
                reason=TerminalReason.TRANSPORT_UNREACHABLE.value,
                route_kind=GROUP_TYPE_RING,
                last_sip_event="PEER_SNAPSHOT_FAILED",
            )
            registry.finish_and_pop(
                call_id,
                reason=TerminalReason.TRANSPORT_UNREACHABLE.value,
                state=CallState.TRANSPORT_UNREACHABLE.value,
            )
            raise
        create_runtime_task(
            hass,
            _run_ring_group_call(
                invite,
                entry,
                peers,
                roster_entries,
                origin_endpoint_id=endpoint_id,
                origin_media_client_id=str(media_client_id or "").strip(),
                request_video=bool(request_video),
                enable_caller_video_send=bool(enable_caller_video_send),
            ),
        )
        return call_id

    hass.data.setdefault(DOMAIN, {})["async_ring_conference_members"] = _ring_conference_members_from_ha
    hass.data.setdefault(DOMAIN, {})["async_start_ring_group_from_ha"] = _start_ring_group_from_ha

    async def _on_invite(invite: SipInvite) -> SipInviteResult:
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
                source_video_enabled = (
                    source_endpoint is None or source_endpoint.supports("video")
                )
                target_video_enabled = (
                    target_endpoint is None or target_endpoint.supports("video")
                )
                if (
                    bool(cfg.get(CONF_SIP_VIDEO, False))
                    and invite.video_format is not None
                    and source_video_enabled
                    and target_video_enabled
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

    async def _on_media_update(
        previous: SipInvite,
        updated: SipInvite,
        method: str,
    ) -> SipInviteResult:
        return await async_prepare_media_update(
            hass,
            local_ip,
            previous,
            updated,
            method,
        )

    async def _on_terminated(call_id: str, reason: str = "remote_hangup") -> None:
        bucket = hass.data.setdefault(DOMAIN, {})
        registry = _call_registry(hass)
        if not registry.begin_termination(call_id):
            _LOGGER.debug(
                "Ignoring duplicate SIP termination call_id=%s reason=%s",
                call_id,
                reason,
            )
            return
        forward_task = bucket.setdefault("forward_tasks", {}).get(call_id)
        if forward_task is not None and forward_task is not asyncio.current_task():
            forward_task.cancel()
            await asyncio.gather(forward_task, return_exceptions=True)
        bucket.setdefault("trunk_info_queues", {}).pop(call_id, None)
        route = _pending_routes(hass).pop(call_id, None)
        closed_calls = bucket.setdefault("trunk_closed_calls", set())
        if len(closed_calls) >= 256:
            closed_calls.pop()
        closed_calls.add(call_id)
        if route is not None:
            future = route.get("future")
            if future is not None and not future.done():
                future.set_result(
                    {
                        "action": "cancel",
                        "reason": "Request Terminated",
                        "decline_reason": reason or TerminalReason.CANCELLED.value,
                    }
                )
        pending = registry.pending_invites
        invite = pending.pop(call_id, None)
        preanswered_item = registry.take_media(call_id, provisional=True)
        _release_media_reservation(preanswered_item)
        active_media = registry.take_media(call_id, default={})
        _release_media_reservation(active_media)
        active_media_invite = active_media.get("invite")
        if invite is None:
            invite = active_media_invite
        session = registry.sessions.get(registry.resolve_session_id(call_id))
        source_call_id, dest_call_id, relay, client, watcher, _called_by_dest = (
            registry.detach_bridge(call_id)
        )
        if source_call_id:
            call_id = source_call_id
        event_caller = invite.caller if invite is not None else (session.caller if session is not None else "")
        event_callee = (
            session.callee
            if session is not None and session.callee
            else invite.target
            if invite is not None
            else ""
        )
        session_metadata = session.metadata if session is not None else {}
        session_endpoint_id = str(
            session_metadata.get("endpoint_id") or DEFAULT_ENDPOINT_ID
        ).strip() or DEFAULT_ENDPOINT_ID
        endpoint_registry = bucket.get("endpoint_registry")
        session_endpoint = (
            endpoint_registry.get(session_endpoint_id)
            if endpoint_registry is not None
            else None
        )
        session_device_id = str(
            session_metadata.get("session_device_id")
            or getattr(session_endpoint, "device_id", "")
            or HA_SOFTPHONE_DEVICE_ID
        )
        softphone_store = _ha_softphone_store(hass, session_endpoint_id)
        softphone_call_id = str(softphone_store.get("call_id") or "")
        terminal_reason = reason or "remote_hangup"
        terminal_state = (
            CallState.CANCELLED.value
            if terminal_reason == TerminalReason.CANCELLED.value
            else CallState.IDLE.value
        )
        manager = bucket.get("conference_manager")
        if manager is not None and await manager.leave_call(
            call_id, reason=terminal_reason
        ):
            registry.finish_and_pop(
                call_id, reason=terminal_reason, state=terminal_state
            )
            return
        if relay is not None or client is not None:
            await async_cleanup_sip_runtime(
                relay=relay,
                client=client,
                watcher=watcher,
                terminate_client=True,
                relay_first=False,
            )
            _set_sip_bridge_call_state(
                hass,
                terminal_state,
                call_id=call_id,
                dest_call_id=dest_call_id,
                caller=event_caller,
                callee=event_callee,
                peer_name=event_callee,
                target=event_callee,
                reason=terminal_reason,
                terminal_reason=terminal_reason,
                origin="remote",
                last_sip_event="BYE",
            )
        elif (
            relay is None
            and client is None
            and (invite is not None or (call_id and softphone_call_id == call_id))
        ):
            _set_ha_softphone_call_state(
                hass,
                terminal_state,
                endpoint_id=session_endpoint_id,
                session_device_id=session_device_id,
                caller=(invite.caller if invite is not None else ""),
                callee=(invite.target if invite is not None else _ha_peer_name(hass)),
                peer_name=(invite.caller if invite is not None else ""),
                direction="incoming",
                call_id=call_id,
                reason=terminal_reason,
                origin="remote",
            )
        elif session is not None:
            # A caller can cancel while a router-owned fork has only early
            # outbound legs. There is then no bridge or browser media object,
            # but the logical session still owes observers one terminal event.
            _set_sip_bridge_call_state(
                hass,
                terminal_state,
                call_id=call_id,
                caller=event_caller,
                callee=event_callee,
                peer_name=event_callee,
                target=event_callee,
                reason=terminal_reason,
                terminal_reason=terminal_reason,
                origin="remote",
                last_sip_event=(
                    "CANCEL"
                    if terminal_reason == TerminalReason.CANCELLED.value
                    else "BYE"
                ),
                route_kind=session.route_kind,
            )
        # ``begin_termination`` makes this callback the sole teardown owner.
        # Finalize exactly once even when the transport reports a call which
        # has no remaining relay, client, pending INVITE or matching browser
        # store.  Leaving that tombstoned session in the registry held endpoint
        # busy forever and made subsequent calls look unrelatedly occupied.
        registry.finish_and_pop(
            call_id, reason=terminal_reason, state=terminal_state
        )
        if relay is not None or client is not None:
            _LOGGER.info(
                "SIP bridge terminated call_id=%s reason=%s relay=%s dest_client=%s",
                call_id,
                terminal_reason,
                relay is not None,
                client is not None,
            )

    supported_formats = list(HA_SIP_PCM_FORMATS)
    endpoint = SipEndpointManager(
        host="0.0.0.0",
        port=int(cfg["sip_port"]),
        local_ip=local_ip,
        local_rtp_port=int(cfg["rtp_port"]),
        supported_formats=supported_formats,
        supported_send_formats=list(HA_SIP_PCM_TX_FORMATS),
        supported_recv_formats=list(HA_SIP_PCM_RX_FORMATS),
        on_invite=_on_invite,
        on_terminated=_on_terminated,
        on_register=_on_register,
        on_info=_on_info,
        on_media_update=_on_media_update,
        udp_enabled=True,
        tcp_enabled=True,
        enable_video=bool(cfg.get(CONF_SIP_VIDEO, False)),
        enable_video_transcoding=bool(cfg.get(CONF_VIDEO_TRANSCODING, False)),
        prefer_browser_video_send=bool(cfg.get(CONF_VIDEO_CAMERA_SEND, False)),
    )
    # Atomic ownership cutover: the runtime and registry are authoritative
    # before either listener can dispatch its first INVITE.  The two component
    # names expose both transports while only one closer stops their shared
    # SipEndpointManager instance.
    pbx_runtime.attach_component("tcp_listener", endpoint)
    pbx_runtime.attach_component("udp_listener", endpoint, closer=endpoint.stop)
    pbx_runtime.activate()
    registry.bind_session_owner(pbx_runtime)
    bucket["pbx_runtime"] = pbx_runtime
    try:
        started = await endpoint.start()
    except BaseException:
        registry.bind_session_owner(None)
        await pbx_runtime.shutdown()
        if bucket.get("pbx_runtime") is pbx_runtime:
            bucket.pop("pbx_runtime", None)
        raise
    if not started:
        registry.bind_session_owner(None)
        await pbx_runtime.shutdown()
        if bucket.get("pbx_runtime") is pbx_runtime:
            bucket.pop("pbx_runtime", None)
        return False
    hass.data[DOMAIN]["async_forward_call"] = _async_forward_existing_call
    hass.data[DOMAIN]["sip_endpoint"] = endpoint
    hass.data[DOMAIN]["sip_server"] = endpoint.udp_server
    hass.data[DOMAIN]["sip_tcp_server"] = endpoint.tcp_server
    _LOGGER.info(
        "SIP endpoint enabled on UDP+TCP/%s (RTP base %s)",
        cfg["sip_port"],
        cfg["rtp_port"],
    )
    return True
