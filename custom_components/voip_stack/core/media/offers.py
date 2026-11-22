"""Complete outgoing audio offers from capabilities and the leg policy."""

from dataclasses import replace

from ..audio_capabilities import HA_AUDIO_CAPABILITIES, local_rtp_audio_formats
from ..audio_format import PcmFormat
from ..codec_capabilities import codec_available
from ..codec_policy import DEFAULT_AUDIO_CODECS
from ..sdp_types import RtpDtmfFormat, RtpPcmFormat
from .codecs import CODECS
from .capability import constrain_formats
from .leg import OfferPlan
from .packet_time import PacketTimes, ReceiverBounds, STANDARD_LADDER_MS, choose_transmit_packet_ms
from .payload_map import PayloadMap, TELEPHONE_EVENT


def _key(fmt):
    return fmt.encoding, fmt.sample_rate, fmt.channels


def expand_configured_linear_formats(values, formats, codec_policy, *, receive,
                                     capabilities=HA_AUDIO_CAPABILITIES, budget=None, default_ms=20):
    """Expand an explicit PCM-width override through the same dynamic HA backend."""
    values = list(values)
    if codec_policy is None or not codec_policy.audio_configured or capabilities is None:
        return values
    known = {_key(fmt) for fmt in values}
    for name in codec_policy.audio or ():
        if name not in {"L16", "L24"}:
            continue
        for pcm in formats:
            fmt = RtpPcmFormat(96, name, pcm.sample_rate, pcm.channels, pcm.frame_ms)
            if _key(fmt) in known or not codec_available(name, receive=receive):
                continue
            if not receive:
                choice = choose_transmit_packet_ms(CODECS[name], fmt.sample_rate, fmt.channels,
                    producible=PacketTimes(), default_ms=default_ms, receiver=ReceiverBounds(),
                    budget=capabilities.transmit_payload_bytes if budget is None else budget)
                if choice is None:
                    continue
                fmt = replace(fmt, frame_ms=choice.packet_ms)
            if capabilities.supports_rtp(fmt, receive=receive):
                values.append(fmt)
                known.add(_key(fmt))
    return values


def _catalog(formats, explicit, *, receive, common, dahua, capabilities, policy,
             codec_policy=None, declared=()):
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
        if capabilities is not None:
            # Presets rank PCM geometry; an explicit standard PCM codec can
            # use the dynamic HA converter at another supported sample width.
            # Unconfigured defaults and explicit RTP rows keep their catalogue.
            values = expand_configured_linear_formats(values, formats, codec_policy,
                receive=receive, capabilities=capabilities, budget=policy.budget,
                default_ms=policy.default_packet_ms)
            known = {_key(fmt) for fmt in values}
            # Complete peer evidence may describe a valid linear geometry
            # absent from presets, including a one-way microphone or speaker.
            allowed = policy.encodings if policy.encodings is not None else DEFAULT_AUDIO_CODECS
            for item in declared:
                if item.mapping[0] in {"L16", "L24"} and item.mapping[0] in allowed and item.mapping not in known:
                    for packet_ms in item.times.explicit or (policy.default_packet_ms,):
                        fmt = RtpPcmFormat(96, *item.mapping, packet_ms)
                        if not receive:
                            choice = choose_transmit_packet_ms(CODECS[fmt.encoding],
                                fmt.sample_rate, fmt.channels, producible=PacketTimes(),
                                default_ms=packet_ms, receiver=ReceiverBounds(), budget=policy.budget)
                            if choice is None:
                                continue
                            fmt = replace(fmt, frame_ms=choice.packet_ms)
                        values.append(fmt)
                    known.add(item.mapping)
    if policy.codec_order:
        values.sort(key=lambda fmt: policy.codec_order.index(fmt.encoding)
                    if fmt.encoding in policy.codec_order else len(policy.codec_order))
    allowed = policy.encodings if policy.encodings is not None else frozenset(DEFAULT_AUDIO_CODECS)
    backend = capabilities or HA_AUDIO_CAPABILITIES
    return [fmt for fmt in values if fmt.encoding in allowed and fmt.encoding in CODECS
            and not CODECS[fmt.encoding].auxiliary and codec_available(fmt.encoding, receive=receive)
            and backend.supports_rtp(fmt, receive=receive)]


def _declared_catalog(values, declaration, *, explicit, capabilities, receive):
    """Constrain one direction without turning dynamic preferences into fixed quanta."""
    candidates = list(values)
    if capabilities is not None and not explicit:
        times = {item.mapping: item.times.explicit for item in declaration}
        seen = {(_key(fmt), fmt.frame_ms) for fmt in candidates}
        for fmt in values:
            for packet_ms in times.get(_key(fmt), ()):
                candidate = replace(fmt, frame_ms=packet_ms)
                identity = (_key(candidate), packet_ms)
                if identity not in seen and capabilities.supports_rtp(candidate, receive=receive):
                    candidates.append(candidate)
                    seen.add(identity)
    return list(constrain_formats(candidates, declaration))


def create_complete_offer(policy, send_formats, receive_formats, *, send_rtp_formats=None,
                          recv_rtp_formats=None, audio_rtp_formats=None, common=False,
                          dahua=False, direction="sendrecv",
                          capabilities=None, previous=None, codec_policy=None, evidence=None):
    """Plan one full or explicitly constrained offer; never guess missing codecs."""
    # A sendrecv m-line advertises formats usable in either direction. Local
    # preferences remain directional; private flow flags do not grant codecs.
    if audio_rtp_formats is not None:
        send_rtp_formats = tuple(fmt for fmt in audio_rtp_formats if fmt.audio_format in send_formats)
        recv_rtp_formats = tuple(fmt for fmt in audio_rtp_formats if fmt.audio_format in receive_formats)
    known_send = evidence is not None and (evidence.complete or evidence.send_geometry_complete)
    known_receive = evidence is not None and (evidence.complete or evidence.receive_geometry_complete)
    peer_sends = evidence.sends if known_send else ()
    peer_receives = evidence.receives if known_receive else ()
    sends = (_catalog(send_formats, send_rtp_formats, receive=False, common=common, dahua=dahua,
                      capabilities=capabilities, policy=policy, codec_policy=codec_policy,
                      declared=peer_receives) if direction != "recvonly" else [])
    receives = (_catalog(receive_formats, recv_rtp_formats, receive=True, common=common, dahua=dahua,
                         capabilities=capabilities, policy=policy, codec_policy=codec_policy,
                         declared=peer_sends) if direction != "sendonly" else [])
    explicit = send_rtp_formats is not None or recv_rtp_formats is not None
    explicit_rows = list(audio_rtp_formats if audio_rtp_formats is not None else
                         (*(send_rtp_formats or ()), *(recv_rtp_formats or ())))
    if explicit and common:
        # Explicit entries keep their mappings and parameters. Common codecs
        # add new alternatives only; they do not widen an existing entry's role.
        # A vendor PCM permission alone never expands a complete RTP catalog.
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
    if known_receive:
        sends = _declared_catalog(sends, peer_receives, explicit=send_rtp_formats is not None,
                                 capabilities=capabilities, receive=False)
    if known_send:
        receives = _declared_catalog(receives, peer_sends, explicit=recv_rtp_formats is not None,
                                    capabilities=capabilities, receive=True)
    for values, declaration in ((sends, evidence.receives if evidence else ()),
                                (receives, tuple(item for item in evidence.sends if item.times.fixed)
                                 if evidence else ())):
        ranking = {item.mapping: index for index, item in enumerate(declaration)}
        values.sort(key=lambda fmt: (
            policy.codec_order.index(fmt.encoding) if fmt.encoding in policy.codec_order else len(policy.codec_order),
            ranking.get(_key(fmt), len(ranking))))
    receive_keys = {_key(fmt) for fmt in receives}
    if direction == "sendrecv" and (not sends or not receives):
        raise ValueError("No enabled audio codec has the required local directions")
    if direction == "sendonly":
        chosen = sends
    elif direction == "recvonly":
        chosen = receives
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

    def supports_time(fmt, packet_ms, *, receive):
        if evidence is not None and evidence.complete and not receive:
            declaration = peer_receives
            if not any(item.mapping == _key(fmt) and item.times.contains(packet_ms) for item in declaration):
                return False
        local = receive_formats if receive else send_formats
        if capabilities is None and packet_ms not in fixed_times(fmt, local):
            return False
        if receive and packet_ms > receive_limit(fmt):
            return False
        return receiver.supports_rtp(replace(fmt, frame_ms=packet_ms), receive=receive)

    def compatible(packet_ms):
        if not packet_ms or not active_receive:
            return False
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
    candidates = dict.fromkeys((preferred, *common_candidates, policy.default_packet_ms,
        *(fmt.frame_ms for fmt in active_receive), *STANDARD_LADDER_MS))
    packet_ms = (sends[0].frame_ms if direction == "sendonly" else
                 next((value for value in candidates if compatible(value)), 0))
    if active_receive and not packet_ms:
        raise ValueError("Audio mappings have no common receivable packet time")
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
    mapped_receives = tuple(replace(fmt, payload_type=numbers[_key(fmt)],
        frame_ms=packet_ms, max_frame_ms=maximum) for fmt in active_receive)
    unique = {_key(fmt): replace(fmt, payload_type=numbers[_key(fmt)]) for fmt in reversed(chosen)}
    primary = tuple(unique[mapping] for mapping in ordered)
    aux_formats = tuple(RtpPcmFormat(numbers[mapping], *mapping, packet_ms or 20) for mapping in auxiliary)
    return OfferPlan(tuple(numbered.receive.items()), packet_ms,
        (f"S1 offered {len(primary)} enabled primary mappings", f"R1 ask {packet_ms or 'unspecified'} ms"),
        formats=(*primary, *aux_formats), send_formats=mapped_sends, receive_formats=mapped_receives,
        events=tuple(RtpDtmfFormat(numbers[(TELEPHONE_EVENT, clock, 1)], clock) for clock in events),
        maximum_ms=maximum, include_dahua_pcm=dahua)
