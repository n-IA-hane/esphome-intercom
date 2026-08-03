"""Inbound trunk call orchestration outside the endpoint listener closure."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Any, Awaitable, Callable

from homeassistant.core import HomeAssistant

from .bridge_manager import async_watch_sip_bridge_destination
from .config import debug_mode, trunk_config
from .const import (
    CONF_AUTOMATION_ROUTING_ENABLED,
    CONF_TRUNK_DTMF_ENABLED,
    CONF_TRUNK_DTMF_TERMINATOR,
    CONF_TRUNK_DTMF_TIMEOUT_MS,
    CONF_TRUNK_INBOUND_DEFAULT_TARGET,
    DOMAIN,
    HA_SOFTPHONE_DEVICE_ID,
)
from .endpoint_lifecycle import call_registry
from .endpoint_routing import (
    EndpointRouteResolver,
    peer_audio_formats,
    peer_for_target,
    roster_entry_formats,
    roster_from_peers,
    sip_target_audio_profile,
)
from .fsm import (
    CallState,
    TerminalReason,
    sip_public_state,
    sip_terminal_reason,
)
from .media_ports import (
    RtpPortReservation,
    release_media_reservation,
    release_sip_rtp_port_pair,
    release_video_media_reservation,
)
from .outbound_attempts import async_close_client_and_release
from .pbx_routing import dtmf_extension_routes
from .peer_snapshot import async_build_peer_snapshot
from .phone_endpoint import DEFAULT_ENDPOINT_ID, EndpointKind
from .phonebook_runtime import registered_roster_entries
from .router import CallContext, RouteAction, RouteReason, route_inbound_trunk
from .sip import parse_sip_uri
from .sip_bridge import build_invite_client_relay
from .sip_client import SipCallClient
from .sip_runtime import (
    enable_reused_tcp_connection,
    send_bye,
    send_final_response,
    uri_transport,
)
from .trunk_dtmf import collect_trunk_dtmf
from .trunk_routing import async_request_inbound_destination, trunk_default_target
from .websocket_api import _set_sip_bridge_call_state

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
    attach_client_media_update: Callable[..., None]
    attach_dtmf_event_bridge: Callable[..., None]
    terminate_sip_bridge: Callable[..., Awaitable[Any]]


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
    bucket = hass.data.setdefault(DOMAIN, {})
    registry = call_registry(hass)
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
        info_queue = bucket.setdefault("trunk_info_queues", {}).setdefault(
            invite.call_id,
            asyncio.Queue(maxsize=MAX_TRUNK_INFO_DIGITS),
        )
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
    if not digits and configured_trunk.get(CONF_AUTOMATION_ROUTING_ENABLED):
        automation_decision = await async_request_inbound_destination(
            hass,
            invite,
            trunk_config=configured_trunk,
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
            str(
                configured_trunk.get(CONF_TRUNK_INBOUND_DEFAULT_TARGET) or "HA"
            ).strip()
            or "HA",
        )
        await runtime.forward_existing_call(
            call_id=invite.call_id,
            destination=automation_destination,
            on_failure="resume",
            initial_selection=True,
        )
        return
    if automation_action in {"decline", "busy", "cancel"}:
        registry.pending_invites.pop(invite.call_id, None)
        preanswered = registry.take_media(invite.call_id, provisional=True)
        release_media_reservation(preanswered)
        status = 486 if automation_action == "busy" else 603
        reason = (
            TerminalReason.BUSY.value
            if automation_action == "busy"
            else TerminalReason.CANCELLED.value
            if automation_action == "cancel"
            else TerminalReason.DECLINED.value
        )
        if bool((preanswered or {}).get("final_response_sent", True)):
            send_bye(hass, invite.call_id)
        else:
            send_final_response(
                hass,
                invite.call_id,
                status,
                "Busy Here" if status == 486 else "Decline",
            )
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
        registry.pending_invites.pop(invite.call_id, None)
        preanswered = registry.take_media(invite.call_id, provisional=True)
        release_media_reservation(preanswered)
        terminal_reason = RouteReason.ROUTE_NOT_FOUND.value
        _LOGGER.info(
            "SIP trunk route not found call_id=%s digits=%s hint=%s",
            invite.call_id,
            digits or "-",
            route_hint or "-",
        )
        if bool((preanswered or {}).get("final_response_sent", True)):
            send_bye(hass, invite.call_id)
        else:
            send_final_response(hass, invite.call_id, 404, "Not Found")
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
        registry.pending_invites.pop(invite.call_id, None)
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
            if not bool((preanswered or {}).get("final_response_sent", True)):
                send_final_response(
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
                send_bye(hass, invite.call_id)
            else:
                send_final_response(
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

    if runtime.route_resolver.is_ha_target(destination):
        runtime.defer_invite_to_softphone(
            invite,
            route_kind="trunk",
            endpoint_id=DEFAULT_ENDPOINT_ID,
            endpoint_device_id=HA_SOFTPHONE_DEVICE_ID,
            callee=runtime.ha_peer_name,
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
            runtime.defer_invite_to_softphone(
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
        await runtime.forward_existing_call(
            call_id=invite.call_id,
            destination=destination,
            on_failure="resume",
        )
        return
    registry.pending_invites.pop(invite.call_id, None)
    preanswered = registry.take_media(invite.call_id, provisional=True)
    release_video_media_reservation(preanswered)
    peer_target = peer_for_target(decision.target or destination, peers)
    bridge_uri = None
    try:
        if peer_target is not None and peer_target.host:
            sip_transport = str(
                (peer_target.device or {}).get("sip_transport") or "tcp"
            ).lower()
            if sip_transport not in {"tcp", "udp"}:
                sip_transport = "tcp"
            bridge_uri = parse_sip_uri(
                f"sip:{decision.target or destination}@{peer_target.host}:"
                f"{peer_target.sip_port or cfg['sip_port']};"
                f"transport={sip_transport}"
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
        send_bye(hass, invite.call_id)
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
    client = SipCallClient(
        local_ip=runtime.local_ip,
        local_name=invite.caller or runtime.ha_peer_name,
        local_uri_user=invite.routing_caller or runtime.ha_peer_name,
        local_sip_port=int(cfg["sip_port"]),
        local_rtp_port=dest_relay_port,
        supported_send_formats=sip_send_formats,
        supported_recv_formats=sip_recv_formats,
        signaling_transport=uri_transport(bridge_uri),
        peer_user_agent=str(
            (
                (decision.entry.metadata or {}).get("user_agent")
                if decision.entry is not None
                else ""
            )
            or ""
        ),
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
        send_bye(hass, invite.call_id)
        public_result = sip_public_state(result)
        terminal_reason = sip_terminal_reason(result, public_result)
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
        "SIP trunk bridge media call_id=%s trunk_tx=%s trunk_rx=%s "
        "destination_tx=%s destination_rx=%s",
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
            debug_capture=debug_mode(hass),
            on_release=lambda ports: release_sip_rtp_port_pair(hass, ports),
        )
        runtime.attach_dtmf_event_bridge(
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
        await async_close_client_and_release(
            client, bridge_ports, bye=True
        )
        send_bye(hass, invite.call_id)
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

    finish_task = hass.async_create_task(
        async_watch_sip_bridge_destination(
            hass,
            client=client,
            source_call_id=invite.call_id,
            terminate_sip_bridge=runtime.terminate_sip_bridge,
        )
    )
    registry.register_bridge(
        source_call_id=invite.call_id,
        dest_call_id=client.dialog_ids.call_id,
        client=client,
        lifecycle_task=finish_task,
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
    runtime.attach_client_media_update(
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
