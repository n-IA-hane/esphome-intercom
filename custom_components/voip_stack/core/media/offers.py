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
    if policy.codec_order:
        values.sort(key=lambda fmt: policy.codec_order.index(fmt.encoding)
                    if fmt.encoding in policy.codec_order else len(policy.codec_order))
    allowed = policy.encodings if policy.encodings is not None else frozenset(DEFAULT_AUDIO_CODECS)
    backend = capabilities or HA_AUDIO_CAPABILITIES
    return [fmt for fmt in values if fmt.encoding in allowed and fmt.encoding in CODECS
            and not CODECS[fmt.encoding].auxiliary and codec_available(fmt.encoding, receive=receive)
            and backend.supports_rtp(fmt, receive=receive)]


def create_complete_offer(policy, send_formats, receive_formats, *, send_rtp_formats=None,
                          recv_rtp_formats=None, audio_rtp_formats=None, common=False,
                          dahua=False, directional=False, independent=True, direction="sendrecv",
                          capabilities=None, previous=None):
    """Plan one full or explicitly constrained offer; never guess missing codecs."""
    if audio_rtp_formats is not None:
        send_rtp_formats = tuple(fmt for fmt in audio_rtp_formats if fmt.audio_format in send_formats)
        recv_rtp_formats = tuple(fmt for fmt in audio_rtp_formats if fmt.audio_format in receive_formats)
    sends = (_catalog(send_formats, send_rtp_formats, receive=False, common=common, dahua=dahua,
                      capabilities=capabilities, policy=policy) if direction != "recvonly" else [])
    receives = (_catalog(receive_formats, recv_rtp_formats, receive=True, common=common, dahua=dahua,
                         capabilities=capabilities, policy=policy) if direction != "sendonly" else [])
    explicit = send_rtp_formats is not None or recv_rtp_formats is not None
    explicit_rows = list(audio_rtp_formats if audio_rtp_formats is not None else
                         (*(send_rtp_formats or ()), *(recv_rtp_formats or ())))
    if explicit and (common or dahua):
        # Explicit entries keep their mappings and parameters. Common codecs
        # add new alternatives only; they do not widen an existing entry's role.
        known = {_key(fmt) for fmt in (*sends, *receives)}
        extra_send = (_catalog(send_formats, None, receive=False, common=common, dahua=dahua,
                              capabilities=capabilities, policy=policy) if direction != "recvonly" else [])
        extra_receive = (_catalog(receive_formats, None, receive=True, common=common, dahua=dahua,
                                 capabilities=capabilities, policy=policy) if direction != "sendonly" else [])
        extra_keys = ({_key(fmt) for fmt in extra_send} & {_key(fmt) for fmt in extra_receive}
                      if direction not in {"sendonly", "recvonly"} else
                      {_key(fmt) for fmt in (*extra_send, *extra_receive)})
        def additional(fmt):
            return (_key(fmt) not in known and _key(fmt) in extra_keys
                    and fmt.encoding not in {"L16", "L24"})
        sends.extend(fmt for fmt in extra_send if additional(fmt))
        receives.extend(fmt for fmt in extra_receive if additional(fmt))
    send_keys, receive_keys = {_key(fmt) for fmt in sends}, {_key(fmt) for fmt in receives}
    if direction == "sendrecv" and (not sends or not receives):
        raise ValueError("No enabled audio codec has the required local directions")
    if direction == "sendonly":
        chosen = sends
    elif direction == "recvonly":
        chosen = receives
    elif directional:
        chosen = [*sends, *receives]
    else:
        chosen = [fmt for fmt in sends if _key(fmt) in receive_keys]
    if explicit:
        chosen_keys = {_key(fmt) for fmt in chosen}
        supplied_order = list(dict.fromkeys(_key(fmt) for fmt in explicit_rows if _key(fmt) in chosen_keys))
        ordered = [*supplied_order, *(key for key in dict.fromkeys(_key(fmt) for fmt in chosen) if key not in supplied_order)]
    else:
        ordered = list(dict.fromkeys(_key(fmt) for fmt in chosen))
    if policy.codec_order:
        ordered.sort(key=lambda mapping: policy.codec_order.index(mapping[0])
                     if mapping[0] in policy.codec_order else len(policy.codec_order))
    if not ordered:
        raise ValueError("No enabled audio codec has the required local directions")
    active_receive = [fmt for fmt in receives if _key(fmt) in ordered] if direction != "sendonly" else []
    def pcm_geometry(pcm):
        wire_pcm = PcmFormat.S24LE if pcm.pcm_format is PcmFormat.S24LE_IN_S32 else pcm.pcm_format
        return pcm.sample_rate, wire_pcm, pcm.channels

    def fixed_times(fmt, formats):
        geometry = pcm_geometry(fmt.audio_format)
        return {pcm.frame_ms for pcm in formats if pcm_geometry(pcm) == geometry}

    def receive_limit(fmt):
        envelope = (capabilities.max_receive_packet_ms(fmt) if capabilities is not None
                    else max(fixed_times(fmt, receive_formats), default=0))
        return min(envelope, fmt.max_frame_ms) if fmt.max_frame_ms else envelope

    maximum = min((receive_limit(fmt) for fmt in active_receive), default=0)
    receiver = capabilities or HA_AUDIO_CAPABILITIES
    preferred = active_receive[0].frame_ms if active_receive else 0
    active_send = [fmt for fmt in sends if _key(fmt) in ordered]
    legacy_common_time = directional and not independent and direction == "sendrecv"
    sender_times = {fmt.frame_ms for fmt in active_send} if legacy_common_time else None
    explicit_send = [fmt for fmt in active_send if fmt in (send_rtp_formats or ())]
    explicit_receive = [fmt for fmt in active_receive if fmt in (recv_rtp_formats or ())]

    def supports_time(fmt, packet_ms, *, receive):
        local = receive_formats if receive else send_formats
        if capabilities is None and packet_ms not in fixed_times(fmt, local):
            return False
        if receive and packet_ms > receive_limit(fmt):
            return False
        return receiver.supports_rtp(replace(fmt, frame_ms=packet_ms), receive=receive)

    def compatible(packet_ms):
        if not packet_ms or not active_receive:
            return False
        if legacy_common_time:
            if not supports_time(active_send[0], packet_ms, receive=False):
                return False
            # A caller's explicit mappings are hard input. Automatic mappings
            # may be omitted when they cannot share the legacy primary's time.
            if any(not supports_time(fmt, packet_ms, receive=False) for fmt in explicit_send):
                return False
            if any(not supports_time(fmt, packet_ms, receive=True) for fmt in explicit_receive):
                return False
            return any(supports_time(fmt, packet_ms, receive=True) for fmt in active_receive)
        if maximum and packet_ms > maximum:
            return False
        return all(supports_time(fmt, packet_ms, receive=True) for fmt in active_receive)
    declared = [{fmt.frame_ms for fmt in active_receive if _key(fmt) == mapping}
                for mapping in ordered if mapping in receive_keys]
    common_times = set.intersection(*declared) if declared else set()
    # Preserve a duration declared for every mapping before considering a
    # dynamic fallback. Explicit directional catalogs can have no common
    # preset even though every receiver implements the primary duration.
    common_order = (policy.default_packet_ms, *sorted(common_times))
    common_candidates = [value for value in common_order if value in common_times]
    primary_time = (active_send[0].frame_ms,) if legacy_common_time else ()
    candidates = dict.fromkeys((*primary_time, *common_candidates, preferred, policy.default_packet_ms,
        *(fmt.frame_ms for fmt in active_receive), *sorted(sender_times or ()), *STANDARD_LADDER_MS))
    packet_ms = (sends[0].frame_ms if direction == "sendonly" else
                 next((value for value in candidates if compatible(value)), 0))
    if legacy_common_time and not packet_ms:
        raise ValueError("Legacy directional audio requires a common packet time")
    pruned = []
    if legacy_common_time:
        for values, receiving in ((active_send, False), (active_receive, True)):
            for fmt in values:
                if not supports_time(fmt, packet_ms, receive=receiving):
                    side = "receive" if receiving else "send"
                    pruned.append(f"R3 omitted automatic {side} {fmt.encoding}/{fmt.sample_rate}/{fmt.channels}: "
                                  f"cannot use the primary's {packet_ms} ms within its fixed set and budget")
        sends = [fmt for fmt in active_send if supports_time(fmt, packet_ms, receive=False)]
        receives = [fmt for fmt in active_receive if supports_time(fmt, packet_ms, receive=True)]
        active_receive = receives
        send_keys, receive_keys = {_key(fmt) for fmt in sends}, {_key(fmt) for fmt in receives}
        ordered = [key for key in ordered if key in send_keys | receive_keys]
        chosen = [*sends, *receives]
        maximum = min((receive_limit(fmt) for fmt in active_receive), default=0)
    events = list(dict.fromkeys([8000, *(clock for _encoding, clock, _channels in ordered)]))
    auxiliary = []
    cn_clock = CODECS["CN"].static_clock_rate
    if (policy.encodings and "CN" in policy.encodings
            and any((clock, channels) == (cn_clock, 1) for _name, clock, channels in ordered)):
        if ((direction == "recvonly" or codec_available("CN"))
                and (direction == "sendonly" or codec_available("CN", receive=True))):
            auxiliary = [("CN", cn_clock, 1)]
    preferences = {}
    for fmt in explicit_rows:
        if _key(fmt) in ordered:
            preferences.setdefault(_key(fmt), fmt.payload_type)
    # Existing Dahua firmware sends its private PCM codec on 97. Keep the
    # target's conventional offer seed without displacing explicit mappings;
    # PayloadMap still gives every live or retired dialog meaning precedence.
    dahua_pcm = ("PCM", 16000, 1)
    if dahua and dahua_pcm in ordered and 97 not in preferences.values():
        preferences.setdefault(dahua_pcm, 97)
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
        (f"S1 offered {len(primary)} enabled primary mappings", f"R1 ask {packet_ms or 'unspecified'} ms", *pruned),
        formats=(*primary, *aux_formats), send_formats=mapped_sends, receive_formats=mapped_receives,
        events=tuple(RtpDtmfFormat(numbers[(TELEPHONE_EVENT, clock, 1)], clock) for clock in events),
        flows=flows, maximum_ms=maximum)
