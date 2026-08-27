"""Apply browser camera intent to the currently owned call generation."""

from __future__ import annotations

import asyncio
import logging
import random

from homeassistant.core import HomeAssistant

from .config import transport_config
from .const import CONF_SIP_VIDEO, CONF_VIDEO_CAMERA_SEND
from .core import sdp
from .core.sdp import DEFAULT_VIDEO_FORMATS, browser_video_send_supported
from .endpoint_lifecycle import call_registry
from .media_ports import reserve_sip_video_media
from .runtime_data import sip_endpoint_manager
from .websocket_api import _fire_call_event, _ha_softphone_store


_LOGGER = logging.getLogger(__name__)
_ACTIVE_STATES = frozenset({"in_call"})


def _video_formats(current: object | None = None) -> tuple:
    configured = tuple(getattr(current, "video_formats", ()) or ())
    if configured:
        return configured
    return tuple(
        item for item in DEFAULT_VIDEO_FORMATS if browser_video_send_supported(item)
    )


def _publish_intent_result(
    hass: HomeAssistant,
    endpoint_id: str,
    *,
    enabled: bool,
    accepted: bool,
    direction: str,
    reason: str = "",
) -> None:
    store = _ha_softphone_store(hass, endpoint_id)
    store.update(
        {
            "video_active": bool(accepted and direction != "inactive"),
            "video_requested": bool(enabled),
            "video_negotiated": bool(accepted),
            "video_status": (
                "degraded" if reason and accepted else "rejected" if reason
                else "active" if accepted and direction != "inactive" else "inactive"
            ),
            "video_failure_reason": reason,
            "video_direction": direction,
            "last_sip_event": "LOCAL_VIDEO_INTENT",
            "media_renegotiations": int(store.get("media_renegotiations") or 0)
            + int(accepted),
        }
    )
    _fire_call_event(hass, dict(store, endpoint_id=endpoint_id), "session")


async def _prepare_client_reinvite(client, *, port: int, formats: tuple, direction: str):
    candidate = await client.async_prepare_video_reinvite(
        local_video_rtp_port=port,
        video_formats=formats,
        video_direction=direction,
    )
    if candidate is not None or int(getattr(client, "last_sip_status_code", 0)) != 491:
        return candidate
    await asyncio.sleep(random.uniform(2.1, 4.0))
    return await client.async_prepare_video_reinvite(
        local_video_rtp_port=port,
        video_formats=formats,
        video_direction=direction,
    )


async def _prepare_endpoint_reinvite(
    manager,
    call_id: str,
    *,
    port: int,
    formats: tuple,
    direction: str,
):
    prepared = await manager.async_prepare_video_reinvite(
        call_id,
        local_video_rtp_port=port,
        video_formats=formats,
        video_direction=direction,
    )
    status, _reason = manager.video_reinvite_result(call_id)
    if prepared is not None or status != 491:
        return prepared
    await asyncio.sleep(random.uniform(2.1, 4.0))
    return await manager.async_prepare_video_reinvite(
        call_id,
        local_video_rtp_port=port,
        video_formats=formats,
        video_direction=direction,
    )


async def async_apply_send_video_intent(
    hass: HomeAssistant,
    endpoint_id: str,
    enabled: bool,
) -> bool:
    """Apply camera intent without creating a parallel call lifecycle."""

    store = _ha_softphone_store(hass, endpoint_id)
    call_id = str(store.get("call_id") or "").strip()
    if not call_id or str(store.get("state") or "") not in _ACTIVE_STATES:
        return False
    cfg = transport_config(hass)
    if enabled and not (
        bool(cfg.get(CONF_SIP_VIDEO, False))
        and bool(cfg.get(CONF_VIDEO_CAMERA_SEND, False))
    ):
        _publish_intent_result(
            hass,
            endpoint_id,
            enabled=enabled,
            accepted=False,
            direction="inactive",
            reason="video_disabled",
        )
        return False

    registry = call_registry(hass)
    session = registry.get_session(call_id)
    if session is None:
        return False
    generation = session.generation

    from .local_softphone_runtime import local_softphone_bridge

    bridge = local_softphone_bridge(hass)
    if bridge is not None and bridge.get_call(call_id) is not None:
        snapshot = bridge.set_video_send(call_id, endpoint_id, enabled)
        direction = snapshot.video_direction_for(endpoint_id)
        _publish_intent_result(
            hass,
            endpoint_id,
            enabled=enabled,
            accepted=snapshot.video_enabled,
            direction=direction,
        )
        return True

    client = registry.sip_client_for(call_id)
    if client is not None and client.dialog is not None:
        previous = client.dialog
        formats = _video_formats(client)
        reservation = None
        rtp_socket = None
        rtcp_socket = None
        port = int(previous.local_video_rtp_port or 0)
        if enabled and previous.video_format is None:
            try:
                reservation, rtp_socket, rtcp_socket = reserve_sip_video_media(hass)
                port = int(reservation.ports[1])
            except (OSError, RuntimeError):
                _LOGGER.exception("Could not reserve video media call_id=%s", call_id)
                _publish_intent_result(
                    hass,
                    endpoint_id,
                    enabled=enabled,
                    accepted=False,
                    direction="inactive",
                    reason="local_video_resources_unavailable",
                )
                return False
        direction = "sendrecv" if enabled else "recvonly"
        try:
            candidate = await _prepare_client_reinvite(
                client,
                port=port,
                formats=formats,
                direction=direction,
            )
            current_store = _ha_softphone_store(hass, endpoint_id)
            if candidate is None:
                reason = f"sip_{int(getattr(client, 'last_sip_status_code', 0)) or 488}"
                _publish_intent_result(
                    hass,
                    endpoint_id,
                    enabled=enabled,
                    accepted=previous.video_format is not None,
                    direction=str(previous.local_video_direction or "inactive"),
                    reason=reason,
                )
                return False
            if not registry.is_generation_current(call_id, generation):
                client.abort_prepared_reinvite(previous, candidate)
                return False
            if not client.commit_prepared_reinvite(previous, candidate):
                client.abort_prepared_reinvite(previous, candidate)
                return False
            accepted_video = candidate.video_format is not None
            if reservation is not None and accepted_video:
                client.media_reservation = reservation
                client.video_rtp_socket = rtp_socket
                client.video_rtcp_socket = rtcp_socket
                reservation = None
                rtp_socket = None
                rtcp_socket = None
            if bool(current_store.get("send_video", False)) == bool(enabled):
                _publish_intent_result(
                    hass,
                    endpoint_id,
                    enabled=enabled,
                    accepted=accepted_video,
                    direction=str(candidate.local_video_direction or "inactive"),
                    reason="" if accepted_video or not enabled else "video_rejected",
                )
            return accepted_video or not enabled
        finally:
            if rtp_socket is not None:
                rtp_socket.close()
            if rtcp_socket is not None:
                rtcp_socket.close()
            if reservation is not None:
                reservation.release()

    media = registry.resource_for(call_id, "softphone_media")
    manager = sip_endpoint_manager(hass)
    if not isinstance(media, dict) or manager is None or media.get("invite") is None:
        return False
    previous = media["invite"]
    formats = _video_formats()
    reservation = None
    rtp_socket = None
    rtcp_socket = None
    port = int(media.get("local_video_rtp_port") or 0)
    if enabled and previous.video_format is None:
        try:
            reservation, rtp_socket, rtcp_socket = reserve_sip_video_media(hass)
            port = int(reservation.ports[1])
        except (OSError, RuntimeError):
            _LOGGER.exception("Could not reserve inbound video media call_id=%s", call_id)
            return False
    direction = "sendrecv" if enabled else "recvonly"
    try:
        prepared = await _prepare_endpoint_reinvite(
            manager,
            call_id,
            port=port,
            formats=formats,
            direction=direction,
        )
        current_store = _ha_softphone_store(hass, endpoint_id)
        if prepared is None:
            status, _reason = manager.video_reinvite_result(call_id)
            _publish_intent_result(
                hass,
                endpoint_id,
                enabled=enabled,
                accepted=previous.video_format is not None,
                direction=str(media.get("video_direction") or "inactive"),
                reason=f"sip_{status or 488}",
            )
            return False
        if not registry.is_generation_current(call_id, generation):
            await prepared.restore(
                local_video_rtp_port=int(media.get("local_video_rtp_port") or 0),
                video_formats=formats,
            )
            return False
        if not prepared.commit():
            return False
        media["invite"] = prepared.candidate
        media["camera_send_authorized"] = bool(enabled)
        remote_video = sdp.parse_video_sdp(prepared.candidate.remote_sdp)
        negotiated_direction = (
            sdp.local_direction_for_offer(str(remote_video["direction"]))
            if remote_video is not None
            else "inactive"
        )
        media["video_direction"] = negotiated_direction
        if reservation is not None:
            media["local_video_rtp_port"] = port
            media["video_rtp_reservation"] = reservation
            media["video_rtp_socket"] = rtp_socket
            media["video_rtcp_socket"] = rtcp_socket
            reservation = None
            rtp_socket = None
            rtcp_socket = None
        if bool(current_store.get("send_video", False)) == bool(enabled):
            _publish_intent_result(
                hass,
                endpoint_id,
                enabled=enabled,
                accepted=prepared.candidate.video_format is not None,
                direction=negotiated_direction,
            )
        return True
    finally:
        if rtp_socket is not None:
            rtp_socket.close()
        if rtcp_socket is not None:
            rtcp_socket.close()
        if reservation is not None:
            reservation.release()
