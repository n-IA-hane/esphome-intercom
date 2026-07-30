"""Composition root for the HA-side SIP endpoint and B2BUA adapters.

This module wires transports, routing, media owners and HA projections
together.  It is intentionally not another call-state authority:
``SipEndpointRuntime`` owns logical PBX lifetimes, SIP listener/client objects
own transactions and dialogs, and staged media callbacks own offer/answer
commit or rollback.  Keep new policy in the focused domain modules and pass it
through the runtime dataclasses instead of rebuilding lifecycle state here.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
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
from .call_forwarder import ForwardRuntime, async_forward_existing_call
from .config_entry_runtime import (
    async_refresh_and_push_phonebook as _refresh_and_push_phonebook,
)
from .const import (
    CONF_ASSIST_ADVANCED_CALL_CONTEXT,
    CONF_ASSIST_PIPELINE,
    CONF_SIP_VIDEO,
    CONF_REGISTRAR_ENABLED,
    CONF_VIDEO_CAMERA_SEND,
    CONF_VIDEO_TRANSCODING,
    DOMAIN,
    HA_SOFTPHONE_DEVICE_ID,
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
    sip_public_state as _sip_public_state,
    sip_terminal_reason as _sip_terminal_reason,
)
from .media_ports import (
    RtpPortReservation,
    release_media_reservation as _release_media_reservation,
    release_sip_rtp_port_pair as _release_sip_rtp_port_pair,
    reserve_sip_video_relay_media,
)
from .media_renegotiation import async_prepare_media_update
from .invite_router import InviteRuntime, route_invite
from .outbound_attempts import (
    BrowserLeg,
    OutboundLeg,
    async_close_outbound_leg as _close_outbound_leg,
)
from .endpoint_registry import EndpointBusyError
from .phone_endpoint import (
    DEFAULT_ENDPOINT_ID,
    EndpointAvailability,
    EndpointKind,
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
        video_formats_passthrough_compatible,
    )
    from .sip import parse_sip_uri
    from .sip_client import SipCallClient
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
    # The explicit runtime owns call generations and ordered cleanup.  The
    # registry remains the observable/resource compatibility index consumed by
    # existing HA adapters; binding the projection keeps both views correlated
    # without creating two independent FSMs.
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
        peer_user_agent_override: str = "",
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
                peer_user_agent=(
                    str(peer_user_agent_override or "").strip()
                    or str(
                        (
                            (member_entry.metadata or {}).get("user_agent")
                            if member_entry is not None
                            else ""
                        )
                        or ""
                    ).strip()
                ),
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
        return await route_invite(
            InviteRuntime(
                hass=hass,
                config=cfg,
                local_ip=local_ip,
                registrar=registrar,
                ha_peer_name=_ha_peer_name,
                get_trunk_config=_get_trunk_config,
                trunk_enabled=_trunk_enabled,
                is_trunk_invite=_is_trunk_invite,
                is_ha_target=_is_ha_target,
                ha_router_decision=_ha_router_decision,
                inbound_route_decision=_inbound_route_decision,
                build_peer_snapshot=_async_build_peer_snapshot,
                attach_client_media_update=_attach_client_media_update,
                browser_leg_for_member=_browser_leg_for_member,
                defer_invite_to_softphone=_defer_invite_to_ha_softphone,
                enable_reused_sip_tcp_connection=_enable_reused_sip_tcp_connection,
                on_conference_inbound_timeout=_on_conference_inbound_timeout,
                ring_conference_members=_ring_conference_members,
                run_ring_group_call=_run_ring_group_call,
                run_trunk_inbound_route_guarded=_run_trunk_inbound_route_guarded,
                send_final_response=_sip_send_final_response,
                sip_uri_transport=_sip_uri_transport,
                start_local_assist_bridge=_start_local_assist_bridge,
                terminate_sip_bridge=_terminate_sip_bridge,
            ),
            invite,
        )

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
