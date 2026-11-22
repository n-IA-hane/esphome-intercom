"""Leg negotiation: the single owner of the media contract of one SIP leg.

Implements the selection rules S1 to S4, R1 and P5 of docs/MEDIA_NEGOTIATION_CONTRACT.md on top
of the packet time and payload primitives. Pure functions over a parsed description: no sockets,
no ports, no dialog state. The result uses the wire value types the rest of the integration
already consumes, so switching an entry point to this engine changes nothing downstream.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from ..audio_capabilities import AudioCapabilities, HA_AUDIO_CAPABILITIES
from ..audio_format import AudioFormat, MAX_SAMPLE_RATE, PcmFormat
from ..codec_capabilities import codec_available
from ..sdp_audio import is_rtp_pcm_mappable, offered_dtmf_formats, offered_pcm_formats
from ..sdp_parser import parse_audio_flow_attributes, parse_sdp, supports_independent_audio_ptime
from ..sdp_types import RtpDtmfFormat, RtpPcmFormat
from .codecs import CODECS
from .packet_time import PacketTimes, ReceiverBounds, choose_transmit_packet_ms
from .payload_map import TELEPHONE_EVENT, RtpMapping

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
    codec_order: tuple[str, ...] = ()
    # None is a dynamic backend; a tuple is an explicit fixed local capability.
    send_capabilities: tuple[AudioFormat, ...] | None = None
    receive_capabilities: tuple[AudioFormat, ...] | None = None
    capabilities: AudioCapabilities | None = HA_AUDIO_CAPABILITIES


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
    # S5: feasible peer receive mappings, numbered as the peer declared them.
    send_accept: tuple[RtpPcmFormat, ...] = field(default=())


@dataclass(frozen=True, slots=True)
class Refusal:
    reason: str
    trace: tuple[str, ...] = field(default=())


def _geometry(fmt: RtpPcmFormat) -> Geometry:
    spec = CODECS[fmt.encoding]
    return (spec.decoded_rate or fmt.sample_rate, spec.decoded_channels or fmt.channels)


def _usable(fmt: RtpPcmFormat, policy: LegPolicy, *, dahua: bool, send=True, receive=True) -> bool:
    """S1: enabled by policy, with a working adapter, and a mapping the adapter implements."""
    name = fmt.encoding
    if name not in CODECS or (policy.encodings is not None and name not in policy.encodings):
        return False
    if CODECS[name].auxiliary:
        return False
    if not 1 <= fmt.sample_rate <= MAX_SAMPLE_RATE or fmt.channels not in (1, 2):
        return False
    if not send and not receive:
        return False
    if (send and not codec_available(name)) or (receive and not codec_available(name, receive=True)):
        return False
    if send and name == "G723":
        from ..g723_codec import g723_encoder_supports_fmtp
        if not g723_encoder_supports_fmtp(fmt.fmtp):
            return False
    if name == "OPUS":
        return (fmt.sample_rate, fmt.channels) == (48000, 2)
    if name == "G722":
        return (fmt.sample_rate, fmt.channels) == (8000, 1)
    if name in {"PCMA", "PCMU"}:
        return fmt.sample_rate == 8000
    if name == "PCM":
        return dahua and (fmt.sample_rate, fmt.channels) == (16000, 1)
    if CODECS[name].pyav_decoder:
        spec = CODECS[name]
        return (fmt.sample_rate, fmt.channels) == (
            spec.conventional_clock_rate or spec.static_clock_rate or spec.decoded_rate,
            spec.conventional_channels if spec.conventional_clock_rate else 1)
    return fmt.channels in (1, 2)


def _rank(order: tuple[Geometry, ...], fmt: RtpPcmFormat) -> int:
    geometry = _geometry(fmt)
    return order.index(geometry) if geometry in order else len(order)


def _fixed_times(fmt: RtpPcmFormat, formats: tuple[AudioFormat, ...] | None) -> PacketTimes | None:
    if formats is None:
        return None
    rate, channels = _geometry(fmt)
    pcm = ((PcmFormat.S24LE, PcmFormat.S24LE_IN_S32)
           if CODECS[fmt.encoding].decodes_to_24_bit else (PcmFormat.S16LE,))
    return PacketTimes(explicit=tuple(dict.fromkeys(
        item.frame_ms for item in formats
        if (item.sample_rate, item.channels) == (rate, channels) and item.pcm_format in pcm
    )))


def _choose_local_time(fmt: RtpPcmFormat, policy: LegPolicy, bounds: ReceiverBounds, *,
                       receive: bool = False, default_ms: float = 0):
    formats = policy.receive_capabilities if receive else policy.send_capabilities
    fixed = _fixed_times(fmt, formats)
    if fixed is not None and not fixed.explicit:
        return None
    spec = CODECS[fmt.encoding]
    choice = choose_transmit_packet_ms(
        spec, fmt.sample_rate, fmt.channels,
        producible=fixed if fixed is not None else (OPUS_FRAMES if fmt.encoding == "OPUS" else ANY_DURATION),
        default_ms=default_ms or spec.assumed_packet_ms or policy.default_packet_ms, receiver=bounds,
        budget=(policy.capabilities or HA_AUDIO_CAPABILITIES).receive_payload_bytes if receive else policy.budget,
    )
    if choice is not None and policy.capabilities is not None:
        if not policy.capabilities.supports_rtp(replace(fmt, frame_ms=choice.packet_ms), receive=receive):
            return None
    return choice


def _receivable_as_offered(fmt: RtpPcmFormat, policy: LegPolicy) -> bool:
    """Validate the receiver already advertised without replacing its packet time (R1)."""
    fixed = _fixed_times(fmt, policy.receive_capabilities)
    return ((fixed is None or fmt.frame_ms in fixed.explicit)
            and (not fmt.max_frame_ms or fmt.frame_ms <= fmt.max_frame_ms)
            and (policy.capabilities or HA_AUDIO_CAPABILITIES).supports_rtp(fmt, receive=True))


def _codec_rank(policy: LegPolicy, fmt: RtpPcmFormat) -> int:
    return policy.codec_order.index(fmt.encoding) if fmt.encoding in policy.codec_order else len(policy.codec_order)


def _same_payload(left: RtpPcmFormat, right: RtpPcmFormat) -> bool:
    return (left.payload_type, left.encoding, left.sample_rate, left.channels) == (
        right.payload_type, right.encoding, right.sample_rate, right.channels)


def _receive_maximum(fmt: RtpPcmFormat, policy: LegPolicy) -> float:
    maximum = (policy.capabilities or HA_AUDIO_CAPABILITIES).max_receive_packet_ms(fmt)
    fixed = _fixed_times(fmt, policy.receive_capabilities)
    if fixed is not None:
        return min(maximum, max(fixed.explicit, default=0))
    return maximum


def _local_receiver(fmt: RtpPcmFormat, packet_ms: float, policy: LegPolicy) -> RtpPcmFormat:
    """Describe this side's receiver for one mapping (R1, R2, P6).

    The packet time is the one this side asks for. The maximum is what the receiver really
    holds. Format parameters are this side's own: the peer's are never carried over, because in
    an answer they would tell the peer its own limits as if they were ours.
    """
    asked = replace(fmt, frame_ms=packet_ms, min_frame_ms=0, max_frame_ms=0, fmtp="")
    return replace(asked, max_frame_ms=_receive_maximum(asked, policy))


def _dtmf(remote_sdp, selected: RtpPcmFormat) -> RtpDtmfFormat | None:
    """P5: the telephone event clock of the selected codec, else 8000 Hz."""
    offered = offered_dtmf_formats(remote_sdp)
    for clock in (selected.sample_rate, 8000):
        match = next((item for item in offered if item.sample_rate == clock), None)
        if match is not None:
            return match
    return None


def receive_auxiliary_formats(remote_sdp, primary: RtpPcmFormat, policy: LegPolicy, *,
                              own_description=None) -> tuple[RtpPcmFormat, ...]:
    """Accept CN only from an enabled, compatible offer/answer intersection.

    The receive payload number belongs to this side's description when applying an
    answer. Merely seeing CN in a peer's SDP never enables the optional adapter.
    """
    clock = CODECS["CN"].static_clock_rate
    if (policy.encodings is None or "CN" not in policy.encodings
            or (primary.sample_rate, primary.channels) != (clock, 1)
            or not codec_available("CN", receive=True)
            or parse_sdp(remote_sdp)["direction"] not in {"sendrecv", "sendonly"}):
        return ()
    remote_flows = parse_audio_flow_attributes(remote_sdp)
    own_flows = parse_audio_flow_attributes(own_description) if own_description else {}
    own = None
    if own_description is not None:
        if parse_sdp(own_description)["direction"] not in {"sendrecv", "recvonly"}:
            return ()
        own = {_mapping(fmt): fmt for fmt in offered_pcm_formats(own_description, include_auxiliary=True)
               if own_flows.get(fmt.payload_type, "sendrecv") in {"recv", "sendrecv"}}
    accepted = []
    for fmt in offered_pcm_formats(remote_sdp, include_auxiliary=True):
        if (fmt.encoding != "CN" or (fmt.sample_rate, fmt.channels) != (clock, 1)
                or remote_flows.get(fmt.payload_type, "sendrecv") not in {"send", "sendrecv"}):
            continue
        if own is not None:
            fmt = own.get(_mapping(fmt))
            if fmt is None:
                continue
        accepted.append(replace(fmt, frame_ms=primary.frame_ms, min_frame_ms=0, max_frame_ms=0, fmtp=""))
    return tuple(dict.fromkeys(accepted))


def answer_offer(
    remote_sdp, policy: LegPolicy, *, dahua: bool = False, established: LegContract | None = None,
) -> LegContract | Refusal:
    """Answer a remote audio offer.

    With ``established`` this is a re-offer inside a dialog (rule N1): the mapping in use is kept
    for each direction while the new offer still lists it on the same payload number. Its packet
    time is kept too unless the new offer states another one or no longer allows it, so new
    limits of the peer always reach the sender.
    """
    trace: list[str] = []
    remote_direction = parse_sdp(remote_sdp)["direction"]
    needs_send = remote_direction != "sendonly"
    needs_receive = remote_direction != "recvonly"
    flows = parse_audio_flow_attributes(remote_sdp)
    offered = [fmt for fmt in offered_pcm_formats(remote_sdp, allow_dahua_pcm=dahua)
               if _usable(fmt, policy, dahua=dahua,
                   send=needs_send and flows.get(fmt.payload_type, "sendrecv") in {"recv", "sendrecv"},
                   receive=needs_receive and flows.get(fmt.payload_type, "sendrecv") in {"send", "sendrecv"})]
    if not offered:
        return Refusal("no offered codec is enabled and available", tuple(trace))
    peer_receives = [fmt for fmt in offered if flows.get(fmt.payload_type, "sendrecv") in {"recv", "sendrecv"}]
    peer_sends = [fmt for fmt in offered if flows.get(fmt.payload_type, "sendrecv") in {"send", "sendrecv"}]
    directional = bool(flows) and bool(peer_receives) and bool(peer_sends)
    def kept(fmt: RtpPcmFormat, side: RtpPcmFormat | None) -> bool:
        return side is not None and _same_payload(fmt, side)

    in_use_send = established.send if established else None
    in_use_receive = established.receive if established else None
    candidates = peer_receives if directional and needs_send else peer_sends if directional else offered
    send_candidates = sorted(candidates, key=lambda fmt: (
        not kept(fmt, in_use_send), _codec_rank(policy, fmt),
        _rank(policy.send_rank if needs_send else policy.receive_rank, fmt), offered.index(fmt)))
    for candidate in send_candidates:
        retained = kept(candidate, in_use_send)
        transmit = _choose_local_time(candidate, policy, ReceiverBounds(
            ptime=candidate.frame_ms, maxptime=candidate.max_frame_ms, minimum=candidate.min_frame_ms),
            default_ms=in_use_send.frame_ms if retained else 0, receive=not needs_send)
        if transmit is None:
            trace.append(f"S1/T3 {candidate.encoding}/{candidate.sample_rate}: no producible format within the receiver bounds")
            continue
        if retained:
            trace.append("N1 kept the mapping in use to send")
        if directional and needs_receive:
            receive_candidates = sorted(peer_sends, key=lambda fmt: (
                not kept(fmt, in_use_receive), _codec_rank(policy, fmt),
                _rank(policy.receive_rank, fmt), offered.index(fmt)))
        else:
            receive_candidates = [candidate]
        legacy_time = transmit.packet_ms if directional and not supports_independent_audio_ptime(remote_sdp) else 0
        for receive_from in receive_candidates:
            asked = _choose_local_time(receive_from, policy, ReceiverBounds(ptime=legacy_time),
                receive=needs_receive,
                default_ms=in_use_receive.frame_ms if kept(receive_from, in_use_receive) else 0)
            if asked is None:
                trace.append(f"R1 {receive_from.encoding}/{receive_from.sample_rate}: nothing receivable")
                continue
            if legacy_time and asked.packet_ms != legacy_time:
                trace.append("R3 legacy directional peer has no common packet time")
                continue
            trace.append(f"S2 {candidate.encoding}/{candidate.sample_rate}/{candidate.channels}")
            trace.append(f"{transmit.rule} send {transmit.packet_ms} ms"
                         + (" below the stated minimum" if transmit.below_minimum else ""))
            trace.append(f"R1 ask {asked.packet_ms} ms")
            receiver = _local_receiver(receive_from, asked.packet_ms, policy)
            auxiliary = receive_auxiliary_formats(remote_sdp, receiver, policy)
            return LegContract(
                send=replace(candidate, frame_ms=transmit.packet_ms),
                receive=receiver,
                receive_accept=(receiver, *auxiliary) if auxiliary else (),
                dtmf=_dtmf(remote_sdp, candidate),
                trace=tuple(trace),
            )
    return Refusal("no offered codec has a feasible packet time", tuple(trace))


def _mapping(fmt: RtpPcmFormat) -> tuple[str, int, int]:
    return (fmt.encoding, fmt.sample_rate, fmt.channels)


def _preferred_packet_ms(fmt: RtpPcmFormat, preferred) -> float:
    times = _fixed_times(fmt, tuple(preferred))
    return times.explicit[0] if times is not None and times.explicit else 0


def _preferred_receiver(fmt: RtpPcmFormat, policy: LegPolicy, preferred):
    """Rank and validate the caller's full receive preference, including PCM width."""
    for index, pcm in enumerate(preferred):
        if not _fixed_times(fmt, (pcm,)).explicit:
            continue
        # Without receiver facts, the compatibility API only declared mappable linear presets.
        if policy.capabilities is None and fmt.encoding in {"L16", "L24"} and not is_rtp_pcm_mappable(pcm):
            continue
        receiver = _local_receiver(fmt, pcm.frame_ms, policy)
        if _receivable_as_offered(receiver, policy):
            return index, receiver
    return len(preferred), None


def apply_answer(local_offer_sdp, answer_sdp, policy: LegPolicy, *, dahua: bool = False,
                 format_direction: str | None = None, send_preferred=(), receive_preferred=()) -> LegContract | Refusal:
    """Apply an answer to its offer, or to local capabilities when no offer was supplied.

    The compatibility API without an offer preserves answer order and one common mapping.
    It cannot establish an offered payload history or an advertised receiver that is absent.
    """
    trace: list[str] = []
    offered = (offered_pcm_formats(local_offer_sdp, allow_dahua_pcm=dahua)
               if local_offer_sdp is not None else None)
    local = {_mapping(fmt): fmt for fmt in offered} if offered is not None else None
    local_flows = parse_audio_flow_attributes(local_offer_sdp) if local is not None else {}
    local_send = {_mapping(fmt): fmt for fmt in offered or ()
                  if local_flows.get(fmt.payload_type, "sendrecv") in {"send", "sendrecv"}}
    local_receive = {_mapping(fmt): fmt for fmt in offered or ()
                     if local_flows.get(fmt.payload_type, "sendrecv") in {"recv", "sendrecv"}}
    remote_direction = parse_sdp(answer_sdp)["direction"]
    direction = format_direction or {"sendonly": "recvonly", "recvonly": "sendonly"}.get(remote_direction, "sendrecv")
    needs_send = direction != "recvonly"
    needs_receive = direction != "sendonly"
    flows = parse_audio_flow_attributes(answer_sdp) if local is not None else {}

    def permits(fmt: RtpPcmFormat, *, sending: bool) -> bool:
        peer_roles = {"recv", "sendrecv"} if sending else {"send", "sendrecv"}
        own_mappings = local_send if sending else local_receive
        return (flows.get(fmt.payload_type, "sendrecv") in peer_roles
                and (local is None or _mapping(fmt) in own_mappings))

    answered = [fmt for fmt in offered_pcm_formats(answer_sdp, allow_dahua_pcm=dahua)
                if (local is None or _mapping(fmt) in local) and _usable(fmt, policy, dahua=dahua,
                    send=needs_send and permits(fmt, sending=True),
                    receive=needs_receive and permits(fmt, sending=False))]
    if not answered:
        return Refusal("the answer lists no enabled and available codec" if local is None
                       else "the answer lists no codec of the offer", tuple(trace))
    if local is None:
        send_preferred = send_preferred or policy.send_capabilities or ()
        receive_preferred = receive_preferred or policy.receive_capabilities or ()
        if policy.capabilities is not None:
            send_preferred = policy.capabilities.expand(list(send_preferred), answered)
            receive_preferred = policy.capabilities.expand(list(receive_preferred), answered, receive=True)
    peer_receives = [fmt for fmt in answered if permits(fmt, sending=True)]
    peer_sends = [fmt for fmt in answered if permits(fmt, sending=False)]
    if (needs_send and not peer_receives) or (needs_receive and not peer_sends):
        return Refusal("the answer flow attributes omit a required audio direction", tuple(trace))
    directional = bool(flows or local_flows) and bool(peer_receives) and bool(peer_sends)
    if local is None and not needs_send:
        send_candidates = sorted(answered, key=lambda fmt: (
            _preferred_receiver(fmt, policy, receive_preferred)[0], answered.index(fmt)))
    elif directional:
        send_candidates = sorted(peer_receives, key=lambda fmt: (
            _codec_rank(policy, fmt), _rank(policy.send_rank, fmt), answered.index(fmt)))
    else:
        # RFC 3264 6.1: the offerer should send with the first format of the answer.
        send_candidates = answered
    receive_mappings = {_mapping(item) for item in (peer_sends if directional else answered)}
    for candidate in send_candidates:
        transmit = _choose_local_time(candidate, policy, ReceiverBounds(
            ptime=candidate.frame_ms, maxptime=candidate.max_frame_ms, minimum=candidate.min_frame_ms),
            receive=not needs_send,
            default_ms=_preferred_packet_ms(candidate, send_preferred) if local is None else 0
        ) if local is not None or needs_send else None
        if transmit is None and (local is not None or needs_send):
            trace.append(f"T3 {candidate.encoding}/{candidate.sample_rate}: no feasible packet time")
            continue
        if directional:
            receive_candidates = sorted(peer_sends, key=lambda fmt: (
                _codec_rank(policy, fmt), _rank(policy.receive_rank, fmt), answered.index(fmt)))
        else:
            receive_candidates = [candidate]
        for receive_from in receive_candidates:
            if local is None:
                if needs_receive:
                    _ranked, receiver = _preferred_receiver(receive_from, policy, receive_preferred)
                    if receiver is None:
                        continue
                    if policy.capabilities is None:
                        receiver = replace(receiver, max_frame_ms=0)
                else:
                    receiver = replace(candidate, frame_ms=transmit.packet_ms)
            else:
                own = (local_receive if needs_receive else local)[_mapping(receive_from)]
                # R1: the receiver already advertised stands; the answer does not change it.
                if needs_receive:
                    if not _receivable_as_offered(own, policy):
                        continue
                    receive_ms = own.frame_ms
                else:
                    asked = _choose_local_time(own, policy, ReceiverBounds(ptime=own.frame_ms))
                    if asked is None:
                        continue
                    receive_ms = asked.packet_ms
                receiver = replace(own, frame_ms=receive_ms,
                    max_frame_ms=own.max_frame_ms or _receive_maximum(own, policy))
            sending = replace(candidate, frame_ms=transmit.packet_ms) if transmit is not None else receiver
            trace.append(f"{'S5' if local is not None else 'S1 capability-only'} "
                         f"{candidate.encoding}/{candidate.sample_rate}/{candidate.channels}")
            if transmit is not None:
                trace.append(f"{transmit.rule} send {sending.frame_ms} ms")
            trace.append(f"R1 {'offered' if local is not None else 'local'} receive {receiver.frame_ms} ms")
            if local is None:
                send_accept = ()
                receive_accept = (receiver,) if needs_receive else ()
            else:
                send_accept = tuple(
                    replace(fmt, frame_ms=sent.packet_ms)
                    for fmt in peer_receives
                    if needs_send and (sent := _choose_local_time(fmt, policy,
                        ReceiverBounds(ptime=fmt.frame_ms, maxptime=fmt.max_frame_ms,
                                       minimum=fmt.min_frame_ms))) is not None)
                receive_accept = tuple(
                    replace(fmt, max_frame_ms=fmt.max_frame_ms or _receive_maximum(fmt, policy))
                    for mapping, fmt in local_receive.items()
                    if mapping in receive_mappings and _receivable_as_offered(fmt, policy)
                ) + receive_auxiliary_formats(answer_sdp, receiver, policy, own_description=local_offer_sdp)
            return LegContract(
                send=sending,
                receive=receiver,
                dtmf=_answered_dtmf(local_offer_sdp, answer_sdp, candidate) if local is not None else None,
                trace=tuple(trace),
                send_accept=send_accept,
                # P3: exact answers retain the offer's PT; capability-only callers have no history.
                receive_accept=receive_accept,
            )
    return Refusal("no answered codec has a feasible packet time", tuple(trace))


def _answered_dtmf(local_offer_sdp, answer_sdp, selected: RtpPcmFormat) -> RtpDtmfFormat | None:
    """P5 for an answer: a clock both descriptions list, the clock of the codec first."""
    offered = {item.sample_rate: item for item in offered_dtmf_formats(local_offer_sdp)}
    answered = [replace(item, events=item.events & offered[item.sample_rate].events)
                for item in offered_dtmf_formats(answer_sdp) if item.sample_rate in offered]
    for clock in (selected.sample_rate, 8000):
        match = next((item for item in answered if item.sample_rate == clock and item.events), None)
        if match is not None:
            return match
    return None


@dataclass(frozen=True, slots=True)
class OfferPlan:
    """What a local offer lists, before it is written: numbered mappings in order, the packet
    time this side asks for, and why."""

    payloads: tuple[tuple[int, RtpMapping], ...]
    packet_ms: float
    trace: tuple[str, ...] = field(default=())
    formats: tuple[RtpPcmFormat, ...] = ()
    send_formats: tuple[RtpPcmFormat, ...] = ()
    receive_formats: tuple[RtpPcmFormat, ...] = ()
    events: tuple[RtpDtmfFormat, ...] = ()
    flows: tuple[tuple[int, str], ...] = ()
    maximum_ms: float = 0

    @property
    def audio(self) -> tuple[RtpMapping, ...]:
        return tuple(mapping for _, mapping in self.payloads if mapping[0] != TELEPHONE_EVENT)
