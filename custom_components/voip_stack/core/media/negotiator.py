"""Public media negotiation API, independent of calls, sockets and Home Assistant.

The SIP adapters supply local capabilities and the exact descriptions exchanged.
This module owns no registrations, tasks or media resources.
"""

from __future__ import annotations

from dataclasses import replace

from ..sdp_parser import local_direction_for_offer, parse_sdp
from ..sdp_types import RtpPcmDirection
from ..codec_policy import DEFAULT_AUDIO_CODECS
from . import leg
from .capability import PeerEvidence, resolve_peer_media_profile
from .payload_map import PayloadMap, PayloadConflict, TELEPHONE_EVENT

LegContract = leg.LegContract
Refusal = leg.Refusal
OfferPlan = leg.OfferPlan
negotiate_dtmf = leg.negotiate_dtmf


def policy_for_leg(send_formats, receive_formats, *, codec_policy=None, capabilities=None) -> leg.LegPolicy:
    """Convert explicit preferences and capabilities into the leg engine's input."""
    send = tuple(send_formats)
    receive = tuple(receive_formats)
    enabled = getattr(codec_policy, "audio", None)
    allowed = DEFAULT_AUDIO_CODECS if enabled is None else enabled
    return leg.LegPolicy(
        send_rank=tuple(dict.fromkeys((item.sample_rate, item.channels) for item in send)),
        receive_rank=tuple(dict.fromkeys((item.sample_rate, item.channels) for item in receive)),
        budget=getattr(capabilities, "transmit_payload_bytes", 1200),
        encodings=frozenset(allowed),
        codec_order=tuple(enabled) if enabled is not None and getattr(codec_policy, "audio_ordered", True) else (),
        send_capabilities=send if capabilities is None else None,
        receive_capabilities=receive if capabilities is None else None,
        capabilities=capabilities,
    )


def answer_offer(remote_sdp, send_formats, receive_formats, *, codec_policy=None,
                 capabilities=None, dahua=False, established: LegContract | None = None,
                 receive_capabilities=None, previous_payloads=None):
    """Choose one local answer from actual local capabilities and a remote offer."""
    policy = policy_for_leg(send_formats, receive_formats, codec_policy=codec_policy, capabilities=capabilities)
    if receive_capabilities is not None:
        policy = replace(policy, receive_capabilities=tuple(receive_capabilities))
    return _remember_payloads(leg.answer_offer(remote_sdp, policy, dahua=dahua, established=established), previous_payloads)


def apply_answer(local_offer_sdp, remote_sdp, send_formats, receive_formats, *,
                 codec_policy=None, capabilities=None, dahua=False, format_direction=None,
                 established: LegContract | None = None, previous_payloads=None):
    """Apply an answer to its exact offer, or to supplied capabilities for compatibility."""
    send_formats, receive_formats = tuple(send_formats), tuple(receive_formats)
    return _remember_payloads(leg.apply_answer(local_offer_sdp, remote_sdp,
        policy_for_leg(send_formats, receive_formats, codec_policy=codec_policy, capabilities=capabilities),
        dahua=dahua, format_direction=format_direction,
        send_preferred=send_formats, receive_preferred=receive_formats, established=established), previous_payloads)


def payload_history(previous, audio, events=None):
    """Retain every negotiated audio/event mapping across dialog generations."""
    def numbers(formats):
        mappings = {}
        for fmt in formats:
            identity = (getattr(fmt, "encoding", TELEPHONE_EVENT).upper(), fmt.sample_rate,
                        getattr(fmt, "channels", 1))
            if mappings.get(fmt.payload_type, identity) != identity:
                raise PayloadConflict("SDP assigned one payload to different negotiated codecs")
            mappings[fmt.payload_type] = identity
        return mappings

    send, receive = audio.send_formats or (audio.send,), audio.recv_formats or (audio.recv,)
    return previous.with_mappings(
        send=numbers((*send, *(events.send_formats if events is not None else ()))),
        receive=numbers((*receive, *(events.recv_formats if events is not None else ()))))


def _remember_payloads(result, previous):
    if previous is None or isinstance(result, Refusal):
        return result
    try:
        audio = RtpPcmDirection(result.send, result.receive, result.receive_accept, result.send_accept)
        return replace(result, payloads=payload_history(previous, audio, result.dtmf_direction))
    except PayloadConflict as error:
        return Refusal(str(error), result.trace)


def renegotiate(remote_sdp, previous: LegContract, send_formats, receive_formats, **options):
    """Prepare a new immutable contract; a refusal leaves the previous contract intact."""
    return answer_offer(remote_sdp, send_formats, receive_formats, established=previous, **options)


def create_offer(send_formats, receive_formats, evidence: PeerEvidence | None = None, *, codec_policy=None,
                 capabilities=None, previous: PayloadMap | None = None, **options) -> OfferPlan:
    """Plan a complete offer, or reduce it from complete binding evidence."""
    policy = policy_for_leg(send_formats, receive_formats, codec_policy=codec_policy, capabilities=capabilities)
    from .offers import create_complete_offer
    return create_complete_offer(policy, send_formats, receive_formats, capabilities=capabilities,
                                 previous=previous, codec_policy=codec_policy, evidence=evidence, **options)


def sip_offer_plan(evidence=None, *, codec_policy=None, peer_metadata=None,
                   peer_user_agent=None, trunk=False):
    """Own route and documented peer preferences over the same backend capabilities."""
    from ..audio_capabilities import HA_AUDIO_CAPABILITIES
    from ..audio_format import HA_SIP_PCM_TX_FORMATS, HA_SIP_PCM_RX_FORMATS, HA_TRUNK_AUDIO_FORMATS

    profile = resolve_peer_media_profile(peer_metadata, user_agent_override=peer_user_agent)
    compatibility_preferences = trunk or profile.is_dahua or profile.include_dahua_pcm
    send = HA_TRUNK_AUDIO_FORMATS if compatibility_preferences else HA_SIP_PCM_TX_FORMATS
    receive = HA_TRUNK_AUDIO_FORMATS if compatibility_preferences else HA_SIP_PCM_RX_FORMATS
    plan = create_offer(send, receive, evidence, codec_policy=codec_policy,
        capabilities=HA_AUDIO_CAPABILITIES, common=True, dahua=profile.include_dahua_pcm)
    return replace(plan, include_dahua_pcm=profile.include_dahua_pcm,
                   match_received_codec=profile.match_received_codec)


def browser_rtp_format():
    """Describe the real browser adapter through the ordinary RTP offer planner."""
    from ..audio_capabilities import HA_AUDIO_CAPABILITIES
    from ..audio_format import HA_SIP_PCM_TX_FORMATS
    from ..codec_policy import CodecPolicy

    plan = create_offer(HA_SIP_PCM_TX_FORMATS, HA_SIP_PCM_TX_FORMATS,
        codec_policy=CodecPolicy(audio=("L16",)), capabilities=HA_AUDIO_CAPABILITIES)
    return plan.send_formats[0]


def as_direction(result, remote_sdp) -> RtpPcmDirection | None:
    """Project the contract into the existing media consumer's directional value."""
    if isinstance(result, Refusal):
        return None
    parsed = parse_sdp(remote_sdp)
    direction = local_direction_for_offer(parsed["direction"],
        remote_connection_held=bool(parsed["connection_held"]))
    receives = result.receive_accept or (result.receive,)
    return RtpPcmDirection(result.send, result.receive,
        recv_formats=receives if direction in {"recvonly", "sendrecv"} else (),
        send_formats=(result.send_accept or (result.send,))
        if direction in {"sendonly", "sendrecv"} else (), payloads=result.payloads)
