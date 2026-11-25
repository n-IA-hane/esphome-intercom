"""Public media negotiation API, independent of calls, sockets and Home Assistant.

The SIP adapters supply local capabilities and the exact descriptions exchanged.
This module owns no registrations, tasks or media resources.
"""

from __future__ import annotations

from dataclasses import replace

from ..sdp_parser import (
    local_direction_for_offer, parse_sdp, fmtp_parameters,
    format_direction_for_remote, normalize_direction,
)
from ..sdp_types import RtpPcmDirection
from ..sdp_types import SdpError
from ..sdp_audio_facts import offered_pcm_formats, offered_dtmf_formats
from ..codec_policy import DEFAULT_AUDIO_CODECS
from ..audio_format import PcmFormat
from . import leg
from .capability import PeerEvidence, MediaCompatibility
from .payload_map import PayloadMap, PayloadConflict, TELEPHONE_EVENT
from .codecs import CODECS, DEFAULT_OPUS_RECEIVE_FMTP

LegContract = leg.LegContract
Refusal = leg.Refusal
OfferPlan = leg.OfferPlan
negotiate_dtmf = leg.negotiate_dtmf


def answer_audio_projection(audio, *, direction, remote_sdp=None):
    """Serialize the chosen mappings and local receiver envelope without negotiating.

    RFC 3264 section 6.1 constructs an inactive answer's format list from the
    offer direction. Peer receiver bounds belong to local TX, never to the
    local receiver's advertised maxptime.
    """
    remote_direction = parse_sdp(remote_sdp)['direction'] if remote_sdp else 'sendrecv'
    format_direction = format_direction_for_remote(remote_direction, normalize_direction(direction))
    send, recv = audio.send, audio.recv
    def key(fmt):
        return fmt.payload_type, fmt.encoding, fmt.sample_rate, fmt.channels
    if format_direction == 'sendonly':
        selected = [send]
        alternatives = audio.send_formats
    elif format_direction == 'recvonly':
        selected = [recv]
        alternatives = audio.recv_formats
    else:
        selected = [recv] if key(send) == key(recv) else [recv, send]
        alternatives = audio.recv_formats
    for fmt in alternatives:
        if fmt.encoding != 'CN' and all(fmt.payload_type != item.payload_type for item in selected):
            selected.append(fmt)
    if format_direction in {'recvonly', 'sendrecv'}:
        if (recv.sample_rate, recv.channels) == (CODECS['CN'].static_clock_rate, 1):
            offered = {key(fmt) for fmt in offered_pcm_formats(remote_sdp, include_auxiliary=True)} if remote_sdp else None
            for fmt in audio.recv_formats:
                if (fmt.encoding == 'CN' and (fmt.sample_rate, fmt.channels) == (recv.sample_rate, 1)
                        and (offered is None or key(fmt) in offered)
                        and all(fmt.payload_type != item.payload_type for item in selected)):
                    selected.append(fmt)
        receivers = {fmt.payload_type: fmt for fmt in audio.recv_formats}
        receivers[recv.payload_type] = recv
        selected = [receivers.get(fmt.payload_type,
            replace(fmt, fmtp=DEFAULT_OPUS_RECEIVE_FMTP) if fmt.encoding == 'OPUS' else fmt)
            for fmt in selected]
        bounds = [fmt.max_frame_ms for fmt in (audio.recv_formats or (recv,)) if fmt.max_frame_ms]
        maximum = min(bounds) if bounds else max(recv.frame_ms, recv.max_frame_ms)
        packet_ms = recv.frame_ms
    else:
        # No peer media is received on a sendonly stream. Retain the existing
        # harmless timing attributes, without treating them as a TX promise.
        packet_ms = maximum = send.frame_ms
    return tuple(selected), packet_ms, maximum


def validate_answer_contract(audio, answer_sdp, *, events=None, compatibility=MediaCompatibility(), remote_sdp=None):
    """Validate serialization of an already chosen answer without selecting again."""
    section = parse_sdp(answer_sdp)
    remote_direction = parse_sdp(remote_sdp)['direction'] if remote_sdp else 'sendrecv'
    format_direction = format_direction_for_remote(remote_direction, section['direction'])
    expected = (audio.send_formats or (audio.send,)) if format_direction == "sendonly" else (
        audio.recv_formats or (audio.recv,))
    if section["direction"] == "inactive" and format_direction == 'sendrecv':
        expected = tuple(dict.fromkeys((*expected, *(audio.send_formats or (audio.send,)))))
    def key(fmt):
        return fmt.payload_type, fmt.encoding, fmt.sample_rate, fmt.channels
    actual = offered_pcm_formats(answer_sdp, optional_mappings=compatibility.optional_mappings, include_auxiliary=True)
    projected, packet_ms, maximum = answer_audio_projection(audio,
        direction=section['direction'], remote_sdp=remote_sdp)
    if {key(fmt) for fmt in projected} != {key(fmt) for fmt in expected}:
        raise SdpError('answer projection differs from the chosen directional media contract')
    advertised_events = offered_dtmf_formats(answer_sdp)
    if set(section["payload_order"]) != {fmt.payload_type for fmt in (*actual, *advertised_events)}:
        raise SdpError("answer advertised a mapping absent from the media backend")
    if {key(fmt) for fmt in actual} != {key(fmt) for fmt in expected}:
        raise SdpError("answer changed accepted mappings without the matching media contract")
    receivers = {key(fmt): fmt for fmt in projected}
    for fmt in actual:
        if fmt.encoding == "OPUS" and fmtp_parameters(fmt.fmtp) != fmtp_parameters(
                receivers[key(fmt)].fmtp or DEFAULT_OPUS_RECEIVE_FMTP):
            raise SdpError("answer changed the chosen Opus receiver parameters")
    if format_direction != "sendonly" and section["ptime"] and section["ptime"] != packet_ms:
        raise SdpError("answer changed receive packet time without the matching media contract")
    expected_events = ((events.recv_formats or ((events.recv,) if events.recv is not None else ()))
                       if events is not None else ())
    if set(advertised_events) != set(expected_events):
        raise SdpError("answer changed telephone-event mappings without the matching media contract")
    bounds = [fmt.max_frame_ms for fmt in (audio.recv_formats or (audio.recv,)) if fmt.max_frame_ms]
    if format_direction != "sendonly" and bounds and section["maxptime"] != maximum:
        raise SdpError("answer changed the chosen receiver envelope")


def policy_for_leg(send_formats, receive_formats, *, codec_policy=None, capabilities=None,
                   compatibility=MediaCompatibility()) -> leg.LegPolicy:
    """Convert explicit preferences and capabilities into the leg engine's input."""
    send = tuple(send_formats)
    receive = tuple(receive_formats)
    enabled = getattr(codec_policy, "audio", None)
    allowed = DEFAULT_AUDIO_CODECS if enabled is None else enabled
    widths = {PcmFormat.S16LE: 16, PcmFormat.S24LE: 24,
              PcmFormat.S24LE_IN_S32: 24, PcmFormat.S32LE: 32}
    return leg.LegPolicy(
        send_rank=tuple(dict.fromkeys((item.sample_rate, item.channels, widths[item.pcm_format]) for item in send)),
        receive_rank=tuple(dict.fromkeys((item.sample_rate, item.channels, widths[item.pcm_format]) for item in receive)),
        budget=getattr(capabilities, "transmit_payload_bytes", 1200),
        encodings=frozenset(allowed),
        codec_order=tuple(enabled) if enabled is not None and getattr(codec_policy, "audio_ordered", True) else (),
        codec_policy_explicit=bool(getattr(codec_policy, "audio_configured", enabled is not None)),
        send_capabilities=send if capabilities is None else None,
        receive_capabilities=receive if capabilities is None else None,
        capabilities=capabilities, compatibility=compatibility,
    )


def answer_offer(remote_sdp, send_formats, receive_formats, *, codec_policy=None,
                 capabilities=None, compatibility=MediaCompatibility(), established: LegContract | None = None,
                 receive_capabilities=None, previous_payloads=None):
    """Choose one local answer from actual local capabilities and a remote offer."""
    policy = policy_for_leg(send_formats, receive_formats, codec_policy=codec_policy, capabilities=capabilities, compatibility=compatibility)
    if receive_capabilities is not None:
        policy = replace(policy, receive_capabilities=tuple(receive_capabilities))
    return _remember_payloads(leg.answer_offer(remote_sdp, policy, established=established), previous_payloads)


def apply_answer(local_offer_sdp, remote_sdp, send_formats, receive_formats, *,
                 codec_policy=None, capabilities=None, compatibility=MediaCompatibility(), format_direction=None,
                 established: LegContract | None = None, previous_payloads=None):
    """Apply an answer to its exact offer, or to supplied capabilities for compatibility."""
    send_formats, receive_formats = tuple(send_formats), tuple(receive_formats)
    return _remember_payloads(leg.apply_answer(local_offer_sdp, remote_sdp,
        policy_for_leg(send_formats, receive_formats, codec_policy=codec_policy, capabilities=capabilities, compatibility=compatibility),
        format_direction=format_direction,
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
                 capabilities=None, previous: PayloadMap | None = None,
                 compatibility=MediaCompatibility(), **options) -> OfferPlan:
    """Plan a complete offer, or reduce it from complete binding evidence."""
    policy = policy_for_leg(send_formats, receive_formats, codec_policy=codec_policy, capabilities=capabilities, compatibility=compatibility)
    from .offers import create_complete_offer
    return create_complete_offer(policy, send_formats, receive_formats, capabilities=capabilities,
                                 previous=previous, codec_policy=codec_policy, evidence=evidence, **options)


def sip_offer_plan(evidence=None, *, codec_policy=None, compatibility=MediaCompatibility(),
                   symmetric_preferences=False, match_received_codec=False):
    """Plan a SIP offer from normalized preferences over the backend capabilities."""
    from ..audio_capabilities import HA_AUDIO_CAPABILITIES
    from ..audio_format import HA_SIP_PCM_TX_FORMATS, HA_SIP_PCM_RX_FORMATS, HA_TRUNK_AUDIO_FORMATS

    send = HA_TRUNK_AUDIO_FORMATS if symmetric_preferences else HA_SIP_PCM_TX_FORMATS
    receive = HA_TRUNK_AUDIO_FORMATS if symmetric_preferences else HA_SIP_PCM_RX_FORMATS
    plan = create_offer(send, receive, evidence, codec_policy=codec_policy,
        capabilities=HA_AUDIO_CAPABILITIES, common=True, compatibility=compatibility)
    return replace(plan, match_received_codec=match_received_codec)


def browser_media_contract():
    """Describe the browser loopback through the ordinary media planner."""
    from ..audio_capabilities import HA_AUDIO_CAPABILITIES
    from ..audio_format import HA_SIP_PCM_TX_FORMATS
    from ..codec_policy import CodecPolicy

    plan = create_offer(HA_SIP_PCM_TX_FORMATS, HA_SIP_PCM_TX_FORMATS,
        codec_policy=CodecPolicy(audio=("L16",)), capabilities=HA_AUDIO_CAPABILITIES)
    fmt = plan.send_formats[0]
    receive = next(value for value in plan.receive_formats if
        (value.payload_type, value.encoding, value.sample_rate, value.channels) ==
        (fmt.payload_type, fmt.encoding, fmt.sample_rate, fmt.channels))
    from ..sdp_types import RtpDtmfDirection
    primary = next(event for event in plan.events if event.sample_rate == fmt.sample_rate)
    events = RtpDtmfDirection(primary, primary, plan.events, plan.events)
    audio = RtpPcmDirection(fmt, receive, (receive,), (fmt,), dtmf_direction=events)
    return replace(audio, payloads=payload_history(PayloadMap(), audio, events))


def browser_rtp_format():
    """Expose the selected browser mapping to existing local adapters."""
    return browser_media_contract().send


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
        if direction in {"sendonly", "sendrecv"} else (), payloads=result.payloads,
        dtmf_direction=result.dtmf_direction)
