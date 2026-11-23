"""Production media contracts for the approved standard-SDP consolidation."""

from dataclasses import replace
import math
from time import perf_counter

import numpy as np
import pytest

from custom_components.voip_stack.core import rtp, sdp
from custom_components.voip_stack.core.audio_capabilities import HA_AUDIO_CAPABILITIES
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.core.audio_pcm import PcmFrameConverter
from custom_components.voip_stack.core.media.bridge import payload_relay_compatible
from custom_components.voip_stack.core.media.payload_map import PayloadMap
from custom_components.voip_stack.rtp_audio_receiver import RtpAudioReceiver
from custom_components.voip_stack.sip_rtp_bridge import RtpPeer, SipRtpRelay

pytestmark = pytest.mark.ha


def standard_offer():
    # Independent receiver description: both mappings work both ways. The
    # transmitter may use its native16k/20 while this receiver prefers48k/10.
    return ("v=0\r\no=- 1 1 IN IP4 192.0.2.2\r\ns=Standard peer\r\n"
            "c=IN IP4 192.0.2.2\r\nt=0 0\r\nm=audio 40000 RTP/AVP 96 97\r\n"
            "a=rtpmap:96 L16/48000/1\r\na=rtpmap:97 L16/16000/1\r\n"
            "a=ptime:10\r\na=maxptime:20\r\na=sendrecv\r\n")


def test_standard_multiformat_answer_preserves_independent_native_audio():
    send, receive = [AudioFormat(48000, frame_ms=10)], [AudioFormat(16000, frame_ms=20)]
    negotiated = sdp.negotiate_directional(standard_offer(), send, receive,
                                         local_capabilities=HA_AUDIO_CAPABILITIES)
    assert negotiated is not None
    assert (negotiated.send.sample_rate, negotiated.send.frame_ms) == (48000, 10)
    assert (negotiated.recv.sample_rate, negotiated.recv.frame_ms) == (16000, 20)
    assert {fmt.sample_rate for fmt in negotiated.recv_formats} == {16000, 48000}
    answer = sdp.build_answer_directional("192.0.2.1", "192.0.2.1", 42000,
        negotiated.send, negotiated.recv, recv_formats=negotiated.recv_formats,
        remote_sdp=standard_offer())
    assert "x-voip-stack" not in answer
    assert "m=audio 42000 RTP/AVP 97 96\r\n" in answer
    assert "a=ptime:20\r\n" in answer
    assert "b=RS:0\r\nb=RR:0\r\n" in answer


def test_local_offer_keeps_retired_payload_meanings():
    previous = PayloadMap(receive={96: ("L16", 48000, 1)})
    formats = [AudioFormat(16000, frame_ms=20)]
    offer = sdp.build_offer_directional("192.0.2.1", "192.0.2.1", 42000,
        formats, formats, previous_payloads=previous)
    assert "a=rtpmap:96 L16/16000/1" not in offer


def test_all_accepted_event_aliases_keep_their_own_clock_and_digit_identity():
    import struct
    from custom_components.voip_stack.core.media.negotiator import negotiate_dtmf
    from custom_components.voip_stack.dtmf import RtpDtmfDecoder

    description = standard_offer().replace("RTP/AVP 96 97", "RTP/AVP 96 97 110 111") + (
        "a=rtpmap:110 telephone-event/16000\r\na=fmtp:110 0-15\r\n"
        "a=rtpmap:111 telephone-event/48000\r\na=fmtp:111 0-15\r\n")
    audio = sdp.RtpPcmDirection(sdp.RtpPcmFormat(96, "L16", 48000, 1, 10),
                                sdp.RtpPcmFormat(97, "L16", 16000, 1, 20))
    events = negotiate_dtmf(description, audio=audio)
    assert events.send.sample_rate == 48000 and events.recv.sample_rate == 16000
    assert {item.payload_type: item.sample_rate for item in events.recv_formats} == {110: 16000, 111: 48000}
    decoder = RtpDtmfDecoder(events.recv_formats)

    def event(pt, sequence, timestamp, duration, *, end=False):
        return struct.pack("!BBHII", 0x80, pt, sequence, timestamp, 7) + struct.pack(
            "!BBH", 5, 128 if end else 0, duration)

    first = decoder.decode_report(event(111, 1, 100, 480))
    assert first.first and first.sample_rate == 48000
    assert decoder.decode_report(event(110, 2, 100, 320)) is None
    last = decoder.decode_report(event(111, 3, 100, 960, end=True))
    assert not last.first and last.end and last.sample_rate == 48000
    alternate = decoder.decode_report(event(110, 4, 2000, 320, end=True))
    assert alternate.first and alternate.sample_rate == 16000


def test_uas_parser_preserves_retired_payloads_across_three_generations():
    from custom_components.voip_stack.core import sip
    from custom_components.voip_stack.sip_listener import SipUdpEndpoint
    from custom_components.voip_stack.sip_server_state import _ActiveDialog

    formats = [AudioFormat(16000, frame_ms=20), AudioFormat(48000, frame_ms=10)]
    endpoint = SipUdpEndpoint(local_ip="192.0.2.1", local_rtp_port=42000,
        supported_formats=formats, on_invite=lambda _invite: None)

    def request(sequence, payload, rate):
        body = ("v=0\r\no=- 1 1 IN IP4 192.0.2.2\r\ns=History\r\n"
            "c=IN IP4 192.0.2.2\r\nt=0 0\r\n"
            f"m=audio 40000 RTP/AVP {payload}\r\na=rtpmap:{payload} L16/{rate}/1\r\n"
            "a=ptime:10\r\na=maxptime:20\r\na=sendrecv\r\n")
        return sip.parse_message(sip.build_request("INVITE", "sip:ha@192.0.2.1", [
            ("Via", f"SIP/2.0/UDP 192.0.2.2:5060;branch=z9hG4bK-history-{sequence}"),
            ("From", "<sip:peer@192.0.2.2>;tag=peer"), ("To", "<sip:ha@192.0.2.1>"),
            ("Call-ID", "payload-history"), ("CSeq", f"{sequence} INVITE"),
            ("Contact", "<sip:peer@192.0.2.2>"), ("Content-Type", "application/sdp")], body.encode()))

    initial = request(1, 96, 16000)
    first = endpoint._parse_invite(initial, ("192.0.2.2", 5060))
    assert first is not None
    dialog = _ActiveDialog(request=initial, addr=("192.0.2.2", 5060), to_tag="ha", cseq=1,
        transport="UDP", invite=first)
    endpoint.active_dialogs[first.call_id] = dialog
    second = endpoint._parse_invite(request(2, 97, 48000), ("192.0.2.2", 5060))
    assert second is not None
    dialog.install_invite(second)
    assert 96 not in dialog.audio_payloads.receive
    assert dialog.audio_payloads.receive_history[96] == ("L16", 16000, 1)
    assert endpoint._parse_invite(request(3, 96, 48000), ("192.0.2.2", 5060)) is None
    assert dialog.invite is second
    dialog.install_invite(first)
    assert dialog.audio_payloads.receive_history[97] == ("L16", 48000, 1)


def test_options_receiver_preference_is_not_a_complete_transmit_catalog():
    evidence = sdp.options_capabilities(standard_offer().replace("m=audio 40000", "m=audio 0"))
    assert evidence["sip_audio_tx_formats"] == ["L16/48000/1", "L16/16000/1"]
    assert evidence["sip_audio_rx_formats"] == ["L16/48000/1/10", "L16/16000/1/10"]
    assert "sip_audio_capabilities_complete" not in evidence
    assert "sip_audio_tx_geometry_complete" not in evidence
    assert "sip_audio_rx_geometry_complete" not in evidence


@pytest.mark.parametrize("toc,duration", [(0xF8, 20), (0xF9, 40), (0xFB, 60), (0x80, 2.5)])
def test_opus_duration_comes_from_toc_and_frame_count(toc, duration):
    payload = (bytes((toc, 3)) if toc & 3 == 3 else bytes((toc,))) + b"\x01\x02\x03\x04\x05\x06"
    assert rtp.opus_packet_duration_ms(payload) == duration
    fmt = sdp.RtpPcmFormat(111, "OPUS", 48000, 2, 20, max_frame_ms=20)
    if duration > 20:
        with pytest.raises(rtp.RtpError, match="duration"):
            rtp.validate_audio_payload_size(payload, fmt)


@pytest.mark.parametrize("payload", [b"", b"\xf9\x01", b"\xfa\xff", b"\xfb\x00", b"\xfb\x07", b"\xfb\x41\xff"])
def test_malformed_opus_framing_is_rejected_before_a_decoder(payload):
    with pytest.raises(rtp.RtpError):
        rtp.opus_packet_duration_ms(payload)


def test_opus_receiver_preferences_do_not_require_equal_fmtp_strings():
    source = sdp.RtpPcmFormat(111, "OPUS", 48000, 2, 20, max_frame_ms=60,
        fmtp="maxaveragebitrate=24000;maxplaybackrate=16000;usedtx=1;sprop-stereo=0")
    target = replace(source, payload_type=112,
        fmtp="maxaveragebitrate=28000;maxplaybackrate=48000;useinbandfec=1")
    assert payload_relay_compatible(source, target)
    assert not payload_relay_compatible(target, source)


def pcm_packet(sequence, *, timestamp=None, ssrc=7):
    return rtp.RtpPacket(96, sequence, sequence * 160 if timestamp is None else timestamp,
                         ssrc, b"\x00\x10" * 160)


def test_decoder_orders_packets_and_decodes_duplicates_at_most_once():
    fmt = sdp.RtpPcmFormat(96, "L16", 8000, 1, 20, max_frame_ms=20)
    receiver = RtpAudioReceiver(fmt)
    observed = []
    receiver.add_pcm_observer(lambda pcm, audio: observed.append(pcm))
    assert len(receiver.decode_rtp(pcm_packet(0))) == 1
    assert receiver.decode_rtp(pcm_packet(2)) == []
    assert len(receiver.decode_rtp(pcm_packet(1))) == 2
    assert receiver.decode_rtp(pcm_packet(1)) == []
    assert len(observed) == 3
    receiver.close()


def test_source_transition_needs_two_consecutive_valid_packets():
    state = rtp.RtpReceiveState()
    assert state.admit(pcm_packet(0))
    assert not state.admit(pcm_packet(20, ssrc=8))
    assert not state.admit(pcm_packet(22, ssrc=8))
    assert state.admit(pcm_packet(23, ssrc=8)) and state.changed
    assert not state.admit(pcm_packet(1, ssrc=7))


class Transport:
    def __init__(self):
        self.sent = []

    def sendto(self, data, address):
        self.sent.append(rtp.parse_packet(data))


def test_encoded_relay_preserves_sequence_loss_reorder_and_timestamps():
    fmt = sdp.RtpPcmFormat(96, "L16", 8000, 1, 20, max_frame_ms=20)
    left = RtpPeer("192.0.2.2", 40000, 96, fmt.audio_format, rtp_format=fmt)
    right = RtpPeer("192.0.2.3", 41000, 97, fmt.audio_format,
                    rtp_format=replace(fmt, payload_type=97), sequence=100, timestamp=1000)
    relay = SipRtpRelay(left=left, right=right, left_port=42000, right_port=42002)
    relay.right_transport = Transport()
    for sequence in (0, 2, 1, 2, 4):
        relay.handle_packet("left", rtp.build_packet(pcm_packet(sequence)), (left.host, left.port))
    assert [packet.sequence for packet in relay.right_transport.sent] == [100, 102, 101, 104]
    assert [packet.timestamp for packet in relay.right_transport.sent] == [1000, 1320, 1160, 1640]
    assert relay.left_decoder is None and relay.right_encoder is None


@pytest.mark.parametrize("source_rate,target_rate,stop_hz", [
    (48000, 8000, 7000), (48000, 16000, 15000), (16000, 8000, 7000)])
def test_streaming_downsampling_rejects_independent_out_of_band_signal(
    source_rate, target_rate, stop_hz, record_property,
):
    source, target = AudioFormat(source_rate, frame_ms=20), AudioFormat(target_rate, frame_ms=20)

    def convert(frequency):
        converter = PcmFrameConverter(source, target)
        count = source_rate // 2
        signal = (np.sin(2 * np.pi * frequency * np.arange(count) / source_rate) * 12000).astype("<i2")
        chunks = []
        started = perf_counter()
        for offset in range(0, len(signal), source.nominal_frame_samples):
            chunks.extend(converter.convert(signal[offset:offset + source.nominal_frame_samples].tobytes()))
        cpu = perf_counter() - started
        output = np.frombuffer(b"".join(chunks), dtype="<i2").astype(float)
        assert len(output) == target_rate // 2
        return output[target_rate // 20:], cpu

    passed, pass_cpu = convert(1000)
    rejected, stop_cpu = convert(stop_hz)
    alias_db = 20 * math.log10(max(np.sqrt(np.mean(rejected ** 2)), 1e-9) / np.sqrt(np.mean(passed ** 2)))
    record_property("alias_db", alias_db)
    record_property("cpu_seconds", pass_cpu + stop_cpu)
    assert alias_db < -55


def test_rtp_dtmf_bridge_keeps_progress_duration_and_end_without_repeating_ivr_callback():
    from custom_components.voip_stack.dtmf import build_telephone_event_payload

    fmt = sdp.RtpPcmFormat(96, "L16", 8000, 1, 20, max_frame_ms=20)
    left = RtpPeer("192.0.2.2", 40000, 96, fmt.audio_format, rtp_format=fmt,
                   dtmf_payload_type=101, dtmf_clock_rate=8000)
    right = RtpPeer("192.0.2.3", 41000, 97, fmt.audio_format,
                    rtp_format=replace(fmt, payload_type=97), dtmf_payload_type=110,
                    dtmf_clock_rate=8000, send_dtmf_clock_rate=16000,
                    send_dtmf_payload_type=111)
    callbacks = []
    relay = SipRtpRelay(left=left, right=right, left_port=42000, right_port=42002,
                       on_dtmf=lambda *args: callbacks.append(args))
    relay.right_transport = Transport()
    for sequence, duration, ended in ((1, 400, False), (2, 8000, False), (3, 9600, True), (4, 9600, True)):
        incoming = rtp.RtpPacket(101, sequence, 1000, 7,
            build_telephone_event_payload("5", duration=duration, end=ended), marker=sequence == 1)
        relay.handle_packet("left", rtp.build_packet(incoming), (left.host, left.port))
    packets = relay.right_transport.sent
    assert callbacks == [("left", "5", "rtp_event")]
    assert [int.from_bytes(packet.payload[2:4], "big") for packet in packets] == [800, 16000, 19200, 19200]
    assert [bool(packet.payload[1] & 128) for packet in packets] == [False, False, True, True]
    assert {packet.payload_type for packet in packets} == {111}
    assert len({packet.timestamp for packet in packets}) == 1


def test_mixed_video_transcoding_keeps_feedback_for_the_relayed_direction():
    from custom_components.voip_stack.core import video_rtcp
    from custom_components.voip_stack.sip_video_relay import SipVideoRtpRelay, VideoRtpPeer

    class RtcpTransport:
        def __init__(self):
            self.sent = []

        def sendto(self, data, address):
            self.sent.append(data)

    video = sdp.RtpVideoFormat(payload_type=102, encoding="H264")
    left = VideoRtpPeer("192.0.2.2", 40000, 40001, video, rx_ssrc=7)
    right = VideoRtpPeer("192.0.2.3", 41000, 41001, replace(video, payload_type=103), rx_ssrc=8)
    relay = SipVideoRtpRelay(left=left, right=right, left_port=42000, right_port=42002)
    relay._transcode.directions = {"left"}
    transport = RtcpTransport()
    relay._transports[("right", True)] = transport
    feedback = video_rtcp.build_pli(7, 8)
    sender = video_rtcp.build_sender_compound(7, None, ntp_seconds=1, ntp_fraction=0,
        rtp_timestamp=1000, packet_count=2, octet_count=2000)
    relay.handle_rtcp("left", sender + feedback, (left.host, left.rtcp_port))
    assert len(transport.sent) == 1
    packets = video_rtcp.parse_compound(transport.sent[0])
    assert [packet.packet_type for packet in packets] == [201, 202, 206]
    assert packets[0].fmt == 0 and packets[0].payload == (7).to_bytes(4, "big")
    assert packets[-1].payload == (7).to_bytes(4, "big") + (8).to_bytes(4, "big")


@pytest.mark.parametrize("source_rate,target_rate", [(8000, 48000), (16000, 48000), (8000, 16000)])
def test_inverse_rate_conversion_preserves_duration_and_passband(source_rate, target_rate, record_property):
    source, target = AudioFormat(source_rate, frame_ms=20), AudioFormat(target_rate, frame_ms=20)
    converter = PcmFrameConverter(source, target)
    sample_count = source_rate // 2
    samples = (12000 * np.sin(2 * np.pi * 1000 * np.arange(sample_count) / source_rate)).astype("<i2")
    output = []
    started = perf_counter()
    for offset in range(0, sample_count, source.nominal_frame_samples):
        output.extend(converter.convert(samples[offset:offset + source.nominal_frame_samples].tobytes()))
    decoded = np.frombuffer(b"".join(output), dtype="<i2").astype(float)
    record_property("cpu_seconds", perf_counter() - started)
    assert len(decoded) == target_rate // 2
    stable = decoded[target_rate // 20:]
    level = np.sqrt(np.mean(stable ** 2))
    assert abs(20 * math.log10(level / (12000 / math.sqrt(2)))) < 0.5
    time = np.arange(len(stable)) / target_rate
    fundamental = abs(np.dot(stable, np.exp(-2j * np.pi * 1000 * time)))
    image = abs(np.dot(stable, np.exp(-2j * np.pi * (source_rate - 1000) * time)))
    image_db = 20 * math.log10(max(image, 1e-9) / fundamental)
    record_property("image_db", image_db)
    assert image_db < -55


def test_standard_options_geometry_constrains_a_contact_without_inventing_transmit_quanta():
    from types import SimpleNamespace
    from custom_components.voip_stack.endpoint_routing import sip_target_rtp_audio_profile

    capabilities = sdp.options_capabilities(standard_offer().replace("m=audio 40000", "m=audio 0"))
    entry = SimpleNamespace(metadata={"sip_capabilities_known": True, **capabilities})
    profile = sip_target_rtp_audio_profile(entry)
    evidence = profile.evidence
    assert not evidence.complete
    assert not evidence.send_geometry_complete and not evidence.receive_geometry_complete
    assert all(not item.times.fixed for item in evidence.sends)
    assert all(not item.times.fixed for item in evidence.receives)
    assert all(item.times.maximum_ms == 20 for item in evidence.receives)
    assert {fmt.sample_rate for fmt in profile.send_rtp_formats} >= {16000, 48000}
    assert {fmt.sample_rate for fmt in profile.recv_rtp_formats} >= {16000, 48000}
    assert {fmt.encoding for fmt in profile.send_rtp_formats} >= {"L16", "PCMU", "PCMA"}
    assert profile.send_rtp_formats[0].sample_rate == 48000
    assert profile.recv_rtp_formats[0].sample_rate == 16000
    assert all(HA_AUDIO_CAPABILITIES.supports_rtp(fmt) for fmt in profile.send_rtp_formats)


def test_unobserved_hardware_hints_cannot_grant_or_remove_a_sip_codec():
    from types import SimpleNamespace
    from custom_components.voip_stack.endpoint_routing import sip_target_rtp_audio_profile
    from custom_components.voip_stack.core.codec_policy import CodecPolicy

    entry = SimpleNamespace(metadata={"endpoint_kind": "esphome", "tx_formats": ["16000:s16le:1:20"],
        "sip_audio_tx_formats": ["L16/16000/1/20"], "sip_audio_rx_formats": ["L16/48000/1/10"]})
    profile = sip_target_rtp_audio_profile(entry, codec_policy=CodecPolicy(audio=("PCMU",)))
    assert {fmt.encoding for fmt in profile.send_rtp_formats} == {"PCMU"}
    assert {fmt.encoding for fmt in profile.recv_rtp_formats} == {"PCMU"}


def test_bridge_preserves_the_destination_receiver_rate_when_source_is_narrower():
    from types import SimpleNamespace
    from custom_components.voip_stack.endpoint_routing import bridge_audio_profile, sip_target_rtp_audio_profile

    capabilities = sdp.options_capabilities(standard_offer().replace("m=audio 40000", "m=audio 0"))
    profile = sip_target_rtp_audio_profile(SimpleNamespace(metadata={"sip_capabilities_known": True, **capabilities}))
    source = sdp.RtpPcmFormat(110, "L16", 16000, 1, 20)
    optimized = bridge_audio_profile(SimpleNamespace(recv_format=source), profile)
    assert optimized.send_rtp_formats[0].sample_rate == 48000
    assert optimized.recv_rtp_formats[0].sample_rate == 16000


def test_long_dtmf_clock_conversion_preserves_elapsed_time_across_segments():
    from custom_components.voip_stack.dtmf import build_telephone_event_payload

    source = sdp.RtpPcmFormat(96, "L16", 8000, 1, 20, max_frame_ms=20)
    target = sdp.RtpPcmFormat(97, "L16", 48000, 1, 10, max_frame_ms=20)
    left = RtpPeer("192.0.2.2", 40000, 96, source.audio_format, rtp_format=source,
                   dtmf_payload_type=101, dtmf_clock_rate=8000)
    right = RtpPeer("192.0.2.3", 41000, 97, target.audio_format, rtp_format=target,
                    dtmf_payload_type=110, dtmf_clock_rate=48000)
    callbacks = []
    relay = SipRtpRelay(left=left, right=right, left_port=42000, right_port=42002,
                       on_dtmf=lambda *args: callbacks.append(args))
    relay.right_transport = Transport()
    for sequence, duration, ended in ((1, 400, False), (2, 40000, False), (3, 48000, True)):
        packet = rtp.RtpPacket(101, sequence, 1000, 7,
            build_telephone_event_payload("5", duration=duration, end=ended), marker=sequence == 1)
        relay.handle_packet("left", rtp.build_packet(packet), (left.host, left.port))
    packets = relay.right_transport.sent
    elapsed = [((packet.timestamp - packets[0].timestamp) & 0xFFFFFFFF)
               + int.from_bytes(packet.payload[2:4], "big") for packet in packets]
    assert max(elapsed) == 6 * 48000
    assert sum(bool(packet.payload[1] & 128) for packet in packets) == 1
    assert callbacks == [("left", "5", "rtp_event")]
    assert len({packet.timestamp for packet in packets}) > 1


def test_segmented_input_press_triggers_ivr_only_once():
    from custom_components.voip_stack.dtmf import RtpDtmfDecoder, build_telephone_event_payload

    decoder = RtpDtmfDecoder(101)
    results = []
    for sequence, timestamp, duration, ended in ((1, 1000, 65000, False),
        (2, 1000, 65535, False), (3, 66535, 8000, False), (4, 66535, 16000, True)):
        packet = rtp.RtpPacket(101, sequence, timestamp, 7,
            build_telephone_event_payload("5", duration=duration, end=ended), marker=sequence == 1)
        results.append(decoder.decode_report(rtp.build_packet(packet)))
    assert [report.first for report in results] == [True, False, False, False]
    assert results[-1].duration == 65535 + 16000
    assert {report.timestamp for report in results} == {1000}


async def test_reversing_existing_relay_retains_sources_and_recording_roles():
    from custom_components.voip_stack.sip_rtp_bridge import _RelayProtocol

    class OwnedTransport(Transport):
        def __init__(self, protocol):
            super().__init__()
            self.protocol = protocol

        def get_protocol(self):
            return self.protocol

        def is_closing(self):
            return False

        def close(self):
            pass

    fmt = sdp.RtpPcmFormat(96, "L16", 8000, 1, 20, max_frame_ms=20)
    left = RtpPeer("192.0.2.2", 40000, 96, fmt.audio_format, rtp_format=fmt)
    right = RtpPeer("192.0.2.3", 41000, 96, fmt.audio_format, rtp_format=fmt)
    relay = SipRtpRelay(left=left, right=right, left_port=42000, right_port=42002)
    relay.left_transport = OwnedTransport(_RelayProtocol(relay, "left"))
    relay.right_transport = OwnedTransport(_RelayProtocol(relay, "right"))
    observed = []
    detach = relay.observe_audio(lambda side, pcm, audio: observed.append(side))
    try:
        relay.handle_packet("left", rtp.build_packet(pcm_packet(0)), (left.host, left.port))
        receive_owner = relay._receive_states["left"]
        async with relay.media_reconfiguration_guard():
            relay.reverse_held_sides()
        assert relay.right is left and relay._receive_states["right"] is receive_owner
        assert relay.right_port == 42000 and relay.right_transport.get_protocol().side == "right"
        relay.handle_packet("right", rtp.build_packet(pcm_packet(1)), (left.host, left.port))
        assert observed == ["left", "right"]
    finally:
        detach()
        await relay.stop()


async def test_audio_reorder_deadline_flushes_a_dtx_tail_once_through_its_callback():
    import asyncio
    from time import monotonic

    fmt = sdp.RtpPcmFormat(96, "L16", 8000, 1, 20, max_frame_ms=20)
    receiver = RtpAudioReceiver(fmt)
    native, ready = [], []
    delivered = asyncio.Event()
    receiver.add_pcm_observer(lambda pcm, _format: native.append(pcm))

    def on_ready(frames):
        ready.extend(frames)
        delivered.set()

    receiver.set_reorder_callback(on_ready)
    try:
        first = receiver.decode_rtp(pcm_packet(0), normalize_frames=True)
        assert len(first) == 1 and len(native) == 1
        started = monotonic()
        assert receiver.decode_rtp(pcm_packet(2), normalize_frames=True) == []
        deadline = receiver.pending_reorder_deadline
        assert started + .05 <= deadline <= monotonic() + .07
        assert receiver.decode_rtp(pcm_packet(3), normalize_frames=True) == []
        assert receiver.pending_reorder_deadline == deadline
        await asyncio.wait_for(delivered.wait(), .5)
        assert monotonic() >= deadline
        assert len(ready) == 2 and len(native) == 3
        assert receiver.pending_reorder_deadline is None
        assert receiver.decode_rtp(pcm_packet(2), normalize_frames=True) == []
        assert len(native) == 3
    finally:
        receiver.close()


async def test_reorder_close_cancels_old_generation_without_repeating_native_observers():
    import asyncio

    fmt = sdp.RtpPcmFormat(96, "L16", 8000, 1, 20, max_frame_ms=20)
    previous = RtpAudioReceiver(fmt)
    native = []
    previous.add_pcm_observer(lambda pcm, _format: native.append(pcm))
    delivered = asyncio.Event()
    previous.set_reorder_callback(lambda _frames: delivered.set())
    previous.decode_rtp(pcm_packet(0))
    previous.decode_rtp(pcm_packet(2))
    prepared = RtpAudioReceiver(fmt, previous=previous)
    try:
        previous.close()
        assert previous.pending_reorder_deadline is None
        prepared.decode_rtp(pcm_packet(10, ssrc=8))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(delivered.wait(), .15)
        assert len(native) == 2
    finally:
        previous.close()
        prepared.close()


async def test_encoded_relay_keeps_no_decode_reorder_timer_for_packet_gaps():
    fmt = sdp.RtpPcmFormat(96, "L16", 8000, 1, 20, max_frame_ms=20)
    left = RtpPeer("192.0.2.2", 40000, 96, fmt.audio_format, rtp_format=fmt)
    right = RtpPeer("192.0.2.3", 41000, 97, fmt.audio_format,
                    rtp_format=replace(fmt, payload_type=97))
    relay = SipRtpRelay(left=left, right=right, left_port=42000, right_port=42002)
    relay.right_transport = Transport()
    for sequence in (0, 2):
        relay.handle_packet("left", rtp.build_packet(pcm_packet(sequence)), (left.host, left.port))
    assert relay.left_decoder is None and relay.left_to_right is None
    assert len(relay.right_transport.sent) == 2
