"""Canonical in-dialog DTMF event projection and bridge wiring."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from .automation_routing import CALL_EVENT_SCHEMA_VERSION, canonical_call_origin
from .dtmf import parse_sip_info_digit, parse_sip_info_duration
from .core.sip import is_picture_fast_update
from .endpoint_lifecycle import call_registry
from .fsm import CallState
from .runtime_data import sip_endpoint_manager
from .const import SIP_DTMF_EVENT


_LOGGER = logging.getLogger(__name__)


async def _request_video_refresh(hass, call_id):
    """Use the existing opposite media/signaling owner for one refresh request."""
    registry = call_registry(hass)
    session_id = registry.resolve_session_id(call_id)
    side = "right" if session_id != call_id else "left"
    relay = registry.resource_for(session_id, "relay")
    video = getattr(relay, "video_relay", None)
    if video is None:
        return
    destination = "left" if side == "right" else "right"
    if video is not None and video.request_keyframe(destination):
        return
    target = session_id if destination == "left" else registry.bridge_link_for(session_id)
    client = registry.sip_client_for(target)
    if client is not None:
        await client.request_video_keyframe()
    else:
        manager = sip_endpoint_manager(hass)
        if manager is not None:
            await manager.async_request_video_keyframe(target)


async def handle_sip_info(
    hass: HomeAssistant,
    request,
    _addr,
    transport,
) -> None:
    """Route one in-dialog SIP INFO digit to its active media owner."""

    if is_picture_fast_update(request.header("Content-Type"), request.body):
        await _request_video_refresh(hass, request.header("Call-ID"))
        return

    digit = parse_sip_info_digit(request.header("Content-Type"), request.body)
    if not digit:
        _LOGGER.info(
            "SIP INFO ignored call_id=%s content_type=%s",
            request.header("Call-ID"),
            request.header("Content-Type") or "-",
        )
        return
    call_id = request.header("Call-ID")
    registry = call_registry(hass)
    session_id = registry.resolve_session_id(call_id)
    side = "right" if session_id != call_id else "left"
    relay = registry.resource_for(session_id, "relay")
    callback = getattr(relay, "on_dtmf", None)
    if callback is not None:
        if getattr(relay, "private_dtmf", False):
            callback(side, digit, "sip_info")
            return
        event = getattr(relay, "on_info_dtmf", None)
        if event is not None:
            event(side, digit, parse_sip_info_duration(request.body))
        else:
            callback(side, digit, "sip_info")
        _LOGGER.info(
            "SIP in-call INFO DTMF RX call_id=%s digit=%s transport=%s",
            call_id,
            digit,
            transport,
        )
        return
    if relay is not None or registry.resource_for(session_id, "softphone_media"):
        session = registry.sessions.get(registry.resolve_session_id(call_id))
        publish_dtmf_event(
            hass,
            call_id=session_id,
            dest_call_id=registry.bridge_link_for(session_id),
            caller=session.caller if session is not None else "",
            callee=session.callee if session is not None else "",
            side=side,
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
    from .trunk_events import add_trunk_names

    add_trunk_names(hass, payload)
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
    source_client=None,
) -> None:
    """Publish negotiated DTMF and translate legacy SIP INFO to RFC 4733."""

    def _emit(side: str, digit: str, transport: str, *, duration_ms=160) -> None:
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
            relay.relay_dtmf(side, digit, duration_ms=duration_ms)

    relay.on_dtmf = _emit
    relay.on_info_dtmf = lambda side, digit, duration_ms: _emit(
        side, digit, "sip_info", duration_ms=duration_ms)

    async def _fallback(source_side: str, digit: str, duration_ms: int) -> bool:
        if source_side == "left" and client is not None:
            return bool(await client.send_dtmf_info(digit, duration_ms=duration_ms))
        if source_side == "left" and dest_call_id:
            endpoint = sip_endpoint_manager(hass)
            return bool(endpoint is not None and await endpoint.async_send_dtmf_info(dest_call_id, digit, duration_ms=duration_ms))
        if source_side == "right":
            if source_client is not None:
                return bool(await source_client.send_dtmf_info(digit, duration_ms=duration_ms))
            endpoint = sip_endpoint_manager(hass)
            return bool(
                endpoint is not None
                and await endpoint.async_send_dtmf_info(call_id, digit, duration_ms=duration_ms)
            )
        return False

    relay.on_dtmf_fallback = _fallback
    if client is not None:
        client.on_info_video = lambda: _request_video_refresh(hass, dest_call_id)
        client.on_info_dtmf = lambda digit, duration_ms: _emit("right", digit, "sip_info", duration_ms=duration_ms)
    if source_client is not None:
        source_client.on_info_video = lambda: _request_video_refresh(hass, call_id)
        source_client.on_info_dtmf = lambda digit, duration_ms: _emit("left", digit, "sip_info", duration_ms=duration_ms)


def attach_direct_client_dtmf_events(
    hass: HomeAssistant,
    client,
    *,
    call_id: str,
    caller: str,
    callee: str,
) -> None:
    """Project SIP INFO received by a direct outbound softphone dialog."""

    client.on_info_dtmf = lambda digit, _duration_ms: publish_dtmf_event(
        hass,
        call_id=call_id,
        dest_call_id="",
        caller=caller,
        callee=callee,
        side="right",
        digit=digit,
        transport="sip_info",
    )
