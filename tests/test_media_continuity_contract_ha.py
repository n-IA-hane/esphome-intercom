"""Independent witnesses for media contract continuity and RTP epochs."""

from dataclasses import replace
import math
import struct
from types import SimpleNamespace

import pytest

from custom_components.voip_stack.core import rtp, sdp, sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.core.audio_pcm import PcmFrameConverter
from custom_components.voip_stack.core.media import negotiator
from custom_components.voip_stack.dtmf import RtpDtmfDecoder, build_telephone_event_payload, parse_sip_info_duration
from custom_components.voip_stack.endpoint_routing import sip_target_rtp_audio_profile
from custom_components.voip_stack.rtp_audio_receiver import RtpAudioReceiver
from custom_components.voip_stack.sip_listener import SipUdpEndpoint
from custom_components.voip_stack.sip_rtp_bridge import RtpPeer, SipRtpRelay, _PayloadRelayClock
from custom_components.voip_stack.sip_server_state import SipInvite, SipInviteResult, _PendingInvite, answered_media_contract
from custom_components.voip_stack.sip_video_relay import SipVideoRtpRelay, VideoRtpPeer

pytestmark = pytest.mark.ha


def packet(sequence, *, ssrc=1, timestamp=None, payload_type=96, payload=b""):
    return rtp.RtpPacket(payload_type, sequence & 65535,
        (sequence * 320 if timestamp is None else timestamp) & 0xFFFFFFFF, ssrc, payload)


def test_source_can_return_after_an_intervening_media_server_without_replaying_old_audio():
    state = rtp.RtpReceiveState()
    assert state.admit(packet(100))
    assert state.admit(packet(101))
    assert not state.admit(packet(200, ssrc=2))
    assert state.admit(packet(201, ssrc=2)) and state.changed
    assert not state.admit(packet(100))
    assert not state.admit(packet(102))
    assert state.admit(packet(103)) and state.changed and state.ssrc == 1
    assert not state.admit(packet(101))
    assert not state.admit(packet(103))
    assert state.admit(packet(104))


def test_local_event_insertion_survives_half_range_and_complete_sequence_wraps():
    clock = _PayloadRelayClock()
    assert clock.map_sequence(packet(0), 1000) == 1000
    assert clock.map_sequence(packet(10), 1010) == 1010
    clock.inserted_after(10, 7)
    for sequence in range(11, 100000):
        assert clock.map_sequence(packet(sequence), 0) == (1000 + sequence + 7) & 65535
    # A late admitted packet keeps its original place, not arrival order.
    assert clock.map_sequence(packet(99998), 0) == (1000 + 99998 + 7) & 65535


def test_same_clock_alias_and_ten_twenty_ms_packets_do_not_rebase_the_timeline():
    clock = _PayloadRelayClock()
    first = sdp.RtpPcmFormat(96, "L16", 16000, 1, 10)
    second = replace(first, payload_type=110, frame_ms=20)
    clock.observe_format(first)
    assert clock.map(1000, 5000) == 5000
    clock.observe_format(second)
    assert clock.map(1480, 9999) == 5480
    assert clock.map(1320, 9999) == 5320


def test_actual_native_48k_alias_copies_and_repacketizes_without_a_16k_roundtrip():
    primary = sdp.RtpPcmFormat(97, "L16", 16000, 1, 20, max_frame_ms=20)
    native = sdp.RtpPcmFormat(110, "L16", 48000, 1, 20, max_frame_ms=20)
    target = replace(native, payload_type=96, frame_ms=10)
    left = RtpPeer("192.0.2.2", 40000, primary.payload_type, primary.audio_format,
        rtp_format=primary, inbound_rtp_formats=(primary, native))
    right = RtpPeer("192.0.2.3", 41000, target.payload_type, target.audio_format, rtp_format=target)
    relay = SipRtpRelay(left=left, right=right, left_port=42000, right_port=42002)
    sent = []
    relay.right_transport = SimpleNamespace(sendto=lambda data, _addr: sent.append(rtp.parse_packet(data)))
    samples = tuple(round(12000 * math.sin(2 * math.pi * 10000 * index / 48000)) for index in range(1440))
    first = rtp.build_packet(packet(0, payload_type=110, timestamp=0, payload=struct.pack("!480h", *samples[:480])))
    second = rtp.build_packet(packet(1, payload_type=110, timestamp=480, payload=struct.pack("!960h", *samples[480:])))
    relay.handle_packet("left", first, (left.host, left.port))
    assert len(sent) == 1 and sent[0].payload == struct.pack("!480h", *samples[:480])
    relay.handle_packet("left", second, (left.host, left.port))
    assert len(sent) == 3 and b"".join(value.payload for value in sent) == struct.pack("!1440h", *samples)
    assert [(value.timestamp - sent[0].timestamp) & 0xFFFFFFFF for value in sent] == [0, 480, 960]
    relay.handle_packet("left", second, (left.host, left.port))
    assert len(sent) == 3
    for name in ("left_decoder", "right_decoder", "left_encoder", "right_encoder"):
        owner = getattr(relay, name)
        if owner is not None:
            owner.close()


def test_format_transition_retires_real_fragments_without_padding_or_dropping_samples():
    source = AudioFormat(16000, frame_ms=20)
    target = AudioFormat(48000, frame_ms=10)
    converter = PcmFrameConverter(source, target)
    fragment = struct.pack("<160h", *([0] * 80 + [12000] + [0] * 79))
    with pytest.raises(ValueError):
        converter.convert(fragment)
    converted = converter.convert(fragment, final_fragment=True)
    assert len(converted) == 1 and len(converted[0]) == 960
    wave = struct.unpack("<480h", converted[0])
    assert 260 <= max(range(480), key=lambda index: abs(wave[index])) <= 290
    assert 32000 < sum(wave) < 40000
    assert converter.drain_pending_pcm() == b""

    old = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20, max_frame_ms=20)
    new = sdp.RtpPcmFormat(110, "L16", 48000, 1, 10, max_frame_ms=20)
    receiver = RtpAudioReceiver(old, (old, new), output_pcm=target)
    observed = []
    receiver.add_pcm_observer(lambda data, fmt: observed.append((fmt.sample_rate, len(data))))
    ramp = tuple(index * 20 for index in range(480))
    try:
        assert receiver.decode_rtp(packet(0, payload=struct.pack("!160h", *struct.unpack("<160h", fragment)), timestamp=0)) == []
        frames = receiver.decode_rtp(packet(1, payload_type=110, timestamp=480,
            payload=struct.pack("!480h", *ramp)))
        assert len(frames) == 2 and sum(len(value) for value in frames) == 1920
        assert frames[0] == converted[0]
        assert frames[-1] == struct.pack("<480h", *ramp)
        assert not receiver.has_pending_pcm and receiver.last_gap_ms == 0
        assert observed == [(16000, 320), (48000, 960)]
    finally:
        receiver.close()


def test_same_pcm_alias_preserves_a_half_frame_until_the_next_real_fragment():
    first = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20, max_frame_ms=20)
    second = replace(first, payload_type=110, frame_ms=10)
    receiver = RtpAudioReceiver(first, (first, second))
    try:
        assert receiver.decode_rtp(packet(0, timestamp=0, payload=struct.pack("!160h", *range(160))), normalize_frames=True) == []
        result = receiver.decode_rtp(packet(1, timestamp=160, payload_type=110,
            payload=struct.pack("!160h", *range(160, 320))), normalize_frames=True)
        assert result == [struct.pack("<320h", *range(320))]
    finally:
        receiver.close()


@pytest.mark.parametrize("rate,quantum", [(16000, 20), (48000, 10)])
def test_first_shared_event_after_four_thousand_audio_packets_is_not_a_restart(rate, quantum):
    fmt = sdp.RtpPcmFormat(96, "L16", rate, 1, quantum, max_frame_ms=20)
    receiver = RtpAudioReceiver(fmt)
    events = RtpDtmfDecoder((sdp.RtpDtmfFormat(101, rate),), audio_receiver=receiver)
    count = rate * quantum // 1000
    try:
        for sequence in range(4001):
            assert receiver.decode_rtp(packet(sequence, timestamp=sequence * count,
                payload=bytes(count * 2)))
        raw = rtp.build_packet(packet(4001, timestamp=4001 * count, payload_type=101,
            payload=build_telephone_event_payload("5", duration=count, end=True)))
        event = events.decode_report(raw)
        assert event is not None and event.first and event.shared_audio and not event.source_changed
        assert events.decode_report(raw) is None
    finally:
        receiver.close()


def test_rendered_comfort_noise_is_not_added_again_as_an_rtp_gap():
    fmt = sdp.RtpPcmFormat(96, "L16", 8000, 1, 20, max_frame_ms=20)
    cn = sdp.RtpPcmFormat(13, "CN", 8000, 1, 20)
    receiver = RtpAudioReceiver(fmt, (fmt, cn))
    try:
        assert receiver.decode_rtp(packet(1, timestamp=0, payload_type=13, payload=b"\x7f"))
        for _tick in range(5):
            assert receiver.take_comfort_noise()
        assert receiver.decode_rtp(packet(2, timestamp=960, payload=bytes(320)))
        assert receiver.last_gap_ms == 0
    finally:
        receiver.close()


def test_partial_options_remains_advisory_without_inventing_transmit_quanta():
    body = ("v=0\r\no=- 1 1 IN IP4 192.0.2.2\r\ns=Partial\r\nc=IN IP4 192.0.2.2\r\nt=0 0\r\n"
        "m=audio 0 RTP/AVP 96\r\na=rtpmap:96 L16/16000/1\r\na=ptime:10\r\na=sendrecv\r\n")
    facts = sdp.options_capabilities(body)
    assert facts["sip_audio_tx_formats"] == ["L16/16000/1"]
    assert "sip_audio_tx_geometry_complete" not in facts
    profile = sip_target_rtp_audio_profile(SimpleNamespace(metadata={"sip_capabilities_known": True, **facts}))
    assert {fmt.encoding for fmt in profile.send_rtp_formats} >= {"L16", "PCMU", "PCMA"}
    assert all(not value.times.fixed for value in profile.evidence.sends)


def test_uas_activation_installs_the_exact_committed_contract_without_negotiating_again(monkeypatch):
    wide = sdp.RtpPcmFormat(96, "L16", 48000, 1, 10)
    narrow = sdp.RtpPcmFormat(97, "L16", 16000, 1, 20)
    original = SipInvite("192.0.2.2", 5060, sip.parse_sip_uri("sip:ha@192.0.2.1"), None,
        "ha", "peer", "contract", "1 INVITE", b"original", wide, narrow, "192.0.2.2", 40000)
    committed = replace(original, recv_format=replace(narrow, frame_ms=10),
        send_formats=(wide, narrow), recv_formats=(narrow, wide), max_forwards=49)
    request = sip.parse_message(sip.build_request("INVITE", "sip:ha@192.0.2.1", [
        ("From", "<sip:peer@192.0.2.2>;tag=peer"), ("To", "<sip:ha@192.0.2.1>"),
        ("Call-ID", "contract"), ("CSeq", "1 INVITE")], b"original"))
    pending = _PendingInvite(request, ("192.0.2.2", 5060), "ha", "UDP",
        identity=original, media_contract=committed)
    endpoint = SipUdpEndpoint(local_ip="192.0.2.1", local_rtp_port=42000,
        supported_formats=[AudioFormat(16000)], on_invite=lambda _invite: None)
    monkeypatch.setattr(endpoint, "_parse_invite", lambda *_args, **_kwargs: pytest.fail("second media decision"))
    active = endpoint._activate_dialog("contract", pending, request, pending.addr)
    assert active.invite.recv_format == committed.recv_format
    assert active.invite.send_formats == committed.send_formats
    assert active.invite.recv_formats == committed.recv_formats
    assert active.invite.max_forwards == 49


def test_changed_legacy_answer_requires_its_explicit_contract_before_any_send():
    fmt = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20, max_frame_ms=20)
    offer = ("v=0\r\no=- 1 1 IN IP4 192.0.2.2\r\ns=Gate\r\nc=IN IP4 192.0.2.2\r\nt=0 0\r\n"
        "m=audio 40000 RTP/AVP 96\r\na=rtpmap:96 L16/16000/1\r\na=ptime:20\r\na=maxptime:20\r\na=sendrecv\r\n")
    request = sip.parse_message(sip.build_request("INVITE", "sip:ha@192.0.2.1", [
        ("From", "<sip:peer@192.0.2.2>;tag=peer"), ("To", "<sip:ha@192.0.2.1>"),
        ("Call-ID", "gate"), ("CSeq", "1 INVITE")], offer.encode()))
    original = SipInvite("192.0.2.2", 5060, sip.parse_sip_uri("sip:ha@192.0.2.1"), None,
        "ha", "peer", "gate", "1 INVITE", offer.encode(), fmt, fmt, "192.0.2.2", 40000,
        send_formats=(fmt,), recv_formats=(fmt,))
    pending = _PendingInvite(request, ("192.0.2.2", 5060), "ha", "UDP", identity=original)
    endpoint = SipUdpEndpoint(local_ip="192.0.2.1", local_rtp_port=42000,
        supported_formats=[AudioFormat(16000)], on_invite=lambda _invite: None)
    unchanged = sdp.build_answer_directional("192.0.2.1", "192.0.2.1", 42000,
        fmt, fmt, recv_formats=(fmt,), remote_sdp=offer)
    assert endpoint._stage_answer_contract(pending, unchanged)
    new_receive = replace(fmt, frame_ms=10)
    changed = replace(original, recv_format=new_receive, recv_formats=(new_receive,))
    answer = sdp.build_answer_directional("192.0.2.1", "192.0.2.1", 42000,
        fmt, new_receive, recv_formats=(new_receive,), remote_sdp=offer)
    assert not endpoint._stage_answer_contract(pending, answer)
    assert endpoint._stage_answer_contract(pending, answer, changed)
    assert pending.media_contract is changed


def test_local_video_projection_disables_rejected_sections_without_changing_audio():
    fmt = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20, max_frame_ms=20)
    video = sdp.RtpVideoFormat(payload_type=26, encoding="JPEG")
    offer = ("v=0\r\no=- 1 1 IN IP4 192.0.2.2\r\ns=Video\r\nc=IN IP4 192.0.2.2\r\nt=0 0\r\n"
        "m=audio 40000 RTP/AVP 96\r\na=rtpmap:96 L16/16000/1\r\na=ptime:20\r\na=sendrecv\r\n"
        "m=video 40002 RTP/AVP 26\r\na=rtpmap:26 JPEG/90000\r\na=sendrecv\r\n")
    original = SipInvite("192.0.2.2", 5060, sip.parse_sip_uri("sip:ha@192.0.2.1"), None,
        "ha", "peer", "video", "1 INVITE", offer.encode(), fmt, fmt, "192.0.2.2", 40000,
        video_format=video, send_formats=(fmt,), recv_formats=(fmt,))
    answer = sdp.build_answer_directional("192.0.2.1", "192.0.2.1", 42000,
        fmt, fmt, recv_formats=(fmt,), remote_sdp=offer)
    chosen = SipInviteResult(200, "OK", answer_sdp=answer, media_contract=original).media_contract
    assert "m=video 0 RTP/AVP 26" in answer and chosen.video_format is None
    assert chosen.send_format is original.send_format and chosen.recv_format is original.recv_format
    assert chosen.send_formats is original.send_formats and chosen.recv_formats is original.recv_formats
    enabled = sdp.build_answer_directional("192.0.2.1", "192.0.2.1", 42000,
        fmt, fmt, recv_formats=(fmt,), remote_sdp=offer,
        video_port=42002, video_format=video, video_direction="recvonly")
    active = answered_media_contract(original, enabled)
    assert active.video_answer_format.direction == "recvonly"
    assert active.recv_video_format.direction == "sendonly"


def test_answer_serializes_local_opus_receiver_parameters_instead_of_peer_limits():
    peer = sdp.RtpPcmFormat(111, "OPUS", 48000, 2, 20, fmtp="maxaveragebitrate=64000")
    native = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20, max_frame_ms=20)
    local = replace(peer, fmtp="stereo=0;sprop-stereo=0;maxaveragebitrate=28000", max_frame_ms=20)
    answer = sdp.build_answer_directional("192.0.2.1", "192.0.2.1", 42000,
        peer, native, recv_formats=(native, local))
    assert "maxaveragebitrate=28000" in answer and "64000" not in answer


def test_explicit_keyframe_request_rearms_an_exhausted_bounded_budget(monkeypatch):
    fmt = sdp.RtpVideoFormat(payload_type=103, encoding="H264", rtcp_feedback=("nack pli",))
    left = VideoRtpPeer("192.0.2.2", 40000, 40001, fmt)
    right = VideoRtpPeer("192.0.2.3", 41000, 41001, fmt)
    left.rx_ssrc = 99
    relay = SipVideoRtpRelay(left=left, right=right, left_port=42000, right_port=42002)
    sent = []
    relay._transports[("left", True)] = SimpleNamespace(sendto=lambda data, addr: sent.append((data, addr)))
    relay._keyframe_attempts["left"] = (99, 3, 10.0)
    monkeypatch.setattr("custom_components.voip_stack.sip_video_relay.monotonic", lambda: 10.6)
    assert relay.request_keyframe("left")
    assert len(sent) == 1
    assert relay.request_keyframe("left")
    assert len(sent) == 1


def test_browser_loopback_contract_and_info_duration_are_real_not_wire_codec_defaults():
    browser = negotiator.browser_media_contract()
    assert browser.send.sample_rate == browser.recv.sample_rate == 48000
    assert browser.send_formats == (browser.send,) and browser.recv_formats == (browser.recv,)
    assert browser.recv.max_frame_ms >= 20
    assert browser.dtmf_direction.send.sample_rate == 48000
    assert parse_sip_info_duration(b"Signal=5\r\nDuration=900\r\n") == 900
    assert parse_sip_info_duration(b"Signal=5\r\n") == 160


def test_media_control_info_and_unknown_compound_feedback_preserve_the_existing_video_owner():
    assert sip.is_picture_fast_update("application/media_control+xml", sip.RFC5168_PICTURE_FAST_UPDATE_BODY)
    assert not sip.is_picture_fast_update("application/dtmf-relay", b"Signal=5\r\n")
    assert not sip.is_picture_fast_update("application/media_control+xml", b"<invalid/>")
    fmt = sdp.RtpVideoFormat(payload_type=103, encoding="H264", rtcp_feedback=("ccm fir",))
    left = VideoRtpPeer("192.0.2.2", 40000, 40001, fmt)
    right = VideoRtpPeer("192.0.2.3", 41000, 41001, fmt)
    right.rx_ssrc = 77
    relay = SipVideoRtpRelay(left=left, right=right, left_port=42000, right_port=42002)
    sent = []
    relay._transports[("right", True)] = SimpleNamespace(sendto=lambda data, addr: sent.append((data, addr)))
    report = struct.pack("!BBHI", 0x80, 201, 1, 33)
    unknown = struct.pack("!BBHII", 0x8F, 205, 2, 33, 0)
    fir = struct.pack("!BBHIIIB3s", 0x84, 206, 4, 33, 0, 77, 1, b"\0\0\0")
    relay.handle_rtcp("left", report + unknown + fir, (left.host, left.rtcp_port))
    assert sent == [(report + fir, (right.host, right.rtcp_port))]
