"""Atomic in-memory updates for active browser media sessions."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any


def committed_video_format_state(negotiated: Any) -> dict[str, str]:
    """Project the committed directional formats into the softphone state."""

    return {
        key: video_format.wire_token() if video_format is not None else ""
        for key, video_format in (
            ("video_format", negotiated.video_format),
            ("video_send_format", negotiated.send_video_format),
            ("video_receive_format", negotiated.recv_video_format),
        )
    }


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
    session.remote_audio_rtcp_host = getattr(negotiated, "remote_audio_rtcp_host", "")
    session.remote_audio_rtcp_port = int(getattr(negotiated, "remote_audio_rtcp_port", 0))
    session.audio_rtcp_allowed = negotiated.audio_rtcp_allowed
    session.local_audio_direction = negotiated.local_audio_direction
    session.remote_audio_connection_held = bool(
        negotiated.remote_audio_connection_held
    )
    session.dtmf_payload_type = dtmf_payload_type
    recv_dtmf = getattr(negotiated, "recv_dtmf_format", None)
    session.dtmf_clock_rate = (recv_dtmf.sample_rate if recv_dtmf is not None
                               else getattr(negotiated, "dtmf_clock_rate", 8000))
    session.dtmf_events = frozenset(dtmf_events)
    session.recv_dtmf_formats = tuple(getattr(negotiated, "recv_dtmf_formats", ()))
    session.send_dtmf_formats = tuple(getattr(negotiated, "send_dtmf_formats", ()))
    # SipInvite exposes the negotiated value; SipDialog stores its directional
    # projection. Publish TX with RX in this same session-owned generation.
    send_dtmf = getattr(negotiated, "send_dtmf_format", None)
    session.send_dtmf_payload_type = (send_dtmf.payload_type if send_dtmf is not None
                                    else getattr(negotiated, "send_dtmf_payload_type", None))
    session.send_dtmf_clock_rate = (send_dtmf.sample_rate if send_dtmf is not None
                                  else getattr(negotiated, "send_dtmf_clock_rate", None) or 8000)
    session.send_dtmf_events = (send_dtmf.events if send_dtmf is not None
                              else frozenset(getattr(negotiated, "send_dtmf_events", None) or ()))
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
