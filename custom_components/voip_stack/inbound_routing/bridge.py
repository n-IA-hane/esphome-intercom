"""Inbound SIP bridge routing and media lifecycle."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Protocol

from homeassistant.core import HomeAssistant

from ..bridge_manager import async_watch_sip_bridge_destination
from ..call_projection import publish_bridge_projection
from ..core.audio_format import HA_TRUNK_AUDIO_FORMATS
from ..config import trunk_identity_uri as _trunk_identity_uri
from ..const import (
    CONF_SIP_VIDEO,
    CONF_VIDEO_TRANSCODING,
    CONF_TRUNK_AUTH_USERNAME,
    CONF_TRUNK_OUTBOUND_PROXY,
    CONF_TRUNK_PASSWORD,
    CONF_TRUNK_PORT,
    CONF_TRUNK_SERVER,
    CONF_TRUNK_TRANSPORT,
    CONF_TRUNK_USERNAME,
)
from ..endpoint_registry import EndpointBusyError
from ..endpoint_termination import EndpointTerminationHandler
from ..endpoint_session import (
    SipTerminationDisposition,
    TerminationInitiator,
    TerminationIntent,
)
from ..endpoint_routing import (
    peer_audio_formats,
    peer_for_target,
    peer_video_codec,
    roster_entry_formats,
    sip_target_audio_profile,
    supports_directional_audio_payloads,
)
from ..fsm import (
    CallState,
    TerminalReason,
    sip_failure_response,
)
from ..media_ports import (
    RtpPortReservation,
    release_sip_rtp_port_pair,
    reserve_sip_video_relay_media,
    take_delayed_offer_ports,
)
from ..outbound_attempts import OutboundLeg, async_close_client_and_release
from ..outbound_bridge_commit import (
    BridgeCommitData,
    BridgeCommitPolicy,
    async_commit_outbound_bridge,
)
from ..phone_endpoint import EndpointKind
from ..peer import sip_uri_for_peer
from ..runtime_data import call_runtime_artifacts
from ..core.sip import parse_sip_uri, sip_endpoints_equal, sip_uri_targets_listener
from ..sip_bridge import (
    build_pending_invite_video_relay,
    video_bridge_offer_formats,
)
from ..sip_client import SIP_TIMER_B, SipCallClient
from ..sip_listener import SipInviteResult

if TYPE_CHECKING:
    from ..pbx_runtime import SipEndpointRuntime
    from ..phone_endpoint import PhoneEndpoint
    from ..router import RouteDecision
    from ..sip_listener import SipInvite

_LOGGER = logging.getLogger(__name__)


class BridgeRuntime(Protocol):
    """Dependencies used by the inbound bridge path."""

    hass: HomeAssistant
    config: dict[str, Any]
    local_ip: str
    ha_peer_name: Callable[..., str]
    enable_reused_sip_tcp_connection: Callable[..., Any]
    send_final_response: Callable[..., Any]
    sip_uri_transport: Callable[..., Any]
    terminate_sip_bridge: Callable[..., Any]


def _resolve_bridge_uri(
    *,
    decision: RouteDecision,
    invite: SipInvite,
    peers: list[Any],
    bridge_to_trunk: bool,
    trunk_config: dict[str, Any],
    default_sip_port: int,
):
    peer_target = peer_for_target(decision.target or invite.routing_target, peers)
    bridge_uri = None
    if bridge_to_trunk:
        bridge_uri = parse_sip_uri(
            f"sip:{decision.target or invite.routing_target}@"
            f"{trunk_config[CONF_TRUNK_SERVER]}:"
            f"{int(trunk_config[CONF_TRUNK_PORT])};"
            f"transport={str(trunk_config[CONF_TRUNK_TRANSPORT]).lower()}"
        )
    elif peer_target is not None and peer_target.host:
        bridge_uri = sip_uri_for_peer(
            peer_target,
            default_port=default_sip_port,
            fallback_user=decision.target or invite.routing_target,
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
            or default_sip_port
        )
        bridge_uri = parse_sip_uri(
            f"sip:{decision.entry.id}@{decision.entry.address}:{bridge_port}"
        )
    return (
        bridge_uri or (parse_sip_uri(decision.sip_uri) if decision.sip_uri else None),
        peer_target,
    )


async def route_sip_bridge(
    *,
    runtime: BridgeRuntime,
    invite: SipInvite,
    decision: RouteDecision,
    peers: list[Any],
    trunk_config: dict[str, Any],
    bridge_to_trunk: bool,
    source_endpoint: PhoneEndpoint | None,
    target_endpoint: PhoneEndpoint | None,
    resolved_callee: str,
    trunk_invite: bool,
    registry: SipEndpointRuntime,
) -> SipInviteResult | None:
    """Route one inbound dialog to a remote SIP endpoint."""

    hass = runtime.hass
    cfg = runtime.config
    local_ip = runtime.local_ip
    decision_uri, peer_target = _resolve_bridge_uri(
        decision=decision,
        invite=invite,
        peers=peers,
        bridge_to_trunk=bridge_to_trunk,
        trunk_config=trunk_config,
        default_sip_port=int(cfg["sip_port"]),
    )
    if peer_target is not None and sip_endpoints_equal(
        peer_target.host,
        peer_target.sip_port,
        invite.source_host,
        invite.source_port,
        default_port=int(cfg["sip_port"]),
    ):
        return SipInviteResult(
            486,
            "Busy Here",
            to_tag="",
            decline_reason=TerminalReason.BUSY.value,
        )

    points_to_local_listener = sip_uri_targets_listener(
        decision_uri,
        listener_hosts=(local_ip, "127.0.0.1", "localhost", "::1"),
        listener_port=int(cfg["sip_port"]),
        default_port=int(cfg["sip_port"]),
    )
    if decision_uri is None or points_to_local_listener:
        return None

    try:
        bridge_ports = take_delayed_offer_ports(
            registry, invite.call_id
        ) or RtpPortReservation.allocate(hass)
    except RuntimeError as err:
        _LOGGER.warning("SIP RTP bridge port allocation failed: %s", err)
        return SipInviteResult(503, "Service Unavailable", to_tag="")
    source_relay_port, dest_relay_port = bridge_ports.ports
    remote_tx_formats = peer_audio_formats(
        peer_target, "tx_formats"
    ) or roster_entry_formats(decision.entry, "tx_formats")
    remote_rx_formats = peer_audio_formats(
        peer_target, "rx_formats"
    ) or roster_entry_formats(decision.entry, "rx_formats")
    sip_send_formats, sip_recv_formats = sip_target_audio_profile(
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
    video_transcoding_enabled = bool(
        runtime.config.get(CONF_VIDEO_TRANSCODING, False)
    )
    target_video_codec = peer_video_codec(peer_target, decision.entry)
    if (
        bool(cfg.get(CONF_SIP_VIDEO, False))
        and invite.video_format is not None
        and target_video_codec != ""
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
                on_release=lambda ports: release_sip_rtp_port_pair(hass, ports),
            )
        except (OSError, RuntimeError) as err:
            for sock in sockets:
                sock.close()
            if video_bridge_ports is not None:
                video_bridge_ports.release()
            video_bridge_ports = None
            video_relay = None
            video_failure_reason = "local_video_resources_unavailable"
            _LOGGER.warning(
                "SIP video relay ports unavailable; bridge remains audio-only: %s",
                err,
            )

    client = SipCallClient(
        local_ip=local_ip,
        local_name=invite.caller or runtime.ha_peer_name(hass),
        local_uri_user=(
            str(trunk_config.get(CONF_TRUNK_USERNAME) or runtime.ha_peer_name(hass))
            if bridge_to_trunk
            else invite.routing_caller or runtime.ha_peer_name(hass)
        ),
        local_identity_uri=(
            _trunk_identity_uri(trunk_config) if bridge_to_trunk else ""
        ),
        local_sip_port=int(cfg["sip_port"]),
        local_rtp_port=dest_relay_port,
        supported_send_formats=sip_send_formats,
        supported_recv_formats=sip_recv_formats,
        signaling_transport=runtime.sip_uri_transport(decision_uri),
        auth_username=str(trunk_config.get(CONF_TRUNK_AUTH_USERNAME) or "")
        if bridge_to_trunk
        else "",
        username=str(trunk_config.get(CONF_TRUNK_USERNAME) or "")
        if bridge_to_trunk
        else "",
        password=str(trunk_config.get(CONF_TRUNK_PASSWORD) or "")
        if bridge_to_trunk
        else "",
        outbound_proxy=str(trunk_config.get(CONF_TRUNK_OUTBOUND_PROXY) or "")
        if bridge_to_trunk
        else "",
        include_common_codecs=bridge_to_trunk or bridge_to_softphone,
        allow_directional_audio_payloads=supports_directional_audio_payloads(
            peer_target, decision.entry
        ),
        peer_user_agent=(
            str((decision.entry.metadata or {}).get("user_agent") or "")
            if bridge_to_softphone and decision.entry is not None
            else ""
        ),
        local_video_rtp_port=(video_bridge_ports.ports[1] if video_bridge_ports else 0),
        video_formats=(
            video_bridge_offer_formats(
                invite.video_format,
                source_receive=invite.recv_video_format,
                enable_transcoding=video_transcoding_enabled,
                target_codec=target_video_codec,
            )
            if video_bridge_ports and invite.video_format is not None
            else ()
        ),
        video_direction=(
            invite.video_format.direction if video_bridge_ports else "inactive"
        ),
        generic_video_relay=bool(video_bridge_ports),
        allow_video_transcoding=video_transcoding_enabled,
    )
    if not bridge_to_trunk:
        runtime.enable_reused_sip_tcp_connection(
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
            await async_close_client_and_release(client, bridge_ports)
            if video_relay is not None:
                await video_relay.stop()
            await EndpointTerminationHandler(hass).terminate_reason(
                invite.call_id,
                TerminalReason.BUSY.value,
                TerminationInitiator.ROUTING,
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
            target_display_name=resolved_callee,
            remote_host=decision_uri.host,
            remote_sip_port=decision_uri.port or int(cfg["sip_port"]),
            request_uri=str(decision_uri),
            timeout=SIP_TIMER_B if bridge_to_trunk else 8.0,
        )
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning(
            "SIP bridge INVITE failed call_id=%s target=%s: %s",
            invite.call_id,
            decision_uri.user,
            err,
        )
        await async_close_client_and_release(client, bridge_ports)
        if video_relay is not None:
            await video_relay.stop()
        await EndpointTerminationHandler(hass).terminate_reason(
            invite.call_id,
            TerminalReason.TRANSPORT_UNREACHABLE.value,
            TerminationInitiator.ROUTING,
        )
        return SipInviteResult(
            503,
            "Service Unavailable",
            to_tag="",
            decline_reason=TerminalReason.TRANSPORT_UNREACHABLE.value,
        )

    artifacts = call_runtime_artifacts(hass).artifacts_for(invite.call_id)
    if artifacts is not None and artifacts.trunk_closed:
        artifacts.trunk_closed = False
        _LOGGER.info(
            "SIP bridge invite completed after caller cancelled call_id=%s; closing outbound leg",
            invite.call_id,
        )
        await async_close_client_and_release(client, bridge_ports, bye=True)
        if video_relay is not None:
            await video_relay.stop()
        await EndpointTerminationHandler(hass).terminate_reason(
            invite.call_id,
            TerminalReason.CANCELLED.value,
            TerminationInitiator.ROUTING,
        )
        return SipInviteResult(
            487,
            "Request Terminated",
            to_tag="",
            decline_reason=TerminalReason.CANCELLED.value,
        )
    if result not in {"ringing", "in_call"}:
        status_code, sip_reason, terminal_reason, public_state = sip_failure_response(
            result
        )
        await async_close_client_and_release(client, bridge_ports)
        if video_relay is not None:
            await video_relay.stop()
        await EndpointTerminationHandler(hass).terminate(
            invite.call_id,
            TerminationIntent(
                terminal_reason,
                sip_disposition=SipTerminationDisposition.NONE,
                response_status=status_code,
            ),
        )
        return SipInviteResult(
            status_code,
            sip_reason,
            to_tag="",
            decline_reason=terminal_reason,
        )

    async def finish_bridge(initial_result: str) -> None:
        nonlocal video_failure_reason, video_relay
        final = initial_result
        if final == "ringing":
            final = await client.wait_for_final()
        if final != "in_call" or client.dialog is None:
            status_code, sip_reason, terminal_reason, public_state = (
                sip_failure_response(final)
            )
            await registry.close_leg(
                invite.call_id,
                client.dialog_ids.call_id,
                reason=terminal_reason,
            )
            bridge_ports.release()
            if video_relay is not None:
                await video_relay.stop()
            await EndpointTerminationHandler(hass).terminate(
                invite.call_id,
                TerminationIntent.final_response(
                    terminal_reason, status_code
                ),
            )
            return

        session = registry.get_session(invite.call_id)
        if session is None:
            bridge_ports.release()
            if video_relay is not None:
                await video_relay.stop()
            return
        winner = OutboundLeg(
            member=resolved_callee,
            uri=decision_uri,
            client=client,
            ports=bridge_ports,
            endpoint_id=(
                logical_target_endpoint.endpoint_id
                if logical_target_endpoint is not None
                else ""
            ),
            video_relay=video_relay,
            video_failure_reason=video_failure_reason,
        )
        reservations = tuple(
            item for item in (bridge_ports, video_bridge_ports) if item is not None
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
                    local_ip=local_ip,
                    release_port_pairs=(bridge_ports.ports,),
                    detach_reservations=reservations,
                    enable_video_transcoding=video_transcoding_enabled,
                ),
                BridgeCommitPolicy(
                    route_kind=decision.action.value,
                    caller=invite.caller,
                    callee=resolved_callee,
                    connected_party=resolved_callee,
                    ingress="trunk" if trunk_invite else "extension",
                    origin="trunk" if trunk_invite else "extension",
                    expected_generation=session.generation,
                    response_already_sent=invite.initial_response_sent,
                    source_endpoint_id=(
                        logical_source_endpoint.endpoint_id
                        if logical_source_endpoint is not None
                        else ""
                    ),
                    dest_endpoint_id=winner.endpoint_id,
                ),
                session=session,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("SIP RTP bridge media conversion unavailable: %s", err)
            return
        if committed is None:
            return
        relay = committed.relay
        selected_video = (
            committed.video_answer.video_format
            if committed.video_answer is not None
            else None
        )
        video_failure_reason = committed.video_failure_reason
        publish_bridge_projection(
            hass,
            session,
            peer_name=resolved_callee,
            direction="incoming",
            selected_tx_format=invite.send_format.audio_format.wire_token(),
            selected_rx_format=invite.recv_format.audio_format.wire_token(),
            selected_tx_rtp_format=invite.send_format.wire_token(),
            selected_rx_rtp_format=invite.recv_format.wire_token(),
            sip_status_code=200,
            last_sip_event="SIP_RESPONSE",
            route_kind=decision.action.value,
            sip_uri=str(decision_uri),
            video_active=bool(relay.video_relay is not None),
            video_requested=bool(invite.video_format is not None),
            video_negotiated=bool(relay.video_relay is not None),
            video_status=(
                "degraded"
                if video_failure_reason == "local_video_resources_unavailable"
                else "rejected"
                if video_failure_reason
                else "active"
                if relay.video_relay is not None
                else "inactive"
            ),
            video_failure_reason=video_failure_reason,
            video_format=selected_video.wire_token() if selected_video else "",
        )
        await async_watch_sip_bridge_destination(
            hass,
            client=client,
            source_call_id=invite.call_id,
            terminate_sip_bridge=runtime.terminate_sip_bridge,
        )

    finish_task = hass.async_create_task(finish_bridge(result))
    session = registry.register_bridge(
        source_call_id=invite.call_id,
        dest_call_id=client.dialog_ids.call_id,
        client=client,
        lifecycle_task=finish_task,
        state=CallState.CONNECTING.value,
        caller=invite.caller,
        callee=resolved_callee,
        route_kind=decision.action.value,
        ingress="trunk" if trunk_invite else "extension",
        origin="trunk" if trunk_invite else "extension",
        source_state=CallState.CONNECTING.value,
        dest_state=result,
    )
    if session is None:
        return SipInviteResult(487, "Request Terminated", to_tag="")
    _LOGGER.info(
        "SIP bridge registered call_id=%s dest_call_id=%s target=%s",
        invite.call_id,
        client.dialog_ids.call_id,
        decision_uri.user,
    )
    if result == "ringing":
        session = registry.transition(
            invite.call_id,
            state=CallState.REMOTE_RINGING.value,
            expected_generation=session.generation,
        )
        if session is not None:
            publish_bridge_projection(
                hass,
                session,
                peer_name=resolved_callee,
                direction="incoming",
                route_kind=decision.action.value,
                sip_uri=str(decision_uri),
                sip_status_code=180,
                last_sip_event="SIP_RESPONSE",
            )
    return SipInviteResult(180, "Ringing", to_tag="", defer_final=True)
