"""Inbound trunk call orchestration outside the endpoint listener closure."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Any, Awaitable, Callable

from homeassistant.core import HomeAssistant

from .call_projection import publish_bridge_projection
from .config import trunk_config
from .const import (
    CONF_AUTOMATION_ROUTING_ENABLED,
    CONF_TRUNK_DTMF_ENABLED,
    CONF_TRUNK_DTMF_TERMINATOR,
    CONF_TRUNK_DTMF_TIMEOUT_MS,
    CONF_TRUNK_INBOUND_DEFAULT_TARGET,
    CONF_SIP_VIDEO,
    CONF_VIDEO_TRANSCODING,
)
from .endpoint_lifecycle import call_registry
from .endpoint_termination import EndpointTerminationHandler
from .endpoint_session import TerminationInitiator, TerminationIntent
from .endpoint_routing import (
    EndpointRouteResolver,
    peer_audio_formats,
    peer_for_target,
    peer_video_codec,
    roster_entry_formats,
    roster_from_peers,
    sip_target_audio_profile,
    supports_directional_audio_payloads,
)
from .fsm import (
    CallState,
    TerminalReason,
    sip_public_state,
    sip_terminal_reason,
)
from .inbound_answer import async_commit_runtime_answer
from .media_ports import (
    RtpPortReservation,
    release_video_media_reservation,
    reserve_sip_video_media,
)
from .outbound_attempts import OutboundLeg, async_close_client_and_release
from .outbound_bridge_commit import (
    BridgeCommitData,
    BridgeCommitPolicy,
    async_commit_outbound_bridge,
)
from .pbx_routing import dtmf_extension_routes
from .peer_snapshot import async_build_peer_snapshot
from .phone_endpoint import EndpointKind
from .peer import sip_uri_for_peer
from .phonebook_runtime import registered_roster_entries
from .router import CallContext, RouteAction, RouteReason, route_inbound_trunk
from .runtime_data import call_runtime_artifacts, preferred_browser_phone
from .core.sip import parse_sip_uri
from .sip_bridge import build_pending_invite_video_relay, video_bridge_offer_formats
from .sip_client import SipCallClient
from .sip_runtime import (
    enable_reused_tcp_connection,
    send_final_response,
    uri_transport,
)
from .trunk_dtmf import collect_trunk_dtmf
from .trunk_routing import async_request_inbound_destination, trunk_default_target

_LOGGER = logging.getLogger(__name__)
SIP_ROUTE_DECISION_TIMEOUT = 1.5
MAX_TRUNK_INFO_DIGITS = 16


@dataclass(frozen=True, slots=True)
class TrunkInboundRuntime:
    """Explicit dependencies required to route one pre-answered trunk call."""

    hass: HomeAssistant
    config: dict
    local_ip: str
    ha_peer_name: str
    route_resolver: EndpointRouteResolver
    forward_existing_call: Callable[..., Awaitable[None]]
    defer_invite_to_softphone: Callable[..., None]
    start_local_assist_bridge: Callable[..., Awaitable[Any]]


async def async_route_trunk_invite(
    runtime: TrunkInboundRuntime,
    invite,
    *,
    bridge_ports: RtpPortReservation,
) -> None:
    """Select and establish the destination leg for one inbound trunk call."""

    hass = runtime.hass
    cfg = runtime.config
    source_relay_port, dest_relay_port = bridge_ports.ports
    artifacts = call_runtime_artifacts(hass).artifacts_for(invite.call_id)
    if artifacts is None:
        bridge_ports.release()
        return
    registry = call_registry(hass)
    session = registry.get_session(invite.call_id)
    if session is None:
        bridge_ports.release()
        return

    async def terminate_source(reason: str, *, status: int, answered: bool) -> None:
        intent = (
            TerminationIntent.bye(
                reason,
                TerminationInitiator.ROUTING,
                response_status=status,
            )
            if answered
            else TerminationIntent.final_response(reason, status)
        )
        await EndpointTerminationHandler(hass).terminate(invite.call_id, intent)

    configured_trunk = trunk_config(hass)
    dtmf_timeout_ms = max(
        0, int(configured_trunk.get(CONF_TRUNK_DTMF_TIMEOUT_MS) or 0)
    )
    destination = ""
    digits = ""
    automation_decision: dict = {}
    peers = await async_build_peer_snapshot(hass)
    roster_entries = roster_from_peers(
        hass, peers, registered_roster_entries(hass)
    )
    routes = dtmf_extension_routes(roster_entries)
    if configured_trunk.get(CONF_TRUNK_DTMF_ENABLED) and dtmf_timeout_ms > 0:
        timeout = float(dtmf_timeout_ms) / 1000.0
        terminator = str(configured_trunk.get(CONF_TRUNK_DTMF_TERMINATOR) or "")
        info_queue = artifacts.trunk_info_queue
        if info_queue is None:
            info_queue = asyncio.Queue(maxsize=MAX_TRUNK_INFO_DIGITS)
            artifacts.trunk_info_queue = info_queue
        selection = await collect_trunk_dtmf(
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
    if artifacts.trunk_closed:
        _LOGGER.info(
            "SIP trunk inbound call_id=%s closed during DTMF collection",
            invite.call_id,
        )
        return

    # Explicit digits always select the canonical phonebook route. Only
    # the no-digits fallback may be overridden by an automation.
    if not digits and configured_trunk.get(CONF_AUTOMATION_ROUTING_ENABLED):
        automation_decision = await async_request_inbound_destination(
            hass,
            invite,
            registry=registry,
            session=session,
            trunk_config=configured_trunk,
            timeout=SIP_ROUTE_DECISION_TIMEOUT,
        )
    artifacts.trunk_info_queue = None

    if artifacts.trunk_closed:
        _LOGGER.info(
            "SIP trunk inbound call_id=%s closed before routing", invite.call_id
        )
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
            str(
                configured_trunk.get(CONF_TRUNK_INBOUND_DEFAULT_TARGET) or "HA"
            ).strip()
            or "HA",
        )
        # The caller is still in the initial, pre-answered routing phase. Feed
        # the selected destination into the canonical dispatcher below rather
        # than invoking the in-call forwarding primitive, which requires an
        # already assigned Home Assistant phone owner.
        destination = automation_destination
        automation_route = runtime.route_resolver.route(destination, roster_entries)
        if automation_route.action is RouteAction.GROUP:
            await runtime.forward_existing_call(
                call_id=invite.call_id,
                destination=destination,
                on_failure="resume",
                initial_selection=True,
            )
            return
    if automation_action in {"decline", "busy", "cancel"}:
        preanswered = registry.resource_for(invite.call_id, "preanswered")
        status = 486 if automation_action == "busy" else 603
        reason = (
            TerminalReason.BUSY.value
            if automation_action == "busy"
            else TerminalReason.CANCELLED.value
            if automation_action == "cancel"
            else TerminalReason.DECLINED.value
        )
        answered = bool((preanswered or {}).get("final_response_sent", True))
        await terminate_source(reason, status=status, answered=answered)
        return

    default_target = trunk_default_target(configured_trunk)
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
        # ANSWER_HA identifies the endpoint kind, not necessarily the default
        # browser phone. Preserve explicit DTMF extensions for the second pass.
        destination = route_hint or decision.target or default_target
    elif decision.action is RouteAction.REJECT:
        preanswered = registry.resource_for(invite.call_id, "preanswered")
        terminal_reason = RouteReason.ROUTE_NOT_FOUND.value
        _LOGGER.info(
            "SIP trunk route not found call_id=%s digits=%s hint=%s",
            invite.call_id,
            digits or "-",
            route_hint or "-",
        )
        answered = bool((preanswered or {}).get("final_response_sent", True))
        await terminate_source(terminal_reason, status=404, answered=answered)
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
        registry.take_pending_invite(invite.call_id)
        preanswered = registry.take_media(invite.call_id, provisional=True)
        release_video_media_reservation(preanswered)
        try:
            await runtime.start_local_assist_bridge(
                invite,
                reservation=bridge_ports,
                local_rtp_port=source_relay_port,
                roster_entries=roster_entries,
                source="trunk",
                called_extension=digits or route_hint,
            )
            answer_result = await async_commit_runtime_answer(
                registry,
                invite.call_id,
                str((preanswered or {}).get("early_answer_sdp") or ""),
                send_final_response=send_final_response,
                response_context=hass,
                owner="assist",
                callee=destination,
                route_kind=decision.action.value,
                response_already_sent=bool(
                    (preanswered or {}).get("final_response_sent", True)
                ),
            )
            if not answer_result.committed:
                raise RuntimeError(answer_result.reason)
        except Exception:
            _LOGGER.exception(
                "SIP trunk Assist bridge failed call_id=%s", invite.call_id
            )
            answered = bool((preanswered or {}).get("final_response_sent", True))
            await terminate_source(
                TerminalReason.MEDIA_INCOMPATIBLE.value,
                status=488,
                answered=answered,
            )
        return

    if runtime.route_resolver.is_ha_target(destination):
        endpoint = preferred_browser_phone(hass)
        if endpoint is None:
            preanswered = registry.resource_for(invite.call_id, "preanswered")
            await terminate_source(
                TerminalReason.TRANSPORT_UNREACHABLE.value,
                status=480,
                answered=bool((preanswered or {}).get("final_response_sent", True)),
            )
            return
        runtime.defer_invite_to_softphone(
            invite,
            route_kind="trunk",
            endpoint_id=endpoint.endpoint_id,
            endpoint_device_id=endpoint.device_id,
            callee=endpoint.name,
            last_sip_event="DTMF_ROUTE",
        )
        return

    decision = runtime.route_resolver.route(
        destination,
        roster_from_peers(hass, peers, registered_roster_entries(hass)),
    )
    if decision.action is RouteAction.ANSWER_HA:
        roster = roster_from_peers(hass, peers, registered_roster_entries(hass))
        target_endpoint = runtime.route_resolver.logical_endpoint(
            decision.target or destination,
            peers,
            roster,
        ) or preferred_browser_phone(hass)
        if target_endpoint is not None and target_endpoint.kind is EndpointKind.BROWSER:
            runtime.defer_invite_to_softphone(
                invite,
                route_kind="trunk",
                endpoint_id=target_endpoint.endpoint_id,
                endpoint_device_id=(
                    target_endpoint.device_id
                ),
                callee=target_endpoint.name,
                last_sip_event="DTMF_ROUTE",
            )
            return
        await terminate_source(
            TerminalReason.TRANSPORT_UNREACHABLE.value,
            status=480,
            answered=True,
        )
        return
    preanswered = registry.resource_for(invite.call_id, "preanswered")
    peer_target = peer_for_target(decision.target or destination, peers)
    bridge_uri = None
    try:
        if peer_target is not None and peer_target.host:
            bridge_uri = sip_uri_for_peer(
                peer_target,
                default_port=int(cfg["sip_port"]),
                fallback_user=decision.target or destination,
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

    if (
        bridge_uri is None
        or runtime.route_resolver.is_local_listener_uri(bridge_uri)
    ):
        _LOGGER.info(
            "SIP trunk destination unresolved destination=%s route=%s",
            destination,
            decision.action.value,
        )
        release_video_media_reservation(preanswered)
        bridge_ports.release()
        await terminate_source(
            TerminalReason.TRANSPORT_UNREACHABLE.value,
            status=404,
            answered=True,
        )
        return

    peer_target = peer_for_target(destination, peers)
    remote_tx_formats = peer_audio_formats(
        peer_target, "tx_formats"
    ) or roster_entry_formats(decision.entry, "tx_formats")
    remote_rx_formats = peer_audio_formats(
        peer_target, "rx_formats"
    ) or roster_entry_formats(decision.entry, "rx_formats")
    sip_send_formats, sip_recv_formats = sip_target_audio_profile(
        remote_tx_formats=remote_tx_formats,
        remote_rx_formats=remote_rx_formats,
        target=destination,
    )
    source_video_reservation = (
        preanswered.get("video_rtp_reservation")
        if isinstance(preanswered, dict)
        else None
    )
    source_video_rtp_socket = (
        preanswered.get("video_rtp_socket")
        if isinstance(preanswered, dict)
        else None
    )
    source_video_rtcp_socket = (
        preanswered.get("video_rtcp_socket")
        if isinstance(preanswered, dict)
        else None
    )
    destination_video_reservation = None
    video_relay = None
    video_failure_reason = str(
        (preanswered or {}).get("video_failure_reason") or ""
    )
    if (
        cfg.get(CONF_SIP_VIDEO, False)
        and invite.video_format is not None
        and source_video_reservation is not None
        and source_video_rtp_socket is not None
        and source_video_rtcp_socket is not None
    ):
        try:
            (
                destination_video_reservation,
                destination_video_rtp_socket,
                destination_video_rtcp_socket,
            ) = reserve_sip_video_media(hass)

            def _release_video_ports(_ports) -> None:
                source_video_reservation.release()
                destination_video_reservation.release()

            video_relay = build_pending_invite_video_relay(
                invite,
                remote_host=str(bridge_uri.host),
                left_port=int((preanswered or {}).get("local_video_rtp_port") or 0),
                right_port=destination_video_reservation.ports[1],
                sockets=(
                    source_video_rtp_socket,
                    source_video_rtcp_socket,
                    destination_video_rtp_socket,
                    destination_video_rtcp_socket,
                ),
                on_release=_release_video_ports,
            )
        except (OSError, RuntimeError, ValueError) as err:
            for sock in (source_video_rtp_socket, source_video_rtcp_socket):
                sock.close()
            source_video_reservation.release()
            if destination_video_reservation is not None:
                destination_video_reservation.release()
            video_failure_reason = "local_video_resources_unavailable"
            _LOGGER.warning(
                "SIP trunk destination video unavailable; bridge remains audio-only: %s",
                err,
            )
    elif source_video_reservation is not None:
        for sock in (source_video_rtp_socket, source_video_rtcp_socket):
            if sock is not None:
                sock.close()
        source_video_reservation.release()
    client = SipCallClient(
        local_ip=runtime.local_ip,
        local_name=invite.caller or runtime.ha_peer_name,
        local_uri_user=invite.routing_caller or runtime.ha_peer_name,
        local_sip_port=int(cfg["sip_port"]),
        local_rtp_port=dest_relay_port,
        supported_send_formats=sip_send_formats,
        supported_recv_formats=sip_recv_formats,
        allow_directional_audio_payloads=supports_directional_audio_payloads(
            peer_target, decision.entry
        ),
        signaling_transport=uri_transport(bridge_uri),
        peer_user_agent=str(
            (
                (decision.entry.metadata or {}).get("user_agent")
                if decision.entry is not None
                else ""
            )
            or ""
        ),
        local_video_rtp_port=(
            destination_video_reservation.ports[1]
            if destination_video_reservation is not None
            else 0
        ),
        video_formats=(
            video_bridge_offer_formats(
                invite.video_format,
                source_receive=invite.recv_video_format,
                enable_transcoding=bool(
                    cfg.get(CONF_VIDEO_TRANSCODING, False)
                ),
                target_codec=peer_video_codec(peer_target, decision.entry),
            )
            if video_relay is not None and invite.video_format is not None
            else ()
        ),
        video_direction=(
            invite.video_format.direction if video_relay is not None else "inactive"
        ),
        generic_video_relay=video_relay is not None,
        allow_video_transcoding=bool(cfg.get(CONF_VIDEO_TRANSCODING, False)),
    )
    enable_reused_tcp_connection(
        hass,
        client,
        bridge_uri,
        target=destination,
        default_sip_port=int(cfg["sip_port"]),
    )
    result = await client.invite(
        target=bridge_uri.user,
        target_display_name=(
            decision.entry.display_name
            if decision.entry is not None
            else destination
        ),
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
        await async_close_client_and_release(client, bridge_ports)
        if video_relay is not None:
            await video_relay.stop()
        public_result = sip_public_state(result)
        terminal_reason = sip_terminal_reason(result, public_result)
        await terminate_source(
            terminal_reason,
            status=client.last_sip_status_code or 480,
            answered=True,
        )
        return
    _LOGGER.info(
        "SIP trunk bridge media call_id=%s trunk_tx=%s trunk_rx=%s "
        "destination_tx=%s destination_rx=%s",
        invite.call_id,
        invite.send_format.wire_token(),
        invite.recv_format.wire_token(),
        client.dialog.send_format.wire_token(),
        client.dialog.recv_format.wire_token(),
    )

    winner = OutboundLeg(
        member=destination,
        uri=bridge_uri,
        client=client,
        ports=bridge_ports,
        video_relay=video_relay,
        video_failure_reason=video_failure_reason,
    )
    reservations = tuple(
        item
        for item in (
            bridge_ports,
            source_video_reservation,
            destination_video_reservation,
        )
        if item is not None
    )
    try:
        committed = await async_commit_outbound_bridge(
            hass,
            registry,
            BridgeCommitData(
                invite=invite,
                winner=winner,
                source_relay_port=source_relay_port,
                dest_relay_port=dest_relay_port,
                local_ip=runtime.local_ip,
                release_port_pairs=tuple(item.ports for item in reservations),
                detach_reservations=reservations,
                enable_video_transcoding=bool(
                    cfg.get(CONF_VIDEO_TRANSCODING, False)
                ),
            ),
            BridgeCommitPolicy(
                route_kind="trunk",
                caller=invite.caller,
                callee=destination,
                connected_party=destination,
                source_role="trunk",
                source_state=CallState.IN_CALL.value,
                bridge_state=CallState.IN_CALL.value,
                response_already_sent=bool(
                    (preanswered or {}).get("final_response_sent", True)
                ),
                consume_pending_source=True,
            ),
        )
    except Exception as err:
        _LOGGER.warning("SIP trunk RTP bridge unavailable: %s", err)
        await terminate_source(
            TerminalReason.MEDIA_INCOMPATIBLE.value,
            status=488,
            answered=True,
        )
        return
    if committed is None:
        await terminate_source(
            TerminalReason.PROTOCOL_ERROR.value,
            status=500,
            answered=True,
        )
        return
    session = registry.get_session(invite.call_id)
    if session is None:
        raise RuntimeError("SIP trunk bridge session disappeared after commit")
    _LOGGER.info(
        "SIP bridge registered call_id=%s dest_call_id=%s target=%s",
        invite.call_id,
        client.dialog_ids.call_id,
        bridge_uri.user,
    )
    publish_bridge_projection(
        hass,
        session,
        peer_name=destination,
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
        video_active=committed.video_answer is not None,
        video_requested=invite.video_format is not None,
        video_failure_reason=committed.video_failure_reason,
        video_format=(
            committed.video_answer.video_format.wire_token()
            if committed.video_answer is not None
            else ""
        ),
        video_direction=(
            committed.video_answer.direction
            if committed.video_answer is not None
            else "inactive"
        ),
    )
