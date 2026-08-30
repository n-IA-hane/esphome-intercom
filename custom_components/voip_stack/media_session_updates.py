"""Atomic in-memory updates for active browser media sessions."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any


def commit_audio_session_update(
    session: Any,
    negotiated: Any,
    *,
    dtmf_payload_type: int | None,
    dtmf_events: Collection[int],
) -> None:
    """Apply one committed audio contract, then wake its browser owner."""

    session.send_format = negotiated.send_format
    session.recv_format = negotiated.recv_format
    session.remote_rtp_host = negotiated.remote_rtp_host
    session.remote_rtp_port = int(negotiated.remote_rtp_port)
    session.local_audio_direction = negotiated.local_audio_direction
    session.remote_audio_connection_held = bool(
        negotiated.remote_audio_connection_held
    )
    session.dtmf_payload_type = dtmf_payload_type
    session.dtmf_events = frozenset(dtmf_events)
    session.media_generation += 1
    session.update_event.set()


def commit_video_session_update(
    session: Any,
    negotiated: Any,
    *,
    local_direction: str,
) -> None:
    """Apply one committed video contract, then wake its browser owner."""

    video_format = negotiated.video_format
    if video_format is None:
        raise ValueError("committed video update has no video format")
    session.remote_rtp_host = negotiated.remote_video_rtp_host
    session.remote_rtp_port = int(negotiated.remote_video_rtp_port)
    session.remote_rtcp_host = (
        negotiated.remote_video_rtcp_host or negotiated.remote_video_rtp_host
    )
    session.remote_rtcp_port = int(
        negotiated.remote_video_rtcp_port
        or int(negotiated.remote_video_rtp_port) + 1
    )
    session.remote_rtcp_mux = bool(negotiated.remote_video_rtcp_mux)
    session.remote_video_payload_types = tuple(
        negotiated.remote_video_payload_types
    )
    session.video_format = video_format
    session.local_video_format = negotiated.recv_video_format
    session.local_direction = local_direction
    session.remote_connection_held = bool(
        negotiated.remote_video_connection_held
    )
    session.media_generation += 1
    session.update_event.set()


def commit_softphone_projection_update(
    hass: Any,
    *,
    endpoint_id: str,
    device_id: str,
    call_id: str,
    negotiated: Any,
    video_direction: str,
    sip_event: str,
) -> None:
    """Publish one committed media renegotiation to the browser owner."""

    from .websocket_api import _fire_call_event, _ha_softphone_store

    store = _ha_softphone_store(hass, endpoint_id)
    if str(store.get("call_id") or "") != call_id:
        return
    video = negotiated.video_format
    video_active = bool(video is not None and video_direction != "inactive")
    store.update(
        {
            "audio_direction": negotiated.local_audio_direction,
            "audio_connection_held": negotiated.remote_audio_connection_held,
            "video_active": video_active,
            "video_requested": video is not None,
            "video_negotiated": video is not None,
            "video_status": "active" if video_active else "inactive",
            "video_failure_reason": "",
            "video_format": video.wire_token() if video is not None else "",
            "video_send_format": (
                negotiated.send_video_format.wire_token()
                if negotiated.send_video_format is not None
                else ""
            ),
            "video_receive_format": (
                negotiated.recv_video_format.wire_token()
                if negotiated.recv_video_format is not None
                else ""
            ),
            "video_direction": video_direction,
            "video_connection_held": negotiated.remote_video_connection_held,
            "last_sip_event": sip_event,
            "media_renegotiations": int(store.get("media_renegotiations") or 0) + 1,
        }
    )
    _fire_call_event(
        hass,
        dict(store, endpoint_id=endpoint_id, device_id=device_id),
        "session",
    )
