"""Pure bridge planning over the media contracts of two independent SIP legs."""

from dataclasses import dataclass
from enum import StrEnum

from .. import sdp
from ..audio_capabilities import AudioCapabilities, HA_AUDIO_CAPABILITIES
from ..audio_format import AudioFormat, PcmFormat
from ..codec_policy import CodecPolicy
from ..sdp_types import RtpPcmDirection, RtpPcmFormat
from .codecs import CODECS


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


@dataclass(frozen=True, slots=True)
class BridgeAudioPlan:
    caller: RtpPcmDirection
    caller_to_callee: AudioFlowPlan
    callee_to_caller: AudioFlowPlan
    trace: tuple[str, ...]


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
    if same_mapping and same_parameters:
        if source.frame_ms == target.frame_ms:
            return AudioFlowPlan(source, target, (AudioOperation.PASSTHROUGH,), "same encoded mapping and packet time")
        if source.encoding != "OPUS":
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
    selected = sdp.negotiate_directional(remote_sdp, list(dict.fromkeys(send_formats)),
        list(dict.fromkeys(recv_formats)), allow_dahua_pcm=dahua, codec_policy=codec_policy,
        # Carry the backend used for admission. Its preset list ranks choices;
        # it cannot become a smaller fixed receiver on the bridge boundary.
        local_recv_capabilities=(receive_capabilities or None) if capabilities is None else None,
        local_capabilities=(capabilities if capabilities is not None else
                            None if send_capabilities else HA_AUDIO_CAPABILITIES))
    if selected is None:
        trace.append("B2 no feasible alternative inside the caller's offer and policy")
        return result(caller)
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
    if (choice(send) == choice(caller.send) and choice(selected.recv) == choice(caller.recv)
            and all(accepted.get(fmt.payload_type) == choice(fmt) for fmt in selected.recv_formats)):
        # Recomputing backend bounds or spelling the primary as an alias is
        # not a bridge optimization. Keep the admitted quantum, hard limits
        # and accepted mappings; a genuinely new alias must reach the owner.
        trace.append("B2 same wire choices and no new receive mapping; preserve the admitted contract")
        return result(caller)
    return result(RtpPcmDirection(send, selected.recv, selected.recv_formats, selected.send_formats))
