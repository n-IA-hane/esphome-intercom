"""Complete outgoing audio offers from capabilities and the leg policy."""

from dataclasses import replace

from ..audio_capabilities import HA_AUDIO_CAPABILITIES, local_rtp_audio_formats
from ..audio_format import PcmFormat
from ..codec_capabilities import codec_available
from ..codec_policy import DEFAULT_AUDIO_CODECS
from ..sdp_types import RtpDtmfFormat, RtpPcmFormat
from .codecs import CODECS
from .leg import OfferPlan
from .packet_time import PacketTimes, ReceiverBounds, STANDARD_LADDER_MS, choose_transmit_packet_ms
from .payload_map import PayloadMap, TELEPHONE_EVENT


def _key(fmt):
    return fmt.encoding, fmt.sample_rate, fmt.channels


def _catalog(formats, explicit, *, receive, common, dahua, capabilities, policy):
    if explicit is not None:
        values = list(explicit)
    else:
        hints = []
        for pcm in formats:
            if pcm.pcm_format not in {PcmFormat.S16LE, PcmFormat.S24LE, PcmFormat.S24LE_IN_S32}:
                continue
            fmt = RtpPcmFormat(96, "L16" if pcm.pcm_format is PcmFormat.S16LE else "L24",
                               pcm.sample_rate, pcm.channels, pcm.frame_ms)
            if capabilities is not None and not receive:
                choice = choose_transmit_packet_ms(CODECS[fmt.encoding], fmt.sample_rate, fmt.channels,
                    producible=PacketTimes(), default_ms=policy.default_packet_ms,
                    receiver=ReceiverBounds(), budget=policy.budget)
                if choice is not None:
                    fmt = replace(fmt, frame_ms=choice.packet_ms)
            hints.append(fmt)
        values = list(local_rtp_audio_formats(list(formats), hints=hints, receive=receive))
        if not common:
            values = [fmt for fmt in values if fmt.encoding in {"L16", "L24"}]
        if capabilities is None:
            values = [fmt for fmt in values if fmt.audio_format in formats]
        if dahua and any(pcm.sample_rate == 16000 and pcm.channels == 1 and pcm.pcm_format is PcmFormat.S16LE for pcm in formats):
            values.insert(0, RtpPcmFormat(97, "PCM", 16000, 1, 20))
        order = policy.codec_order or DEFAULT_AUDIO_CODECS
        values.sort(key=lambda fmt: order.index(fmt.encoding) if fmt.encoding in order else len(order))
    allowed = policy.encodings if policy.encodings is not None else frozenset(DEFAULT_AUDIO_CODECS)
    return [fmt for fmt in values if fmt.encoding in allowed and fmt.encoding in CODECS
            and not CODECS[fmt.encoding].auxiliary and codec_available(fmt.encoding, receive=receive)]


def create_complete_offer(policy, send_formats, receive_formats, *, send_rtp_formats=None,
                          recv_rtp_formats=None, audio_rtp_formats=None, common=False,
                          dahua=False, directional=False, independent=True, direction="sendrecv",
                          capabilities=None, previous=None):
    """Plan one full or explicitly constrained offer; never guess missing codecs."""
    if audio_rtp_formats is not None:
        send_rtp_formats = tuple(fmt for fmt in audio_rtp_formats if fmt.audio_format in send_formats)
        recv_rtp_formats = tuple(fmt for fmt in audio_rtp_formats if fmt.audio_format in receive_formats)
    sends = _catalog(send_formats, send_rtp_formats, receive=False, common=common, dahua=dahua,
                     capabilities=capabilities, policy=policy)
    receives = _catalog(receive_formats, recv_rtp_formats, receive=True, common=common, dahua=dahua,
                        capabilities=capabilities, policy=policy)
    send_keys, receive_keys = {_key(fmt) for fmt in sends}, {_key(fmt) for fmt in receives}
    if direction == "sendonly":
        chosen = sends
    elif direction == "recvonly":
        chosen = receives
    elif directional:
        chosen = [*sends, *receives]
    else:
        chosen = [fmt for fmt in sends if _key(fmt) in receive_keys]
    ordered = list(dict.fromkeys(_key(fmt) for fmt in chosen))
    if not ordered:
        raise ValueError("No enabled audio codec has the required local directions")
    active_receive = [fmt for fmt in receives if _key(fmt) in ordered] if direction != "sendonly" else []
    maximum = min((fmt.max_frame_ms or (capabilities.max_receive_packet_ms(fmt) if capabilities is not None
                   else max(item.frame_ms for item in active_receive if _key(item) == _key(fmt)))
                   for fmt in active_receive), default=0)
    receiver = capabilities or HA_AUDIO_CAPABILITIES
    preferred = active_receive[0].frame_ms if active_receive else 0
    sender_times = ({fmt.frame_ms for fmt in sends if _key(fmt) == _key(sends[0])}
                    if sends and directional and not independent else None)
    def compatible(packet_ms):
        if not packet_ms or not active_receive or (maximum and packet_ms > maximum):
            return False
        if sender_times is not None and packet_ms not in sender_times:
            return False
        for fmt in active_receive:
            if capabilities is None and not fmt.max_frame_ms:
                if not any(_key(item) == _key(fmt) and item.frame_ms == packet_ms for item in active_receive):
                    return False
            if not receiver.supports_rtp(replace(fmt, frame_ms=packet_ms), receive=True):
                return False
        return True
    declared = [{fmt.frame_ms for fmt in active_receive if _key(fmt) == mapping}
                for mapping in ordered if mapping in receive_keys]
    common_times = set.intersection(*declared) if declared else set()
    # Preserve a duration declared for every mapping before considering a
    # dynamic fallback. Explicit directional catalogs can have no common
    # preset even though every receiver implements the primary duration.
    common_order = (policy.default_packet_ms, *sorted(common_times))
    common_candidates = [value for value in common_order if value in common_times]
    candidates = dict.fromkeys((*common_candidates, preferred, policy.default_packet_ms,
        *(fmt.frame_ms for fmt in active_receive), *(sender_times or ()), *STANDARD_LADDER_MS))
    packet_ms = next((value for value in candidates if compatible(value)), 0)
    if directional and not independent and not packet_ms:
        raise ValueError("Legacy directional audio requires a common packet time")
    events = list(dict.fromkeys([8000, *(clock for _encoding, clock, _channels in ordered)]))
    auxiliary = []
    if policy.encodings and "CN" in policy.encodings and any(clock == 8000 for _name, clock, _channels in ordered):
        if codec_available("CN") and codec_available("CN", receive=True):
            auxiliary = [("CN", 8000, 1)]
    preferences = {_key(fmt): fmt.payload_type for fmt in chosen}
    numbered = PayloadMap.for_offer([*ordered, *auxiliary, *((TELEPHONE_EVENT, clock, 1) for clock in events)],
                                    previous=previous, preferences=preferences)
    numbers = {mapping: number for number, mapping in numbered.receive.items()}
    mapped_sends = tuple(replace(fmt, payload_type=numbers[_key(fmt)]) for fmt in sends if _key(fmt) in ordered)
    mapped_receives = tuple(replace(fmt, payload_type=numbers[_key(fmt)]) for fmt in active_receive)
    unique = {_key(fmt): replace(fmt, payload_type=numbers[_key(fmt)]) for fmt in reversed(chosen)}
    primary = tuple(unique[mapping] for mapping in ordered)
    aux_formats = tuple(RtpPcmFormat(numbers[mapping], *mapping, packet_ms or 20) for mapping in auxiliary)
    flows = tuple((numbers[mapping], "sendrecv" if mapping in send_keys and mapping in receive_keys
                   else "send" if mapping in send_keys else "recv") for mapping in ordered)
    return OfferPlan(tuple(numbered.receive.items()), packet_ms,
        (f"S1 offered {len(primary)} enabled primary mappings", f"R1 ask {packet_ms or 'unspecified'} ms"),
        formats=(*primary, *aux_formats), send_formats=mapped_sends, receive_formats=mapped_receives,
        events=tuple(RtpDtmfFormat(numbers[(TELEPHONE_EVENT, clock, 1)], clock) for clock in events),
        flows=flows, maximum_ms=maximum)
