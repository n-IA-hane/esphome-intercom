"""Leg negotiation: the single owner of the media contract of one SIP leg.

Implements the selection rules S1 to S4, R1 and P5 of docs/MEDIA_NEGOTIATION_CONTRACT.md on top
of the packet time and payload primitives. Pure functions over a parsed description: no sockets,
no ports, no dialog state. The result uses the wire value types the rest of the integration
already consumes, so switching an entry point to this engine changes nothing downstream.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from ..codec_capabilities import common_sip_codecs
from ..sdp_audio import offered_dtmf_formats, offered_pcm_formats
from ..sdp_parser import parse_audio_flow_attributes
from ..sdp_types import RtpDtmfFormat, RtpPcmFormat
from .codecs import CODECS
from .packet_time import PacketTimes, ReceiverBounds, choose_transmit_packet_ms

# Decoded PCM geometry: (sample rate, channels).
Geometry = tuple[int, int]

OPUS_FRAMES = PacketTimes(explicit=(2.5, 5, 10, 20, 40, 60))
ANY_DURATION = PacketTimes()


@dataclass(frozen=True, slots=True)
class LegPolicy:
    """What is preferred toward one peer. Geometries are ranked best first; a geometry that is
    not listed ranks after every listed one. Equal rank is decided by the order of the offer."""

    send_rank: tuple[Geometry, ...]
    # Receive order for peers that state a direction per payload (profile esp_directional_v1).
    receive_rank: tuple[Geometry, ...]
    default_packet_ms: int = 20
    budget: int = 1200
    encodings: frozenset[str] | None = None


PHONE_POLICY = LegPolicy(
    send_rank=((48000, 1), (48000, 2), (32000, 1), (16000, 1), (8000, 1)),
    receive_rank=((16000, 1), (48000, 1), (48000, 2), (32000, 1), (8000, 1)),
)
TRUNK_POLICY = LegPolicy(
    send_rank=((48000, 2), (48000, 1), (16000, 1), (8000, 1)),
    receive_rank=((48000, 2), (48000, 1), (16000, 1), (8000, 1)),
)


@dataclass(frozen=True, slots=True)
class LegContract:
    send: RtpPcmFormat
    receive: RtpPcmFormat
    dtmf: RtpDtmfFormat | None
    trace: tuple[str, ...] = field(default=())
    # S5: every mapping accepted on receive, numbered as this side declared them.
    receive_accept: tuple[RtpPcmFormat, ...] = field(default=())


@dataclass(frozen=True, slots=True)
class Refusal:
    reason: str
    trace: tuple[str, ...] = field(default=())


def _geometry(fmt: RtpPcmFormat) -> Geometry:
    spec = CODECS[fmt.encoding]
    return (spec.decoded_rate or fmt.sample_rate, spec.decoded_channels or fmt.channels)


def _usable(fmt: RtpPcmFormat, policy: LegPolicy, *, dahua: bool) -> bool:
    """S1: enabled by policy, with a working adapter, and a mapping the adapter implements."""
    name = fmt.encoding
    if name not in CODECS or (policy.encodings is not None and name not in policy.encodings):
        return False
    if name == "OPUS":
        return "OPUS" in common_sip_codecs() and (fmt.sample_rate, fmt.channels) == (48000, 2)
    if name == "G722":
        return "G722" in common_sip_codecs() and (fmt.sample_rate, fmt.channels) == (8000, 1)
    if name in {"PCMA", "PCMU"}:
        return fmt.sample_rate == 8000
    if name == "PCM":
        return dahua and (fmt.sample_rate, fmt.channels) == (16000, 1)
    return fmt.channels in (1, 2)


def _rank(order: tuple[Geometry, ...], fmt: RtpPcmFormat) -> int:
    geometry = _geometry(fmt)
    return order.index(geometry) if geometry in order else len(order)


def _packet_ms(fmt: RtpPcmFormat, policy: LegPolicy, receiver: ReceiverBounds):
    spec = CODECS[fmt.encoding]
    return choose_transmit_packet_ms(
        spec, fmt.sample_rate, fmt.channels,
        producible=OPUS_FRAMES if spec.octets_per_sample is None else ANY_DURATION,
        default_ms=policy.default_packet_ms, receiver=receiver, budget=policy.budget)


def _dtmf(remote_sdp, selected: RtpPcmFormat) -> RtpDtmfFormat | None:
    """P5: the telephone event clock of the selected codec, else 8000 Hz."""
    offered = offered_dtmf_formats(remote_sdp)
    for clock in (selected.sample_rate, 8000):
        match = next((item for item in offered if item.sample_rate == clock), None)
        if match is not None:
            return match
    return None


def answer_offer(remote_sdp, policy: LegPolicy, *, dahua: bool = False) -> LegContract | Refusal:
    """Answer a remote audio offer."""
    trace: list[str] = []
    offered = [fmt for fmt in offered_pcm_formats(remote_sdp, allow_dahua_pcm=dahua)
               if _usable(fmt, policy, dahua=dahua)]
    if not offered:
        return Refusal("no offered codec is enabled and available", tuple(trace))
    flows = parse_audio_flow_attributes(remote_sdp)
    peer_receives = [fmt for fmt in offered if flows.get(fmt.payload_type, "sendrecv") in {"recv", "sendrecv"}]
    peer_sends = [fmt for fmt in offered if flows.get(fmt.payload_type, "sendrecv") in {"send", "sendrecv"}]
    directional = bool(flows) and bool(peer_receives) and bool(peer_sends)
    send_candidates = sorted(
        peer_receives if directional else offered,
        key=lambda fmt: (_rank(policy.send_rank, fmt), offered.index(fmt)))
    for candidate in send_candidates:
        transmit = _packet_ms(candidate, policy, ReceiverBounds(
            ptime=candidate.frame_ms, maxptime=candidate.max_frame_ms, minimum=candidate.min_frame_ms))
        if transmit is None:
            trace.append(f"T3 {candidate.encoding}/{candidate.sample_rate}: no feasible packet time")
            continue
        if directional:
            receive_from = min(peer_sends, key=lambda fmt: (_rank(policy.receive_rank, fmt), offered.index(fmt)))
        else:
            receive_from = candidate
        asked = _packet_ms(receive_from, policy, ReceiverBounds())
        if asked is None:
            trace.append(f"R1 {receive_from.encoding}/{receive_from.sample_rate}: nothing receivable")
            continue
        trace.append(f"S2 {candidate.encoding}/{candidate.sample_rate}/{candidate.channels}")
        trace.append(f"{transmit.rule} send {transmit.packet_ms} ms"
                     + (" below the stated minimum" if transmit.below_minimum else ""))
        trace.append(f"R1 ask {asked.packet_ms} ms")
        return LegContract(
            send=replace(candidate, frame_ms=transmit.packet_ms),
            receive=replace(receive_from, frame_ms=asked.packet_ms, min_frame_ms=0, max_frame_ms=0),
            dtmf=_dtmf(remote_sdp, candidate),
            trace=tuple(trace),
        )
    return Refusal("no offered codec has a feasible packet time", tuple(trace))


def _mapping(fmt: RtpPcmFormat) -> tuple[str, int, int]:
    return (fmt.encoding, fmt.sample_rate, fmt.channels)


def apply_answer(local_offer_sdp, answer_sdp, policy: LegPolicy, *, dahua: bool = False) -> LegContract | Refusal:
    """Apply a remote answer to the offer this side sent (S5, S6, P3)."""
    trace: list[str] = []
    local = {_mapping(fmt): fmt for fmt in offered_pcm_formats(local_offer_sdp, allow_dahua_pcm=dahua)}
    answered = [fmt for fmt in offered_pcm_formats(answer_sdp, allow_dahua_pcm=dahua)
                if _mapping(fmt) in local and _usable(fmt, policy, dahua=dahua)]
    if not answered:
        return Refusal("the answer lists no codec of the offer", tuple(trace))
    flows = parse_audio_flow_attributes(answer_sdp)
    peer_receives = [fmt for fmt in answered if flows.get(fmt.payload_type, "sendrecv") in {"recv", "sendrecv"}]
    peer_sends = [fmt for fmt in answered if flows.get(fmt.payload_type, "sendrecv") in {"send", "sendrecv"}]
    directional = bool(flows) and bool(peer_receives) and bool(peer_sends)
    if directional:
        send_candidates = sorted(peer_receives, key=lambda fmt: (_rank(policy.send_rank, fmt), answered.index(fmt)))
    else:
        # RFC 3264 6.1: the offerer should send with the first format of the answer.
        send_candidates = answered
    for candidate in send_candidates:
        transmit = _packet_ms(candidate, policy, ReceiverBounds(
            ptime=candidate.frame_ms, maxptime=candidate.max_frame_ms, minimum=candidate.min_frame_ms))
        if transmit is None:
            trace.append(f"T3 {candidate.encoding}/{candidate.sample_rate}: no feasible packet time")
            continue
        if directional:
            receive_from = min(peer_sends, key=lambda fmt: (_rank(policy.receive_rank, fmt), answered.index(fmt)))
        else:
            receive_from = candidate
        own = local[_mapping(receive_from)]
        # R1: what this side asked for in its own offer stands; the answer does not change it.
        asked = _packet_ms(own, policy, ReceiverBounds(ptime=own.frame_ms))
        if asked is None:
            continue
        trace.append(f"S5 {candidate.encoding}/{candidate.sample_rate}/{candidate.channels}")
        trace.append(f"{transmit.rule} send {transmit.packet_ms} ms; R1 receive {asked.packet_ms} ms")
        return LegContract(
            send=replace(candidate, frame_ms=transmit.packet_ms),
            # P3: packets arrive on the number of the offer, whatever number the answer used.
            receive=replace(own, frame_ms=asked.packet_ms, min_frame_ms=0, max_frame_ms=0),
            dtmf=_answered_dtmf(local_offer_sdp, answer_sdp, candidate),
            trace=tuple(trace),
            receive_accept=tuple(
                local[_mapping(fmt)] for fmt in (peer_sends if directional else answered)),
        )
    return Refusal("no answered codec has a feasible packet time", tuple(trace))


def _answered_dtmf(local_offer_sdp, answer_sdp, selected: RtpPcmFormat) -> RtpDtmfFormat | None:
    """P5 for an answer: a clock both descriptions list, the clock of the codec first."""
    offered = {item.sample_rate for item in offered_dtmf_formats(local_offer_sdp)}
    answered = [item for item in offered_dtmf_formats(answer_sdp) if item.sample_rate in offered]
    for clock in (selected.sample_rate, 8000):
        match = next((item for item in answered if item.sample_rate == clock), None)
        if match is not None:
            return match
    return None
