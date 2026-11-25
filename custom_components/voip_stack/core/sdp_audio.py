"""Audio RTP capabilities and directional codec and packet-time negotiation."""

from __future__ import annotations

from dataclasses import replace

from ..peer_media_profile import legacy_pcm_compatibility
from .audio_format import AudioFormat
from .sdp_audio_facts import (
    audio_format_to_rtp as audio_format_to_rtp,
    is_rtp_pcm_mappable as is_rtp_pcm_mappable,
    offered_dtmf_formats as offered_dtmf_formats,
    offered_media_descriptions as offered_media_descriptions,
    offered_pcm_formats as offered_pcm_formats,
    parse_rtp_audio_capability as parse_rtp_audio_capability,
)
from .sdp_document import SdpDocument
from .sdp_parser import (
    format_direction_for_remote,
    local_direction_for_offer,
    parse_sdp,
)
from .sdp_types import (
    RtpDtmfDirection,
    RtpDtmfFormat,
    RtpPcmDirection,
)

def negotiate_dtmf_answer_directional(
    remote_sdp: str | bytes,
    local_offer_sdp: str | bytes,
    *,
    send_clock_rate: int | None = None,
    receive_clock_rate: int | None = None,
    audio: RtpPcmDirection | None = None,
) -> RtpDtmfDirection | None:
    """Project the media engine's directional RFC 4733 decision."""
    from .media.negotiator import negotiate_dtmf

    return negotiate_dtmf(remote_sdp, local_offer_sdp, audio=audio,
        send_clock_rate=send_clock_rate, receive_clock_rate=receive_clock_rate)


def negotiate_dtmf_answer(
    remote_sdp: str | bytes,
    local_offer_sdp: str | bytes,
) -> RtpDtmfFormat | None:
    """Return the receive-side RFC 4733 mapping for compatibility callers."""

    selected = negotiate_dtmf_answer_directional(remote_sdp, local_offer_sdp)
    return selected.recv if selected is not None else None


def dtmf_format_for_clock(remote_sdp, clock_rate):
    """Compatibility projection of the media engine's RFC 4733 decision."""
    from .media.negotiator import negotiate_dtmf

    selected = negotiate_dtmf(remote_sdp, send_clock_rate=clock_rate, receive_clock_rate=clock_rate)
    return selected.recv if selected is not None else None


def first_offered_dtmf_format(remote_sdp):
    """Retain the old projection while the engine owns its preference order."""
    from .media.negotiator import negotiate_dtmf

    selected = negotiate_dtmf(remote_sdp)
    return selected.recv if selected is not None else None


def negotiate_directional(
    remote_sdp: str | bytes,
    local_send_preferred: list[AudioFormat],
    local_recv_preferred: list[AudioFormat],
    *,
    allow_dahua_pcm: bool = False,
    codec_policy=None,
    local_recv_capabilities: tuple[AudioFormat, ...] | None = None,
    local_capabilities=None,
    established: RtpPcmDirection | None = None,
    previous_payloads=None,
) -> RtpPcmDirection | None:
    from .media import negotiator

    remote_sdp = SdpDocument.parse(remote_sdp)
    previous = (negotiator.LegContract(established.send, established.recv, None,
                    receive_accept=established.recv_formats, send_accept=established.send_formats)
                if established is not None else None)
    contract = negotiator.answer_offer(remote_sdp, local_send_preferred, local_recv_preferred,
        capabilities=local_capabilities, codec_policy=codec_policy,
        compatibility=legacy_pcm_compatibility(allow_dahua_pcm), established=previous, receive_capabilities=local_recv_capabilities,
        previous_payloads=previous_payloads)
    return negotiator.as_direction(contract, remote_sdp)


def negotiate_answer_directional(
    remote_sdp: str | bytes,
    local_send_preferred: list[AudioFormat],
    local_recv_preferred: list[AudioFormat],
    *,
    local_offer_direction: str = "sendrecv",
    local_offer_sdp: str | bytes | None = None,
    allow_dahua_pcm: bool = False,
    local_capabilities=None,
    codec_policy=None,
    established: RtpPcmDirection | None = None,
    previous_payloads=None,
) -> RtpPcmDirection | None:
    """Apply an exact answer, preserving the capability-only API when no offer is supplied."""
    from .media import negotiator

    remote_sdp = SdpDocument.parse(remote_sdp)
    if local_offer_sdp is not None:
        local_offer_sdp = SdpDocument.parse(local_offer_sdp)
    format_direction = None
    if local_offer_sdp is None:
        parsed = parse_sdp(remote_sdp)
        local_direction = local_direction_for_offer(parsed["direction"],
            remote_connection_held=bool(parsed["connection_held"]))
        format_direction = format_direction_for_remote(parsed["direction"], local_direction,
            inactive_default=local_offer_direction)
    contract = negotiator.apply_answer(local_offer_sdp, remote_sdp,
        local_send_preferred, local_recv_preferred, capabilities=local_capabilities,
        codec_policy=codec_policy, compatibility=legacy_pcm_compatibility(allow_dahua_pcm), format_direction=format_direction,
        established=negotiator.LegContract(established.send, established.recv, None,
            receive_accept=established.recv_formats, send_accept=established.send_formats)
            if established is not None else None, previous_payloads=previous_payloads)
    selected = negotiator.as_direction(contract, remote_sdp)
    # Compatibility callers have no exact offer authorizing transmit alternatives.
    return replace(selected, send_formats=()) if selected is not None and local_offer_sdp is None else selected
