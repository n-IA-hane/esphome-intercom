"""Detached receive-processing data, without sockets, tasks or call ownership."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .core import rtp, sdp
from .core.video_rtp import (
    H264Depacketizer, JpegDepacketizer, Vp8Depacketizer,
    RtpExtendedSequenceTracker, RtpReorderBuffer,
)

if TYPE_CHECKING:
    from .video_ws_view import _VideoMediaSession

VideoDepacketizer = H264Depacketizer | JpegDepacketizer | Vp8Depacketizer


@dataclass(slots=True)
class VideoReceiveRoute:
    remote_host: str
    remote_port: int
    remote_rtcp_host: str
    remote_rtcp_port: int
    remote_rtcp_host_explicit: bool
    remote_rtcp_offset: int
    latched_source: tuple[str, int] | None = None
    latched_ssrc: int | None = None
    latched_rtcp_source: tuple[str, int] | None = None

    @classmethod
    def from_session(cls, session: _VideoMediaSession) -> VideoReceiveRoute:
        return cls(str(session.remote_rtp_host), int(session.remote_rtp_port),
            str(session.remote_rtcp_host or session.remote_rtp_host), int(session.remote_rtcp_port),
            bool(session.remote_rtcp_host and session.remote_rtcp_host != session.remote_rtp_host),
            0 if session.remote_rtcp_mux else int(session.remote_rtcp_port) - int(session.remote_rtp_port))

    def rtp_destination(self) -> tuple[str, int]:
        return self.remote_host, self.remote_port


@dataclass(slots=True)
class VideoReceiveState:
    route: VideoReceiveRoute
    browser_format: sdp.RtpVideoFormat
    depacketizer: VideoDepacketizer | None
    applied_media_generation: int
    active_pipeline_signature: tuple[object, ...]
    counters: dict[str, int]
    reorder: RtpReorderBuffer[rtp.RtpPacket] = field(default_factory=RtpReorderBuffer)
    input_reorder: RtpReorderBuffer[tuple[bytes, rtp.RtpPacket]] = field(default_factory=RtpReorderBuffer)
    extended_sequence: RtpExtendedSequenceTracker = field(default_factory=RtpExtendedSequenceTracker)
    needs_key_frame: bool = True
    highest_sequence: int = 0
    last_keyframe_feedback: float = 0.0
    last_browser_keyframe_feedback: float = 0.0
    restart_notified_generation: int = -1
    admission_duplicates: int = 0


def video_receive_counters(keepalives: int) -> dict[str, int]:
    return {
        "video_rtp_rx_packets": 0,
        "video_rtp_tx_packets": 0,
        "video_rtp_rx_bytes": 0,
        "video_rtp_tx_bytes": 0,
        "video_rtp_tx_payload_bytes": 0,
        "video_rtp_drop_queue": 0,
        "video_access_units_rx": 0,
        "video_access_units_tx": 0,
        "video_drop_addr": 0,
        "video_drop_payload_type": 0,
        "video_drop_error": 0,
        "video_drop_wait_keyframe": 0,
        "video_drop_direction": 0,
        "video_drop_connection_hold": 0,
        "video_reordered_packets": 0,
        "video_lost_packets": 0,
        "video_duplicate_packets": 0,
        "video_keyframe_requests": 0,
        "video_sip_keyframe_requests": 0,
        "video_sip_keyframe_accepted": 0,
        "video_symmetric_rtp_keepalives": keepalives,
        "video_symmetric_rtp_keepalive_payload_type": 0,
        "video_access_unit_queue_max": 0,
        "video_access_unit_queue_drops": 0,
        "video_rate_limited_access_units": 0,
        "video_browser_keyframe_requests": 0,
        "video_rtcp_rx_packets": 0,
        "video_rtcp_rx_bytes": 0,
        "video_rtcp_tx_packets": 0,
        "video_rtcp_tx_bytes": 0,
        "video_rtcp_drop_addr": 0,
        "video_rtcp_drop_error": 0,
        "video_rtcp_drop_queue": 0,
        "video_rtcp_send_errors": 0,
        "video_rtcp_task_errors": 0,
        "video_keepalive_task_errors": 0,
        "video_rtcp_pli_rx": 0,
        "video_rtcp_fir_rx": 0,
        "video_rtcp_keyframe_requests_to_browser": 0,
        "video_pipeline_restarts": 0,
        "video_jpeg_normalized": 0,
        "video_jpeg_normalizer_errors": 0,
    }
