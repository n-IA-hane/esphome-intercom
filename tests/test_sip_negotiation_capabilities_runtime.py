"""Runtime audio policy negotiates valid peers beyond its preferred tuples."""

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.voip_stack.core import codec_capabilities, rtp, sdp, sip
from custom_components.voip_stack.core.audio_capabilities import (
    HA_AUDIO_CAPABILITIES,
    local_rtp_audio_formats,
)
from custom_components.voip_stack.core.audio_format import (
    AudioFormat,
    HA_SIP_PCM_TX_FORMATS,
    PcmFormat,
    UDP_SAFE_PAYLOAD_BYTES,
)
from custom_components.voip_stack.endpoint_routing import (
    sip_target_audio_profile,
    sip_target_rtp_audio_profile,
)
from custom_components.voip_stack.rtp_audio_receiver import RtpAudioReceiver
from custom_components.voip_stack.sip_client import RtpPayloadEncoder
from custom_components.voip_stack.sip_listener import SipInviteResult, SipUdpEndpoint


pytestmark = pytest.mark.ha


def _negotiate(offer, formats=None, **kwargs):
    return sdp.negotiate_directional(
        offer,
        *(formats if formats is not None else _local_formats()),
        local_capabilities=HA_AUDIO_CAPABILITIES,
        **kwargs,
    )


def _peer_sdp(
    encoding="L16",
    rate=8000,
    channels=1,
    frame_ms=32,
    *,
    port=35000,
    min_frame_ms=None,
    max_frame_ms=None,
    payload_type=110,
):
    """Describe an independent standard peer, without the production builder."""
    minimum = frame_ms if min_frame_ms is None else min_frame_ms
    maximum = frame_ms if max_frame_ms is None else max_frame_ms
    min_line = f"a=minptime:{minimum}\r\n" if minimum else ""
    max_line = f"a=maxptime:{maximum}\r\n" if maximum else ""
    return (
        "v=0\r\no=- 1 1 IN IP4 192.0.2.2\r\ns=Capability regression peer\r\n"
        "c=IN IP4 192.0.2.2\r\nt=0 0\r\n"
        f"m=audio {port} RTP/AVP {payload_type}\r\n"
        f"a=rtpmap:{payload_type} {encoding}/{rate}/{channels}\r\n"
        f"a=ptime:{frame_ms}\r\n"
        f"{min_line}{max_line}"
        "a=sendrecv\r\n"
    )


def _local_formats(discovered_sdp=None):
    metadata = (
        {**sdp.options_capabilities(discovered_sdp), "sip_capabilities_known": True}
        if discovered_sdp is not None
        else {}
    )
    entry = SimpleNamespace(metadata=metadata)
    profile = sip_target_rtp_audio_profile(None, entry)
    if profile is not None:
        return list(profile.send_formats), list(profile.recv_formats)
    return sip_target_audio_profile(
        remote_tx_formats=None, remote_rx_formats=None, target="regression-peer"
    )


@pytest.mark.parametrize("discovery_known", [False, True], ids=["unknown", "known"])
def test_l16_8000_mono_32ms_is_negotiable_without_a_preferred_tuple(discovery_known):
    offer = _peer_sdp()
    local_send, local_recv = _local_formats(
        _peer_sdp(port=0) if discovery_known else None
    )
    selected = _negotiate(offer, (local_send, local_recv))

    assert selected is not None, (
        "valid L16/8000/1 with an exact 32 ms receiver range was lost"
    )
    assert (
        selected.send.encoding,
        selected.send.sample_rate,
        selected.send.channels,
        selected.send.frame_ms,
    ) == ("L16", 8000, 1, 32)
    assert (
        selected.recv.encoding,
        selected.recv.sample_rate,
        selected.recv.channels,
    ) == ("L16", 8000, 1)
    assert selected.recv.frame_ms != selected.send.frame_ms
    assert HA_AUDIO_CAPABILITIES.supports_rtp(selected.send)
    assert HA_AUDIO_CAPABILITIES.supports_rtp(selected.recv, receive=True)
    assert HA_AUDIO_CAPABILITIES.max_receive_packet_ms(selected.recv) >= 32
    # The actual witness packet must fit the decoder even though its local
    # output quantum remains independent of the peer's receive preference.
    receiver = RtpAudioReceiver(selected.recv)
    assert receiver.decode(selected.recv.payload_type, bytes(512))
    _assert_receiver_matches_advertised_bounds(offer, selected)


def _assert_receiver_matches_advertised_bounds(offer, selected):
    answer = sdp.build_answer_directional(
        "192.0.2.1",
        "192.0.2.1",
        40000,
        selected.send,
        selected.recv,
        remote_sdp=offer,
    )
    advertised = sdp.parse_sdp(answer)
    fmt = selected.recv
    assert advertised["ptime"] == fmt.frame_ms
    maximum = advertised["maxptime"]
    assert maximum >= fmt.frame_ms
    sample_bytes = fmt.channels * (2 if fmt.encoding == "L16" else 3)
    wire_bytes = fmt.sample_rate * maximum * sample_bytes // 1000
    assert wire_bytes <= rtp.MAX_AUDIO_RTP_PAYLOAD_BYTES
    receiver = RtpAudioReceiver(fmt)
    frames = receiver.decode(fmt.payload_type, bytes(wire_bytes))
    assert frames
    decoded = b"".join(frames)
    assert decoded == bytes(len(decoded))
    assert len(decoded) <= wire_bytes
    with pytest.raises(rtp.RtpError):
        receiver.decode(fmt.payload_type, bytes(wire_bytes + sample_bytes))


@pytest.mark.parametrize("preference, maximum", [(32, 20), (30, 12), (40, 10)])
def test_peer_maxptime_limits_tx_without_narrowing_local_rx(preference, maximum):
    offer = _peer_sdp(frame_ms=preference, min_frame_ms=0, max_frame_ms=maximum)
    selected = _negotiate(offer)
    assert selected is not None
    assert selected.send.encoding == selected.recv.encoding == "L16"
    assert selected.send.sample_rate == selected.recv.sample_rate == 8000
    assert selected.send.frame_ms <= maximum
    assert selected.recv.frame_ms == 20
    _assert_receiver_matches_advertised_bounds(offer, selected)


def test_native_default_sends_48k_10ms_and_receives_16k_20ms():
    offer = (
        "v=0\r\no=- 1 1 IN IP4 192.0.2.2\r\ns=Independent native peer\r\n"
        "c=IN IP4 192.0.2.2\r\nt=0 0\r\nm=audio 35000 RTP/AVP 110 111\r\n"
        "a=rtpmap:110 L16/16000/1\r\na=rtpmap:111 L16/48000/1\r\n"
        "a=x-voip-stack-flow:110 send\r\na=x-voip-stack-flow:111 recv\r\n"
        "a=x-voip-stack-independent-ptime:1\r\n"
        "a=ptime:10\r\na=maxptime:10\r\na=sendrecv\r\n"
    )
    selected = _negotiate(offer)
    assert selected is not None
    assert (selected.send.sample_rate, selected.send.frame_ms) == (48000, 10)
    assert (selected.recv.sample_rate, selected.recv.frame_ms) == (16000, 20)


@pytest.mark.parametrize(
    "user_agent, allowed", [("Dahua UAC/V4.511", True), ("Generic SIP", False)]
)
def test_dahua_pcm_stays_profile_gated_little_endian(user_agent, allowed):
    offer = _peer_sdp("PCM", 16000, frame_ms=20)
    selected = _negotiate(
        offer,
        allow_dahua_pcm=codec_capabilities.supports_dahua_pcm(user_agent),
    )
    assert (selected is not None) is allowed
    if selected is None:
        return
    assert selected.send.encoding == selected.recv.encoding == "PCM"
    pcm = b"\x34\x12\xcc\xed" * (selected.send.audio_format.nominal_frame_samples // 2)
    assert RtpPayloadEncoder(selected.send).encode(pcm) == pcm
    assert b"".join(RtpAudioReceiver(selected.recv).decode(110, pcm)) == pcm


@pytest.mark.parametrize(
    "encoding, clock_rate, channels, pcm_rate",
    [
        ("G722", 8000, 1, 16000),
        ("OPUS", 48000, 2, 48000),
    ],
)
def test_optional_codec_uses_its_rtp_clock_independently_of_pcm_rate(
    encoding,
    clock_rate,
    channels,
    pcm_rate,
):
    if encoding not in codec_capabilities.common_sip_codecs():
        pytest.skip(f"{encoding} backend is unavailable")
    selected = _negotiate(_peer_sdp(encoding, clock_rate, channels, frame_ms=20))
    assert selected is not None
    for fmt in (selected.send, selected.recv):
        assert fmt.rtp_clock_rate == clock_rate
        assert fmt.pcm_sample_rate == pcm_rate
        assert fmt.rtp_timestamp_step == clock_rate * fmt.frame_ms // 1000
    pcm = bytes(selected.send.audio_format.nominal_frame_bytes)
    payload = RtpPayloadEncoder(selected.send).encode(pcm)
    assert payload
    assert len(payload) <= rtp.MAX_RTP_PAYLOAD_BYTES
    assert RtpAudioReceiver(selected.recv).decode(110, payload)


def _declared_wire_shapes():
    """Discover the runtime codec shapes from real local capabilities.

    Seed the common PCM domain, including rates absent from HA's preference
    tuples. The production capability query supplies codec mappings and
    optional backend availability. Expected duration/size results below are
    computed independently from wire samples, not from supports_rtp().
    """
    preferred = list(HA_SIP_PCM_TX_FORMATS)
    for rate in (8000, 11025, 16000, 24000, 32000, 44100, 48000, 96000):
        for pcm in (PcmFormat.S16LE, PcmFormat.S24LE):
            for channels in (1, 2):
                # 11025 Hz needs 40 ms to contain an integral sample count.
                preferred.append(
                    AudioFormat(rate, pcm, channels, 40 if rate == 11025 else 10)
                )
    shapes = {
        (fmt.encoding, fmt.sample_rate, fmt.channels)
        for fmt in local_rtp_audio_formats(preferred)
    }
    return tuple(sorted(shapes))


WIRE_SHAPES = _declared_wire_shapes()


def test_declared_capabilities_cover_mono_stereo_and_optional_codec_families():
    required = {
        ("L16", 8000, 1),
        ("L16", 16000, 2),
        ("L16", 32000, 1),
        ("L24", 8000, 1),
        ("L24", 16000, 2),
        ("PCMA", 8000, 1),
        ("PCMU", 8000, 1),
    }
    if "G722" in codec_capabilities.common_sip_codecs():
        required.add(("G722", 8000, 1))
    if "OPUS" in codec_capabilities.common_sip_codecs():
        required.add(("OPUS", 48000, 2))
    assert required <= set(WIRE_SHAPES)


def _shape_id(shape):
    return f"{shape[0]}-{shape[1]}-{shape[2]}ch"


def _wire_duration_fits(encoding, rate, channels, frame_ms):
    if encoding == "OPUS":
        return frame_ms in {2.5, 5, 10, 20, 40, 60}
    numerator = rate * frame_ms
    if numerator % 1000:
        return False
    width = {"L16": 2, "L24": 3, "PCMA": 1, "PCMU": 1, "G722": 1}[encoding]
    return numerator * channels * width / 1000 <= UDP_SAFE_PAYLOAD_BYTES


@pytest.mark.parametrize("shape", WIRE_SHAPES, ids=_shape_id)
@pytest.mark.parametrize("frame_ms", [30, 32, 40])
def test_capability_domain_negotiates_nondefault_packet_times(shape, frame_ms):
    encoding, rate, channels = shape
    expected = _wire_duration_fits(encoding, rate, channels, frame_ms)
    offer = _peer_sdp(encoding, rate, channels, frame_ms)
    selected = _negotiate(offer)
    if not expected:
        assert selected is None, (
            "a pinned peer range must not escape codec or wire limits"
        )
        return

    assert selected is not None, (
        f"runtime-capable {shape}/{frame_ms} was excluded by preference tuples"
    )
    assert (
        selected.send.encoding,
        selected.send.sample_rate,
        selected.send.channels,
        selected.send.frame_ms,
    ) == (*shape, frame_ms)
    assert HA_AUDIO_CAPABILITIES.supports_rtp(selected.send)
    assert (
        selected.recv.encoding,
        selected.recv.sample_rate,
        selected.recv.channels,
    ) == shape
    pcm = bytes(selected.send.audio_format.nominal_frame_bytes)
    payload = RtpPayloadEncoder(selected.send).encode(pcm)
    assert payload
    packet = rtp.build_packet(rtp.RtpPacket(110, 1, 0, 1, payload))
    assert rtp.parse_packet(packet).payload == payload
    assert len(payload) <= UDP_SAFE_PAYLOAD_BYTES
    receiver = RtpAudioReceiver(selected.recv)
    # A whole real encoder packet must be accepted even when RX normalizes it
    # to a different local quantum. Some codecs buffer their first output.
    receiver.decode(selected.recv.payload_type, payload)
    if encoding in {"L16", "L24"}:
        _assert_receiver_matches_advertised_bounds(offer, selected)


@pytest.mark.parametrize("frame_ms", [2.5, 5, 10, 20, 40, 60])
def test_every_declared_opus_tx_duration_opens_the_real_encoder(frame_ms):
    if "OPUS" not in codec_capabilities.common_sip_codecs():
        pytest.skip("Opus backend is unavailable")
    selected = _negotiate(_peer_sdp("OPUS", 48000, 2, frame_ms))
    assert selected is not None
    assert selected.send.frame_ms == frame_ms
    assert selected.send.rtp_timestamp_step == 48 * frame_ms
    payload = RtpPayloadEncoder(selected.send).encode(
        bytes(selected.send.audio_format.nominal_frame_bytes)
    )
    assert payload
    assert (
        RtpAudioReceiver(selected.recv).decode(110, payload)
        or frame_ms < selected.recv.frame_ms
    )


def test_discovery_cannot_change_the_authoritative_8000_only_result():
    offer = _peer_sdp()
    unknown = _negotiate(offer)
    known = _negotiate(offer, _local_formats(_peer_sdp(port=0)))
    assert unknown is not None and known is not None
    assert known == unknown


@pytest.mark.parametrize("discovery_known", [False, True], ids=["unknown", "known"])
def test_final_answer_can_select_8000_32ms_from_the_real_local_offer(discovery_known):
    local_send, local_recv = _local_formats(
        _peer_sdp(port=0) if discovery_known else None
    )
    local_offer = sdp.build_offer_directional(
        "192.0.2.1",
        "192.0.2.1",
        40000,
        local_send,
        local_recv,
        include_common_codecs=True,
    )
    mapping = next(
        fmt
        for fmt in sdp.offered_pcm_formats(local_offer)
        if (fmt.encoding, fmt.sample_rate, fmt.channels) == ("L16", 8000, 1)
    )
    answer = _peer_sdp(payload_type=mapping.payload_type)
    selected = sdp.negotiate_answer_directional(
        answer,
        local_send,
        local_recv,
        local_offer_sdp=local_offer,
        local_capabilities=HA_AUDIO_CAPABILITIES,
    )
    assert selected is not None
    assert (
        selected.send.encoding,
        selected.send.sample_rate,
        selected.send.channels,
        selected.send.frame_ms,
    ) == ("L16", 8000, 1, 32)
    assert (
        selected.recv.encoding,
        selected.recv.sample_rate,
        selected.recv.channels,
    ) == ("L16", 8000, 1)
    assert selected.send.payload_type == mapping.payload_type
    assert selected.recv.frame_ms != selected.send.frame_ms


@pytest.mark.parametrize("encoding, rate", [("L24", 32000), ("L16", 48000)])
@pytest.mark.parametrize("minimum", [0, 10], ids=["soft-preference", "hard-minimum"])
def test_pcm_packet_budget_can_choose_below_10ms_only_without_a_hard_minimum(
    encoding,
    rate,
    minimum,
):
    selected = _negotiate(
        _peer_sdp(
            encoding,
            rate,
            2,
            15,
            min_frame_ms=minimum,
            max_frame_ms=20,
        )
    )
    if minimum:
        assert selected is None, (
            "the smallest allowed packet exceeds the local TX budget"
        )
        return
    assert selected is not None, (
        "ptime is advisory, so a shorter packet can satisfy the wire budget"
    )
    assert (
        selected.send.encoding,
        selected.send.sample_rate,
        selected.send.channels,
    ) == (encoding, rate, 2)
    assert 0 < selected.send.frame_ms <= 6.25
    pcm = bytes(selected.send.audio_format.nominal_frame_bytes)
    payload = RtpPayloadEncoder(selected.send).encode(pcm)
    assert len(payload) <= UDP_SAFE_PAYLOAD_BYTES
    assert (
        rtp.parse_packet(rtp.build_packet(rtp.RtpPacket(110, 1, 0, 1, payload))).payload
        == payload
    )


def test_offer_maxptime_is_safe_for_every_receivable_mapping():
    preferred = [AudioFormat(8000, frame_ms=32)]
    hint = sdp.RtpPcmFormat(111, "L16", 192000, 2, 10)
    receive = local_rtp_audio_formats(preferred, hints=(hint,), receive=True)
    assert any((fmt.sample_rate, fmt.channels) == (192000, 2) for fmt in receive)
    offer = sdp.build_offer_directional(
        "192.0.2.1",
        "192.0.2.1",
        40000,
        [],
        [fmt.audio_format for fmt in receive],
        recv_rtp_formats=receive,
        audio_direction="recvonly",
    )
    parsed = sdp.parse_sdp(offer)
    maximum = parsed["maxptime"]
    assert 0 < parsed["ptime"] <= maximum <= 16
    for fmt in sdp.offered_pcm_formats(offer):
        assert maximum <= HA_AUDIO_CAPABILITIES.max_receive_packet_ms(fmt)
        width = {"L16": 2, "L24": 3, "PCMA": 1, "PCMU": 1, "G722": 1}[fmt.encoding]
        wire_bytes = fmt.sample_rate * maximum * fmt.channels * width // 1000
        assert wire_bytes <= rtp.MAX_AUDIO_RTP_PAYLOAD_BYTES
        # SDP promises this duration for every payload in the media section.
        # Verify that promise through the actual receiver, not only metadata.
        assert RtpAudioReceiver(fmt).decode(fmt.payload_type, bytes(wire_bytes))


@pytest.mark.parametrize("discovery_known", [False, True], ids=["unknown", "known"])
async def test_standard_8000_32ms_reaches_real_udp_listener_callback_and_tears_down(
    socket_enabled,
    discovery_known,
):
    class Peer(asyncio.DatagramProtocol):
        def __init__(self):
            self.messages = asyncio.Queue()

        def datagram_received(self, raw, address):
            self.messages.put_nowait(sip.parse_message(raw))

    accepted = []

    async def on_invite(invite):
        accepted.append(invite)
        body = sdp.build_answer_directional(
            "127.0.0.1",
            "127.0.0.1",
            40000,
            invite.send_format,
            invite.recv_format,
            remote_sdp=invite.remote_sdp,
        )
        return SipInviteResult(200, "OK", answer_sdp=body)

    local_send, local_recv = _local_formats(
        _peer_sdp(port=0) if discovery_known else None
    )
    endpoint = SipUdpEndpoint(
        local_ip="127.0.0.1",
        local_rtp_port=40000,
        supported_formats=local_send,
        supported_send_formats=local_send,
        supported_recv_formats=local_recv,
        on_invite=on_invite,
    )
    loop = asyncio.get_running_loop()
    server, _ = await loop.create_datagram_endpoint(
        lambda: endpoint, local_addr=("127.0.0.1", 0)
    )
    transport = None
    try:
        transport, peer = await loop.create_datagram_endpoint(
            Peer, local_addr=("127.0.0.1", 0)
        )
        listener = server.get_extra_info("sockname")
        address = transport.get_extra_info("sockname")
        endpoint.local_sip_port = listener[1]
        uri = f"sip:ha@127.0.0.1:{listener[1]}"
        call_id = f"generic-pcm-capability-{discovery_known}"
        headers = [
            ("Via", f"SIP/2.0/UDP 127.0.0.1:{address[1]};branch=z9hG4bK-generic;rport"),
            ("From", "<sip:capability-peer@127.0.0.1>;tag=independent"),
            ("To", "<sip:ha@127.0.0.1>"),
            ("Call-ID", call_id),
            ("CSeq", "1 INVITE"),
            ("Content-Type", "application/sdp"),
            ("Contact", f"<sip:capability-peer@127.0.0.1:{address[1]}>"),
        ]

        async def final_response(cseq):
            async with asyncio.timeout(2):
                while True:
                    message = await peer.messages.get()
                    if message.status_code >= 200 and message.header("CSeq") == cseq:
                        return message

        offer = _peer_sdp().replace("192.0.2.2", "127.0.0.1")
        transport.sendto(
            sip.build_request("INVITE", uri, headers, offer.encode()), listener
        )
        response = await final_response("1 INVITE")
        assert response.status_code == 200
        (invite,) = accepted
        assert invite.call_id == call_id
        assert invite.caller_uri.user == "capability-peer"
        assert invite.source_host == "127.0.0.1"
        assert (invite.send_format.sample_rate, invite.send_format.frame_ms) == (
            8000,
            32,
        )
        assert (invite.recv_format.sample_rate, invite.recv_format.frame_ms) == (
            8000,
            20,
        )
        assert sdp.parse_sdp(response.body)["ptime"] == 20
        dialog_headers = [
            (key, value)
            for key, value in headers
            if key not in {"To", "CSeq", "Content-Type"}
        ]
        dialog_headers.append(("To", response.header("To")))
        transport.sendto(
            sip.build_request("ACK", uri, [*dialog_headers, ("CSeq", "1 ACK")]),
            listener,
        )
        transport.sendto(
            sip.build_request("BYE", uri, [*dialog_headers, ("CSeq", "2 BYE")]),
            listener,
        )
        assert (await final_response("2 BYE")).status_code == 200
        assert not endpoint.pending_invites and not endpoint.active_dialogs
    finally:
        if transport is not None:
            transport.close()
        server.close()
        await endpoint.wait_closed()
