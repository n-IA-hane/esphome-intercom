"""Pure SIP offer/answer policy shared by every media owner.

This module deliberately owns no sockets and mutates no calls.  It decides
whether a proposed video contract can be committed by a direct softphone or
by a bridged RTP leg.  Keeping this policy outside the endpoint runtime makes
the wire-level replay tests exercise the same rules as production.
"""

from __future__ import annotations

from dataclasses import dataclass

from .core.sdp import (
    RtpVideoFormat,
    directional_video_renegotiation_compatible,
    remote_can_receive,
    remote_can_send,
    video_formats_passthrough_compatible,
)


@dataclass(frozen=True, slots=True)
class VideoOfferDecision:
    """Result of validating one pending video offer."""

    accepted: bool
    reason: str = ""


def validate_direct_video_reoffer(
    previous_send: RtpVideoFormat | None,
    previous_recv: RtpVideoFormat | None,
    updated_send: RtpVideoFormat | None,
    updated_recv: RtpVideoFormat | None,
) -> VideoOfferDecision:
    """Validate a re-offer without changing the committed media contract."""

    # RFC 3264 section 8 permits a new media stream to be added by a later
    # offer.  Its codec contract is negotiated from scratch, so there is no
    # previous video format to compare.  Likewise, port-zero/removal is a
    # valid update and must not invalidate the established audio dialog.
    if previous_send is None or updated_send is None:
        return VideoOfferDecision(True)
    if directional_video_renegotiation_compatible(
        previous_send,
        previous_recv,
        updated_send,
        updated_recv,
    ):
        return VideoOfferDecision(True)
    return VideoOfferDecision(False, "incompatible_video_contract")


def validate_bridged_video_reoffer(
    previous_send: RtpVideoFormat | None,
    updated_send: RtpVideoFormat | None,
    updated_recv: RtpVideoFormat | None,
    *,
    peer_send: RtpVideoFormat | None,
    peer_recv: RtpVideoFormat | None,
    peer_direction: RtpVideoFormat | None,
    peer_held: bool = False,
    updated_held: bool = False,
    caller_to_peer_transcoding: bool = False,
    peer_to_caller_transcoding: bool = False,
) -> VideoOfferDecision:
    """Validate only RTP paths active on both sides of a bridged call leg."""

    if updated_send is None:
        return VideoOfferDecision(True)
    if peer_direction is None:
        return VideoOfferDecision(False, "peer_has_no_video_contract")

    caller_sends = remote_can_send(updated_send)
    caller_receives = remote_can_receive(
        updated_send,
        connection_held=updated_held,
    )
    peer_sends = remote_can_send(peer_direction)
    peer_receives = remote_can_receive(
        peer_direction,
        connection_held=peer_held,
    )
    if (
        caller_sends
        and peer_receives
        and not caller_to_peer_transcoding
        and not video_formats_passthrough_compatible(updated_recv, peer_send)
    ):
        return VideoOfferDecision(False, "caller_to_peer_contract_incompatible")
    if (
        caller_receives
        and peer_sends
        and not peer_to_caller_transcoding
        and not video_formats_passthrough_compatible(peer_recv, updated_send)
    ):
        return VideoOfferDecision(False, "peer_to_caller_contract_incompatible")
    return VideoOfferDecision(True)
