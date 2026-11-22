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
from .capability import PeerEvidence
from .payload_map import PayloadMap

LegContract = leg.LegContract
Refusal = leg.Refusal
OfferPlan = leg.OfferPlan


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
                 receive_capabilities=None):
    """Choose one local answer from actual local capabilities and a remote offer."""
    policy = policy_for_leg(send_formats, receive_formats, codec_policy=codec_policy, capabilities=capabilities)
    if receive_capabilities is not None:
        policy = replace(policy, receive_capabilities=tuple(receive_capabilities))
    return leg.answer_offer(remote_sdp, policy, dahua=dahua, established=established)


def apply_answer(local_offer_sdp, remote_sdp, send_formats, receive_formats, *,
                 codec_policy=None, capabilities=None, dahua=False):
    """Apply an answer only to the exact offer and policy that were sent."""
    return leg.apply_answer(local_offer_sdp, remote_sdp,
        policy_for_leg(send_formats, receive_formats, codec_policy=codec_policy, capabilities=capabilities),
        dahua=dahua)


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
        if direction in {"sendonly", "sendrecv"} else ())
