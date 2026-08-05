"""Canonical in-dialog DTMF event projection and bridge wiring."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from .automation_routing import CALL_EVENT_SCHEMA_VERSION, canonical_call_origin
from .dtmf import parse_sip_info_digit
from .endpoint_lifecycle import call_registry
from .fsm import CallState
from .runtime_data import call_runtime_artifacts
from .websocket_api import SIP_DTMF_EVENT


_LOGGER = logging.getLogger(__name__)


async def handle_sip_info(
    hass: HomeAssistant,
    request,
    _addr,
    transport,
) -> None:
    """Route one in-dialog SIP INFO digit to its active media owner."""

    digit = parse_sip_info_digit(request.header("Content-Type"), request.body)
    if not digit:
        _LOGGER.info(
            "SIP INFO ignored call_id=%s content_type=%s",
            request.header("Call-ID"),
            request.header("Content-Type") or "-",
        )
        return
    call_id = request.header("Call-ID")
    queue = call_runtime_artifacts(hass).trunk_info_queues.get(call_id)
    if queue is None:
        registry = call_registry(hass)
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
            publish_dtmf_event(
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
            "SIP INFO DTMF queue full call_id=%s; digit ignored",
            call_id,
        )
        return
    queue.put_nowait(digit)
    _LOGGER.info(
        "SIP trunk INFO DTMF RX call_id=%s digit=%s transport=%s",
        call_id,
        digit,
        transport,
    )


def publish_dtmf_event(
    hass: HomeAssistant,
    *,
    call_id: str,
    dest_call_id: str,
    caller: str,
    callee: str,
    side: str,
    digit: str,
    transport: str,
) -> None:
    """Publish one canonical in-dialog DTMF occurrence."""

    source_is_caller = side == "left"
    registry = call_registry(hass)
    event_context = registry.event_context(call_id)
    state = (
        event_context.state if event_context is not None else CallState.IN_CALL.value
    )
    session = registry.sessions.get(registry.resolve_session_id(call_id))
    session_metadata = session.metadata if session is not None else {}
    call_origin = canonical_call_origin(
        session_metadata.get("ingress") or session_metadata.get("origin"),
        session.route_kind if session is not None else "",
    )
    payload = {
        "schema_version": CALL_EVENT_SCHEMA_VERSION,
        "event_type": "dtmf",
        "state": state,
        "sip_state": state,
        "call_id": call_id,
        "dest_call_id": dest_call_id,
        "caller": caller,
        "callee": callee,
        "source": caller if source_is_caller else callee,
        "source_leg": "caller" if source_is_caller else "callee",
        "side": side,
        "digit": digit,
        "transport": transport,
        "direction": str(session_metadata.get("direction") or "incoming"),
        "scope": "sip_bridge",
        "actor": "sip_bridge",
        "ingress": call_origin,
        "origin": call_origin,
        "route_kind": session.route_kind if session is not None else "",
        "automation_control": "ha_anchored",
    }
    payload.update(registry.event_fields(call_id, state))
    context = registry.ha_context(call_id)
    if context is None:
        hass.bus.async_fire(SIP_DTMF_EVENT, payload)
    else:
        hass.bus.async_fire(SIP_DTMF_EVENT, payload, context=context)


def attach_dtmf_event_bridge(
    hass: HomeAssistant,
    relay,
    *,
    call_id: str,
    dest_call_id: str,
    caller: str,
    callee: str,
    client=None,
) -> None:
    """Publish negotiated DTMF and translate legacy SIP INFO to RFC 4733."""

    def _emit(side: str, digit: str, transport: str) -> None:
        publish_dtmf_event(
            hass,
            call_id=call_id,
            dest_call_id=dest_call_id,
            caller=caller,
            callee=callee,
            side=side,
            digit=digit,
            transport=transport,
        )
        if transport == "sip_info":
            # SIP INFO is accepted as an ingress compatibility format. The
            # opposite leg still receives the negotiated RFC 4733 RTP event.
            relay.relay_dtmf(side, digit)

    relay.on_dtmf = _emit
    if client is not None:
        client.on_info_dtmf = lambda digit: _emit("right", digit, "sip_info")


def attach_direct_client_dtmf_events(
    hass: HomeAssistant,
    client,
    *,
    call_id: str,
    caller: str,
    callee: str,
) -> None:
    """Project SIP INFO received by a direct outbound softphone dialog."""

    client.on_info_dtmf = lambda digit: publish_dtmf_event(
        hass,
        call_id=call_id,
        dest_call_id="",
        caller=caller,
        callee=callee,
        side="right",
        digit=digit,
        transport="sip_info",
    )
