"""Pure bridge planning over the media contracts of two independent SIP legs."""

from dataclasses import dataclass, replace
from enum import StrEnum

from .. import sdp
from ..audio_capabilities import AudioCapabilities, HA_AUDIO_CAPABILITIES
from ..audio_format import AudioFormat, PcmFormat, MAX_FRAME_MS
from ..codec_policy import CodecPolicy
from ..sdp_types import RtpPcmDirection, RtpPcmFormat
from . import leg, negotiator
from .codecs import CODECS
from .capability import PeerEvidence


class AudioOperation(StrEnum):
    PASSTHROUGH = "passthrough"
    REPACKETIZE = "repacketize"
    RESAMPLE = "resample"
    TRANSCODE = "transcode"


@dataclass(frozen=True, slots=True)
class AudioFlowPlan:
    source: RtpPcmFormat
    target: RtpPcmFormat
    operations: tuple[AudioOperation, ...]
    reason: str

    @property
    def payload_relay(self) -> bool:
        return self.operations == (AudioOperation.PASSTHROUGH,)


def _opus_limits(fmtp: str) -> dict[str, int]:
    from ..opus_codec import _receive_limits

    return _receive_limits(fmtp)


def payload_relay_compatible(source: RtpPcmFormat, target: RtpPcmFormat, *, payload_size=None) -> bool:
    """Decide encoded relay using directional receiver envelopes, not fmtp equality."""
    from .. import rtp

    if (source.encoding, source.sample_rate, source.channels) != (target.encoding, target.sample_rate, target.channels):
        return False
    if source.encoding == "OPUS":
        incoming, outgoing = _opus_limits(source.fmtp), _opus_limits(target.fmtp)
        if any(name in outgoing and incoming.get(name, default) > outgoing[name]
               for name, default in (("maxaveragebitrate", 510000), ("maxplaybackrate", 48000))):
            return False
        # ptime is a preference. Actual packets are validated against both
        # maxptime bounds before transmission, independently of byte count.
        return max(source.frame_ms, source.max_frame_ms or 120) <= (target.max_frame_ms or 120)
    if sdp.fmtp_parameters(source.fmtp) != sdp.fmtp_parameters(target.fmtp):
        return False
    if (source.frame_ms == target.frame_ms
            and max(source.frame_ms, source.max_frame_ms) <= max(target.frame_ms, target.max_frame_ms)
            and rtp.audio_payload_size_limit(source) <= rtp.MAX_RTP_PAYLOAD_BYTES):
        return True
    return bool(payload_size is not None and source.encoding in {"L16", "L24", "PCM", "PCMA", "PCMU"}
        and payload_size == target.rtp_timestamp_step * target.channels * target.bits // 8
        and payload_size <= min(rtp.audio_payload_size_limit(source), rtp.audio_payload_size_limit(target),
                                rtp.MAX_RTP_PAYLOAD_BYTES))


@dataclass(frozen=True, slots=True)
class BridgeAudioPlan:
    caller: RtpPcmDirection
    caller_to_callee: AudioFlowPlan
    callee_to_caller: AudioFlowPlan
    trace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BrowserAudioPlan:
    microphone: AudioFormat
    speaker: AudioFormat
    max_message_bytes: int


def plan_browser_audio(send, receive) -> BrowserAudioPlan:
    """Keep the real browser PCM edge at 48 kHz independently of the SIP codec."""
    def edge(wire):
        pcm = wire.audio_format
        try:
            return replace(pcm, sample_rate=48000)
        except ValueError:
            # A legal fractional peer quantum need not produce whole samples
            # at the browser's fixed clock. Its packetizer retains that quantum.
            return replace(pcm, sample_rate=48000, frame_ms=20)

    return BrowserAudioPlan(edge(send), edge(receive),
        48000 * 2 * 4 * MAX_FRAME_MS // 1000 + 1)


def plan_bridge_offer(send_formats, receive_formats, source, *, send_rtp_formats=None,
                      receive_rtp_formats=None, evidence: PeerEvidence | None = None,
                      codec_policy=None, common=True, dahua=False, match_received_codec=False):
    """Rank a relay opportunity inside one destination's real capability plan."""
    plan = negotiator.create_offer(send_formats, receive_formats, evidence,
        codec_policy=codec_policy, capabilities=HA_AUDIO_CAPABILITIES,
        send_rtp_formats=send_rtp_formats, recv_rtp_formats=receive_rtp_formats,
        common=common, dahua=dahua)
    plan = replace(plan, match_received_codec=match_received_codec)
    if not plan.send_formats or (codec_policy is not None and codec_policy.audio_ordered):
        return plan
    source_key = (source.encoding, source.sample_rate, source.channels)
    preferred = _pcm_shape(plan.send_formats[0])
    sends = tuple(sorted(plan.send_formats, key=lambda fmt:
        (_pcm_shape(fmt) != preferred,
         (fmt.encoding, fmt.sample_rate, fmt.channels) != source_key)))
    return replace(plan, send_formats=sends)


def _pcm_shape(fmt):
    # Geometry does not require a duration. Constructing an AudioFormat here
    # would reject a fractional-sample packet that is only being compared.
    spec = CODECS.get(fmt.encoding)
    if spec is None:
        return fmt.sample_rate, PcmFormat.S24LE, fmt.channels
    return (spec.decoded_rate or fmt.sample_rate,
            PcmFormat.S24LE if spec.decodes_to_24_bit else PcmFormat.S16LE,
            spec.decoded_channels or fmt.channels)


def plan_audio_flow(source: RtpPcmFormat, target: RtpPcmFormat) -> AudioFlowPlan:
    """Describe format adaptation; the RTP owner still validates real packets."""
    same_mapping = (source.encoding, source.sample_rate, source.channels) == (
        target.encoding, target.sample_rate, target.channels)
    same_parameters = sdp.fmtp_parameters(source.fmtp) == sdp.fmtp_parameters(target.fmtp)
    same_pcm = _pcm_shape(source) == _pcm_shape(target)
    if payload_relay_compatible(source, target):
        return AudioFlowPlan(source, target, (AudioOperation.PASSTHROUGH,), "encoded packets fit the directional receiver envelope")
    if same_mapping and same_parameters:
        if source.encoding != "OPUS" and source.frame_ms != target.frame_ms:
            return AudioFlowPlan(source, target, (AudioOperation.REPACKETIZE,), "same mapping with a different packet time")
    if source.encoding in {"L16", "L24", "PCM"} and target.encoding in {"L16", "L24", "PCM"}:
        operations = (AudioOperation.RESAMPLE,)
        reason = "convert decoded PCM geometry while preserving each leg's rate"
    else:
        operations = (AudioOperation.TRANSCODE,)
        reason = "decode and encode the independently negotiated codec contracts"
        if not same_pcm:
            operations = (AudioOperation.RESAMPLE, *operations)
    if source.frame_ms != target.frame_ms:
        operations = (*operations, AudioOperation.REPACKETIZE)
    return AudioFlowPlan(source, target, operations, reason)


def plan_caller_audio(remote_sdp: bytes, caller: RtpPcmDirection, callee: RtpPcmDirection, *,
                      frozen: bool = False, codec_policy: CodecPolicy = CodecPolicy(),
                      send_capabilities: tuple[AudioFormat, ...] = (),
                      receive_capabilities: tuple[AudioFormat, ...] = (),
                      capabilities: AudioCapabilities | None = None, dahua: bool = False) -> BridgeAudioPlan:
    """B2/B3: refine a free caller with the existing leg negotiator, or retain it.

    ``frozen`` means its description was actually transmitted. A queued offer
    or a bodyless provisional response does not freeze the caller's contract.
    Capabilities constrain candidates; the callee never grants a new codec.
    """
    trace = []

    def result(selected):
        return BridgeAudioPlan(selected, plan_audio_flow(selected.recv, callee.send),
                               plan_audio_flow(callee.recv, selected.send), tuple(trace))

    if frozen:
        trace.append("B3 caller description was transmitted; preserve its contract")
        return result(caller)
    preferred = callee.recv.audio_format
    send_formats = [preferred, caller.send.audio_format]
    if send_capabilities:
        # These bounds apply to a bridge optimization, not to admission of
        # the caller's already valid contract. Try every declared alternative
        # if the callee's preferred geometry cannot meet the receiver bounds.
        send_formats = [fmt for fmt in dict.fromkeys((preferred, *send_capabilities))
            if fmt in send_capabilities]
    recv_formats = (callee.send.audio_format, caller.recv.audio_format)
    if _pcm_shape(callee.send) == _pcm_shape(caller.recv):
        recv_formats = recv_formats[::-1]
    policy = negotiator.policy_for_leg(list(dict.fromkeys(send_formats)),
        list(dict.fromkeys(recv_formats)), codec_policy=codec_policy,
        capabilities=(capabilities if capabilities is not None else
                      None if send_capabilities else HA_AUDIO_CAPABILITIES))
    if send_capabilities:
        # B2 searches only configured TX alternatives; admission stays dynamic.
        policy = replace(policy, send_capabilities=tuple(send_formats))
    if capabilities is None and receive_capabilities:
        policy = replace(policy, receive_capabilities=tuple(receive_capabilities))
    document = sdp.SdpDocument.parse(remote_sdp)
    selected = negotiator.as_direction(leg.answer_offer(document, policy, dahua=dahua), document)
    if selected is None:
        trace.append("B2 no feasible alternative inside the caller's offer and policy")
        return result(caller)

    def receive_mapping(fmt):
        return (fmt.payload_type, fmt.encoding, fmt.sample_rate, fmt.channels,
                tuple(sorted(sdp.fmtp_parameters(fmt.fmtp).items())))

    previous_receives = {receive_mapping(fmt): fmt for fmt in caller.recv_formats}
    previous_receives.setdefault(receive_mapping(caller.recv), caller.recv)
    can_retain_receive = (capabilities is None or (
        capabilities.supports_rtp(caller.recv, receive=True)
        and (not caller.recv.max_frame_ms
             or caller.recv.max_frame_ms <= capabilities.max_receive_packet_ms(caller.recv))))
    if (capabilities is not None
            and can_retain_receive
            and receive_mapping(selected.recv) == receive_mapping(caller.recv)
            and all(receive_mapping(fmt) in previous_receives for fmt in selected.recv_formats)):
        # R1: a bridge cannot retime an unchanged admitted receiver merely
        # because its candidate search uses the generic backend default.
        selected = replace(selected, recv=caller.recv, recv_formats=tuple(
            previous_receives[receive_mapping(fmt)] for fmt in selected.recv_formats))
        trace.append("B2 unchanged receive mappings; retain admitted timing and bounds")
    selected_receives = {receive_mapping(fmt): fmt for fmt in selected.recv_formats}
    if (can_retain_receive and caller.recv.encoding in {"L16", "L24", "PCM"}
            and selected.recv.encoding in {"L16", "L24", "PCM"}
            and receive_mapping(caller.recv) in selected_receives
            and all(not fmt.max_frame_ms or caller.recv.max_frame_ms <= fmt.max_frame_ms
                    for fmt in selected.recv_formats)):
        # Standard alternatives let the source keep transmitting its native
        # mapping. Changing our primary cannot promise to remove a conversion.
        selected = replace(selected, recv=caller.recv, recv_formats=tuple(dict.fromkeys((
            caller.recv, *(previous_receives.get(receive_mapping(fmt), fmt)
                           for fmt in selected.recv_formats)))))
        trace.append("B2 retain admitted linear receive preference; adapt the actual source mapping")
    if _pcm_shape(selected.recv) != _pcm_shape(caller.recv):
        original_conversions = sum((_pcm_shape(caller.recv) != _pcm_shape(callee.send),
                                    _pcm_shape(callee.recv) != _pcm_shape(caller.send)))
        candidate_conversions = sum((_pcm_shape(selected.recv) != _pcm_shape(callee.send),
                                     _pcm_shape(callee.recv) != _pcm_shape(selected.send)))
        if candidate_conversions >= original_conversions:
            trace.append("B2 retain caller receive geometry; moving a conversion does not save one")
            return result(caller)
    send = selected.send
    if send.encoding.upper() == caller.send.encoding.upper() == "L16" and send.sample_rate < caller.send.sample_rate:
        if caller.send.payload_type == caller.recv.payload_type:
            trace.append("B2 preserve the shared caller PCM mapping instead of lowering its rate")
            return result(caller)
        send = caller.send
        trace.append("B2 preserve the caller PCM receive rate and resample the bridge")
    else:
        trace.append("B2 free caller selected within its declared codecs and local capabilities")
    def choice(fmt):
        return (fmt.payload_type, fmt.encoding, fmt.sample_rate, fmt.channels,
                fmt.frame_ms, sdp.fmtp_parameters(fmt.fmtp))

    accepted = {fmt.payload_type: choice(fmt) for fmt in caller.recv_formats}
    accepted.setdefault(caller.recv.payload_type, choice(caller.recv))
    if (can_retain_receive and choice(send) == choice(caller.send) and choice(selected.recv) == choice(caller.recv)
            and all(accepted.get(fmt.payload_type) == choice(fmt) for fmt in selected.recv_formats)):
        # Recomputing backend bounds or spelling the primary as an alias is
        # not a bridge optimization. Keep the admitted quantum, hard limits
        # and accepted mappings; a genuinely new alias must reach the owner.
        trace.append("B2 same wire choices and no new receive mapping; preserve the admitted contract")
        return result(caller)
    return result(RtpPcmDirection(send, selected.recv, selected.recv_formats, selected.send_formats))
