"""Shared ring-group preflight and browser-leg settlement."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from .call_projection import observe_phone_leg_projection
from .dial_fork import DialDisposition
from .endpoint_lifecycle import call_registry
from .fsm import CallState
from .outbound_attempts import BrowserLeg
from .phone_endpoint import EndpointAvailability, EndpointKind


_LOGGER = logging.getLogger(__name__)


def endpoint_preflight_disposition(
    endpoint,
    *,
    call_id: str,
    browser: bool,
) -> DialDisposition | None:
    """Classify a logical endpoint before creating or claiming a dial leg."""

    if endpoint is None:
        return None
    if endpoint.dnd:
        return DialDisposition.DND
    if browser:
        if endpoint.availability is EndpointAvailability.UNAVAILABLE:
            return DialDisposition.UNAVAILABLE
    elif endpoint.availability is not EndpointAvailability.AVAILABLE:
        return DialDisposition.UNAVAILABLE
    if endpoint.active_call_id and endpoint.active_call_id != call_id:
        return DialDisposition.BUSY
    return None


def settle_browser_candidates(
    hass: HomeAssistant,
    registry,
    browser_legs: list[BrowserLeg],
    *,
    call_id: str,
    caller: str,
    callee: str,
    state: str,
    reason: str,
    route_kind: str,
    keep_endpoint_id: str = "",
) -> None:
    """Release and publish every browser candidate except one committed winner."""

    session = registry.get_session(call_id)
    for leg in browser_legs:
        if leg.endpoint_id == keep_endpoint_id:
            continue
        registry.release_endpoint_claim(call_id, leg.endpoint_id)
        try:
            leg_id = f"browser:{leg.endpoint_id}"
            if session is None:
                continue
            observe_phone_leg_projection(
                hass,
                registry,
                session,
                leg.endpoint_id,
                state,
                leg_id=leg_id,
                peer_name=caller,
                direction="incoming",
                reason=reason,
                terminal_reason=reason,
                route_kind=route_kind,
                last_sip_event="SIP_RESPONSE",
            )
        except Exception:  # noqa: BLE001 - observer failure must not leak claims.
            _LOGGER.exception(
                "SIP ring group candidate cleanup publication failed "
                "call_id=%s endpoint_id=%s",
                call_id,
                leg.endpoint_id,
            )


def publish_browser_candidates_ringing(
    hass: HomeAssistant,
    registry,
    browser_legs: list[BrowserLeg],
    *,
    invite,
    callee: str,
    route_kind: str,
    origin_endpoint_id: str,
    source_endpoint_id: str,
    origin_media_client_id: str,
) -> None:
    """Publish one ringing projection for every browser candidate."""

    if not browser_legs:
        return
    session = registry.upsert(
        invite.call_id,
        state=CallState.RINGING.value,
        owner="ha_softphone",
        caller=invite.caller,
        callee=callee,
        route_kind=route_kind,
        endpoint_id=origin_endpoint_id,
        source_endpoint_id=source_endpoint_id,
        ring_endpoint_ids=tuple(leg.endpoint_id for leg in browser_legs),
        media_client_id=origin_media_client_id,
    )
    for leg in browser_legs:
        observe_phone_leg_projection(
            hass,
            registry,
            session,
            leg.endpoint_id,
            CallState.RINGING.value,
            leg_id=f"browser:{leg.endpoint_id}",
            peer_name=invite.caller,
            direction="incoming",
            selected_tx_format=invite.send_format.audio_format.wire_token(),
            selected_rx_format=invite.recv_format.audio_format.wire_token(),
            selected_tx_rtp_format=invite.send_format.wire_token(),
            selected_rx_rtp_format=invite.recv_format.wire_token(),
            audio_mode="full_duplex",
            route_kind=route_kind,
            sip_status_code=180,
            last_sip_event="INVITE",
        )


def publish_ring_group_origin_state(
    hass: HomeAssistant,
    *,
    enabled: bool,
    state: str,
    endpoint_id: str,
    peer_name: str,
    call_id: str,
    reason: str,
    origin: str,
    route_kind: str,
    last_sip_event: str,
    sip_status_code: int | None = None,
) -> None:
    """Publish one outgoing state for the browser that started the group call."""

    if not enabled:
        return
    extra = {
        "reason": reason,
        "terminal_reason": reason,
        "origin": origin,
        "last_sip_event": last_sip_event,
        "route_kind": route_kind,
    }
    if sip_status_code is not None:
        extra["sip_status_code"] = int(sip_status_code)
    registry = call_registry(hass)
    session = registry.get_session(call_id)
    if session is None:
        return
    leg_id = f"browser-origin:{endpoint_id}"
    observe_phone_leg_projection(
        hass,
        registry,
        session,
        endpoint_id,
        state,
        leg_id=leg_id,
        peer_name=peer_name,
        direction="outgoing",
        **extra,
    )


def endpoint_is_esphome(endpoint) -> bool:
    """Return whether a claimed logical destination owns an ESP transport."""

    return endpoint is not None and endpoint.kind is EndpointKind.ESPHOME
