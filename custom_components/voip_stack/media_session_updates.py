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
    session.recv_formats = getattr(negotiated, "recv_formats", ())
    session.send_formats = getattr(negotiated, "send_formats", ())
    session.match_received_codec = getattr(negotiated, "match_received_codec", False)
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
