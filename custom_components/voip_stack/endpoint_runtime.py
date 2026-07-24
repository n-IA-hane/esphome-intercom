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
from .call_forwarder import ForwardRuntime, async_forward_existing_call
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
    release_sip_rtp_port_pair as _release_sip_rtp_port_pair,
    reserve_sip_video_media,
    reserve_sip_video_relay_media,
)
from .media_renegotiation import async_prepare_media_update
from .outbound_attempts import (
    BrowserLeg,
    OutboundLeg,
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
    caller_matches_group_member as _caller_matches_member,
    roster_entry_for_target as _roster_entry_for_target,
    unique_group_members as _unique_group_members,
)
from .phonebook_runtime import registered_roster_entries as _registered_roster_entries
from .router import (
    RouteAction,
    RouteReason,
)
from .ring_group_orchestrator import RingGroupRuntime, run_ring_group_call
from .session_cleanup import async_cleanup_sip_runtime
from .sip_bridge import (
    build_pending_invite_video_relay,
    build_invite_client_relay,
    configure_answered_invite_video_relay,
    dialog_rtp_peer,
    dialog_video_rtp_peer,
)
from .store import sip_accounts as _sip_accounts
from .trunk_inbound_router import (
    TrunkInboundRuntime,
    async_route_trunk_invite,
)
from .websocket_api import (
    _ha_softphone_store,
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


    async def _async_forward_existing_call(
        *,
        call_id: str,
        destination: str,
        on_failure: str = "resume",
        expected_state: str = "",
        expected_sequence: int = 0,
        initial_selection: bool = False,
    ) -> None:
        await async_forward_existing_call(
            ForwardRuntime(
                hass=hass,
                config=cfg,
                local_ip=local_ip,
                route_resolver=route_resolver,
                attach_client_media_update=_attach_client_media_update,
                browser_leg_for_member=_browser_leg_for_member,
                defer_invite_to_softphone=_defer_invite_to_ha_softphone,
                prepare_outbound_leg=_prepare_outbound_leg,
                publish_pending_ringing=_publish_pending_ha_softphone_ringing,
                sip_uri_for_member=_sip_uri_for_member,
                start_local_assist_bridge=_start_local_assist_bridge,
            ),
            call_id=call_id,
            destination=destination,
            on_failure=on_failure,
            expected_state=expected_state,
            expected_sequence=expected_sequence,
            initial_selection=initial_selection,
        )

    async def _run_trunk_inbound_route_guarded(
        invite: SipInvite,
        *,
        bridge_ports: RtpPortReservation,
    ) -> None:
        """Fail one detached trunk route closed and release all ownership."""

        try:
            await async_route_trunk_invite(
                TrunkInboundRuntime(
                    hass=hass,
                    config=cfg,
                    local_ip=local_ip,
                    ha_peer_name=_ha_peer_name(hass),
                    route_resolver=route_resolver,
                    forward_existing_call=_async_forward_existing_call,
                    defer_invite_to_softphone=_defer_invite_to_ha_softphone,
                    start_local_assist_bridge=_start_local_assist_bridge,
                    attach_client_media_update=_attach_client_media_update,
                    attach_dtmf_event_bridge=_attach_dtmf_event_bridge,
                ),
                invite,
                bridge_ports=bridge_ports,
            )
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
        await run_ring_group_call(
            RingGroupRuntime(
                hass=hass,
                config=cfg,
                local_ip=local_ip,
                ha_peer_name=_ha_peer_name,
                browser_leg_for_member=_browser_leg_for_member,
                logical_endpoint_for_member=_logical_endpoint_for_member,
                prepare_outbound_leg=_prepare_outbound_leg,
                attach_client_media_update=_attach_client_media_update,
                terminate_sip_bridge=_terminate_sip_bridge,
            ),
            invite,
            entry,
            peers,
            roster_entries,
            origin_endpoint_id=origin_endpoint_id,
            origin_media_client_id=origin_media_client_id,
            request_video=request_video,
            enable_caller_video_send=enable_caller_video_send,
        )

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
