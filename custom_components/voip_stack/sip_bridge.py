"""Shared SIP B2BUA bridge primitives."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import sdp
from .sip_client import SipCallClient, SipDialog
from .sip_listener import SipInvite
from .sip_rtp_bridge import RtpPeer, SipRtpRelay
from .sip_video_relay import (
    SipVideoRtpRelay,
    VideoRtpPeer,
    remote_can_receive,
    remote_can_send,
)


@dataclass(frozen=True, slots=True)
class VideoBridgeAnswer:
    """Video contract that can be committed to the inbound answer."""

    video_format: sdp.RtpVideoFormat
    direction: str


def build_pending_invite_video_relay(
    invite: SipInvite,
    *,
    remote_host: str,
    left_port: int,
    right_port: int,
    sockets: tuple[Any, Any, Any, Any],
    on_release: Callable[[tuple[int, int]], None] | None = None,
) -> SipVideoRtpRelay:
    """Build a reserved video branch before its outbound answer arrives."""

    if invite.video_format is None:
        raise ValueError("cannot fork video for an audio-only INVITE")
    return SipVideoRtpRelay(
        left=invite_video_rtp_peer(invite),
        right=VideoRtpPeer(
            host=str(remote_host),
            port=0,
            rtcp_host=str(remote_host),
            rtcp_port=0,
            video_format=invite.video_format,
            local_video_format=invite.video_format,
            signaling_host=str(remote_host),
        ),
        left_port=int(left_port),
        right_port=int(right_port),
        left_socket=sockets[0],
        right_socket=sockets[2],
        left_rtcp_socket=sockets[1],
        right_rtcp_socket=sockets[3],
        on_release=on_release,
    )


def configure_answered_invite_video_relay(
    invite: SipInvite,
    dialog: SipDialog,
    relay: SipVideoRtpRelay,
) -> VideoBridgeAnswer | None:
    """Commit an exact passthrough contract after the fork leg answers."""

    remote_video = dialog.video_format
    source_video = (
        sdp.video_answer_contract(invite.video_format, remote_video)
        if invite.video_format is not None and remote_video is not None
        else None
    )
    source_directional = (
        sdp.video_offer_answer_directional(invite.video_format, source_video)
        if invite.video_format is not None and source_video is not None
        else None
    )
    if not (
        remote_video is not None
        and source_video is not None
        and source_directional is not None
        and dialog.remote_video_rtp_port > 0
        and sdp.video_formats_passthrough_compatible(
            source_directional.recv,
            dialog.send_video_format,
        )
        and sdp.video_formats_passthrough_compatible(
            dialog.recv_video_format,
            source_directional.send,
        )
    ):
        return None

    relay.left.video_format = source_directional.send
    relay.left.local_video_format = source_directional.recv
    relay.reconfigure_peer("right", dialog_video_rtp_peer(dialog))
    return VideoBridgeAnswer(
        video_format=source_video,
        direction=sdp.constrained_video_direction(
            invite.video_format.direction,
            allow_send=(
                remote_can_send(remote_video)
                and not invite.remote_video_connection_held
            ),
            allow_receive=remote_can_receive(
                remote_video,
                connection_held=dialog.remote_video_connection_held,
            ),
        ),
    )


def invite_rtp_peer(invite: SipInvite) -> RtpPeer:
    """Build the relay peer represented by one inbound offer."""

    dtmf = sdp.first_offered_dtmf_format(invite.remote_sdp)
    return RtpPeer(
        host=invite.remote_rtp_host,
        port=invite.remote_rtp_port,
        payload_type=invite.recv_format.payload_type,
        audio_format=invite.recv_format.audio_format,
        rtp_format=invite.recv_format,
        send_payload_type=invite.send_format.payload_type,
        send_audio_format=invite.send_format.audio_format,
        send_rtp_format=invite.send_format,
        dtmf_payload_type=(dtmf.payload_type if dtmf is not None else None),
        dtmf_clock_rate=(dtmf.sample_rate if dtmf is not None else 8000),
        dtmf_events=(dtmf.events if dtmf is not None else frozenset()),
        can_send=invite.remote_audio_direction in {"sendonly", "sendrecv"},
        can_receive=(
            invite.remote_audio_direction in {"recvonly", "sendrecv"}
            and not invite.remote_audio_connection_held
        ),
        connection_held=invite.remote_audio_connection_held,
        signaling_host=invite.source_host,
    )


def dialog_rtp_peer(dialog: SipDialog) -> RtpPeer:
    """Build the relay peer represented by one established outbound dialog."""

    return RtpPeer(
        host=dialog.remote_rtp_host,
        port=dialog.remote_rtp_port,
        payload_type=dialog.recv_format.payload_type,
        audio_format=dialog.recv_format.audio_format,
        rtp_format=dialog.recv_format,
        send_payload_type=dialog.send_format.payload_type,
        send_audio_format=dialog.send_format.audio_format,
        send_rtp_format=dialog.send_format,
        dtmf_payload_type=dialog.dtmf_payload_type,
        dtmf_clock_rate=dialog.dtmf_clock_rate,
        dtmf_events=dialog.dtmf_events,
        can_send=dialog.remote_audio_direction in {"sendonly", "sendrecv"},
        can_receive=(
            dialog.remote_audio_direction in {"recvonly", "sendrecv"}
            and not dialog.remote_audio_connection_held
        ),
        connection_held=dialog.remote_audio_connection_held,
        signaling_host=dialog.remote_host,
    )


def invite_video_rtp_peer(invite: SipInvite) -> VideoRtpPeer:
    """Build a directional video peer represented by an inbound offer."""

    if invite.video_format is None:
        raise ValueError("cannot build video peer for an audio-only INVITE")
    return VideoRtpPeer(
        host=invite.remote_video_rtp_host,
        port=int(invite.remote_video_rtp_port),
        rtcp_host=invite.remote_video_rtcp_host or invite.remote_video_rtp_host,
        rtcp_port=int(
            invite.remote_video_rtcp_port or invite.remote_video_rtp_port + 1
        ),
        video_format=invite.video_format,
        local_video_format=invite.recv_video_format,
        signaling_host=invite.source_host,
        connection_held=invite.remote_video_connection_held,
    )


def dialog_video_rtp_peer(dialog: SipDialog) -> VideoRtpPeer:
    """Build a directional video peer from an established outbound dialog."""

    if dialog.video_format is None:
        raise ValueError("cannot build video peer for an audio-only dialog")
    return VideoRtpPeer(
        host=dialog.remote_video_rtp_host,
        port=int(dialog.remote_video_rtp_port),
        rtcp_host=dialog.remote_video_rtcp_host or dialog.remote_video_rtp_host,
        rtcp_port=int(
            dialog.remote_video_rtcp_port or dialog.remote_video_rtp_port + 1
        ),
        video_format=dialog.video_format,
        local_video_format=dialog.recv_video_format,
        signaling_host=dialog.remote_host,
        connection_held=dialog.remote_video_connection_held,
    )


def build_invite_client_relay(
    *,
    invite: SipInvite,
    client: SipCallClient,
    source_relay_port: int,
    dest_relay_port: int,
    debug_capture: bool = False,
    on_release: Callable[[tuple[int, int]], None] | None = None,
) -> SipRtpRelay:
    """Build the RTP relay for an inbound INVITE bridged to an outbound client."""
    if client.dialog is None:
        raise ValueError("cannot bridge SIP client without an established dialog")
    return SipRtpRelay(
        left=invite_rtp_peer(invite),
        right=dialog_rtp_peer(client.dialog),
        left_port=source_relay_port,
        right_port=dest_relay_port,
        debug_capture=debug_capture,
        capture_name=f"{invite.call_id}_{client.dialog_ids.call_id}",
        on_release=on_release,
    )


def build_local_client_relay(
    *,
    client: SipCallClient,
    local_host: str,
    local_to_relay_format: sdp.RtpPcmFormat,
    relay_to_local_format: sdp.RtpPcmFormat,
    source_relay_port: int,
    dest_relay_port: int,
    capture_name: str,
    debug_capture: bool = False,
    on_release: Callable[[tuple[int, int]], None] | None = None,
) -> SipRtpRelay:
    """Build a browser-loopback to established SIP dialog RTP relay.

    A local browser leg is not a SIP offer and therefore has no SDP to parse
    or UAS transaction to answer.  Its websocket RTP adapter learns the
    ephemeral source port through symmetric RTP on the loopback host.
    """
    if client.dialog is None:
        raise ValueError("cannot bridge SIP client without an established dialog")
    local_peer = RtpPeer(
        host=str(local_host),
        port=0,
        payload_type=local_to_relay_format.payload_type,
        audio_format=local_to_relay_format.audio_format,
        rtp_format=local_to_relay_format,
        send_payload_type=relay_to_local_format.payload_type,
        send_audio_format=relay_to_local_format.audio_format,
        send_rtp_format=relay_to_local_format,
        signaling_host=str(local_host),
    )
    return SipRtpRelay(
        left=local_peer,
        right=dialog_rtp_peer(client.dialog),
        left_port=source_relay_port,
        right_port=dest_relay_port,
        debug_capture=debug_capture,
        capture_name=capture_name,
        on_release=on_release,
    )
