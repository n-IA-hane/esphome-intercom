"""Behavioral coverage for mixed negotiated RTP receive codecs."""

import struct

import numpy as np
import pytest

from .voip_phase1_support import _load_intercom_module, sdp, sip_client

RtpAudioReceiver = _load_intercom_module("rtp_audio_receiver").RtpAudioReceiver


def fmt(pt, encoding, rate=8000, channels=1, frame_ms=20):
    return sdp.RtpPcmFormat(pt, encoding, rate, channels, frame_ms)


def pcm(value, samples):
    return struct.pack('<h', value) * samples


def test_selected_format_preserves_short_packet_and_decoder():
    selected = fmt(0, 'PCMU')
    receiver = RtpAudioReceiver(selected)
    wire = b'\xff' * 80
    assert receiver.decode(0, wire) == [sip_client.rtp_payload_to_pcm(wire, selected)]
    decoder = receiver._decoders[0]
    receiver.decode(0, wire)
    assert receiver._decoders[0] is decoder
    assert receiver._converter is None


def test_pcm_16k_normalizes_to_selected_pcmu_8k():
    selected, alternate = fmt(0, 'PCMU'), fmt(97, 'PCM', 16000)
    receiver = RtpAudioReceiver(selected, (selected, alternate))
    result = receiver.decode(97, pcm(12000, 320))
    assert len(result) == 1 and len(result[0]) == 320
    # Allow the anti-aliasing filter startup transient, then verify signed gain.
    samples = np.frombuffer(result[0], dtype='<i2')
    assert np.max(np.abs(samples[24:] - 12000)) <= 2
    result = receiver.decode(97, pcm(-12000, 320))
    assert np.max(np.abs(np.frombuffer(result[0], dtype='<i2')[24:] + 12000)) <= 2


def test_recording_observer_gets_native_pcm_before_resampling_and_survives_reoffer():
    selected, alternate = fmt(0, 'PCMU'), fmt(97, 'PCM', 16000)
    receiver = RtpAudioReceiver(selected, (selected, alternate))
    seen = []
    detach = receiver.add_pcm_observer(lambda data, audio_format: seen.append((data, audio_format)))
    source = pcm(12000, 320)
    result = receiver.decode(97, source)
    assert seen == [(source, alternate.audio_format)]
    assert len(result[0]) == 320
    updated = RtpAudioReceiver(alternate, (alternate,), previous=receiver)
    updated.decode(97, source)
    assert len(seen) == 2
    detach()
    updated.decode(97, source)
    assert len(seen) == 2


def test_recording_observer_failure_does_not_drop_call_audio():
    selected = fmt(97, 'PCM', 16000)
    receiver = RtpAudioReceiver(selected)
    calls = []
    def broken(_data, _format):
        calls.append(1)
        raise OSError("synthetic recording failure")
    receiver.add_pcm_observer(broken)
    source = pcm(1000, 320)
    assert receiver.decode(97, source) == [source]
    assert receiver.decode(97, source) == [source]
    assert calls == [1]


def test_recording_started_after_reoffer_preparation_survives_its_commit():
    selected = fmt(97, 'PCM', 16000)
    previous = RtpAudioReceiver(selected)
    prepared = RtpAudioReceiver(selected, previous=previous)
    seen = []
    detach = previous.add_pcm_observer(lambda data, _format: seen.append(data))
    source = pcm(700, 320)
    assert prepared.decode(97, source) == [source]
    assert seen == [source]
    detach()
    prepared.decode(97, source)
    assert seen == [source]


def test_payload_97_l16_uses_network_byte_order_not_vendor_pcm():
    selected, alternate = fmt(0, 'PCMU'), fmt(97, 'L16')
    receiver = RtpAudioReceiver(selected, (alternate,))
    assert receiver.format_for(97) == alternate
    assert receiver.decode(97, struct.pack('>h', 1234) * 160) == [pcm(1234, 160)]
    assert receiver.decode(0, b'\xff' * 160) == [b'\x00' * 320]


def test_short_packets_accumulate_before_resampling_with_bounded_remainder():
    selected, alternate = fmt(0, 'PCMU'), fmt(97, 'PCM', 16000)
    receiver = RtpAudioReceiver(selected, (alternate,))
    frames = []
    for _ in range(101):
        frames.extend(receiver.decode(97, pcm(4000, 80)))
        assert len(receiver._pending) < alternate.audio_format.nominal_frame_bytes
    assert len(frames) == 25
    assert all(len(frame) == 320 for frame in frames)
    assert len(receiver._pending) == 160


def test_long_negotiated_ptime_emits_multiple_selected_frames():
    selected, alternate = fmt(0, 'PCMU', frame_ms=10), fmt(97, 'PCM', 16000)
    receiver = RtpAudioReceiver(selected, (alternate,))
    result = receiver.decode(97, pcm(6000, 320))
    assert len(result) == 2
    assert all(len(frame) == 160 for frame in result)


def test_stereo_alternate_is_downmixed():
    selected, alternate = fmt(0, 'PCMU'), fmt(97, 'L16', channels=2)
    receiver = RtpAudioReceiver(selected, (alternate,))
    assert receiver.decode(97, struct.pack('>hh', 2000, 6000) * 160) == [pcm(4000, 160)]


def test_codec_switch_discards_partial_input_and_partial_output():
    selected, alternate = fmt(0, 'PCMU'), fmt(97, 'PCM', 16000, frame_ms=10)
    receiver = RtpAudioReceiver(selected, (alternate,))
    assert receiver.decode(97, pcm(16000, 160)) == []  # half a destination frame
    assert receiver.decode(97, pcm(16000, 80)) == []  # partial source frame too
    receiver.decode(0, b'\xff' * 80)
    assert not receiver._pending
    assert receiver.decode(97, pcm(0, 160)) == []
    assert receiver.decode(97, pcm(0, 160)) == [bytes(320)]


@pytest.mark.parametrize('pt,payload', [(99, b'1234'), (97, b'x'), (97, b''), (97, bytes(642))])
def test_unnegotiated_or_malformed_payload_rejected(pt, payload):
    receiver = RtpAudioReceiver(fmt(0, 'PCMU'), (fmt(97, 'PCM', 16000),))
    with pytest.raises(ValueError):
        receiver.decode(pt, payload)
    assert not receiver._pending


def test_renegotiation_overlap_expires_and_normalizes(monkeypatch):
    module = _load_intercom_module('rtp_audio_receiver')
    clock = [100.0]
    monkeypatch.setattr(module, 'monotonic', lambda: clock[0])
    old, new = fmt(97, 'PCM', 16000), fmt(0, 'PCMU')
    previous = RtpAudioReceiver(old)
    receiver = RtpAudioReceiver(new, previous=previous)
    assert set(receiver.accepted_formats) == {old, new}
    assert len(receiver.decode(97, pcm(4000, 320))[0]) == 320
    clock[0] = 160.0
    assert receiver.format_for(97) is None
    assert receiver.accepted_formats == (new,)
    with pytest.raises(ValueError):
        receiver.decode(97, pcm(4000, 320))


def test_new_payload_retires_old_immediately():
    old, new = fmt(97, 'PCM', 16000), fmt(0, 'PCMU')
    receiver = RtpAudioReceiver(new, previous=RtpAudioReceiver(old))
    receiver.decode(0, b'\xff' * 160)
    assert receiver.format_for(97) is None


def test_unchanged_payload_does_not_retire_old():
    old, common, new = fmt(97, 'PCM', 16000), fmt(0, 'PCMU'), fmt(8, 'PCMA')
    receiver = RtpAudioReceiver(common, (common, new), previous=RtpAudioReceiver(old, (old, common)))
    receiver.decode(0, b'\xff' * 160)
    assert receiver.format_for(97) == old
    receiver.decode(8, b'\xd5' * 160)
    assert receiver.format_for(97) is None


def test_retired_formats_do_not_propagate_across_generations():
    first, second, third = fmt(97, 'PCM', 16000), fmt(0, 'PCMU'), fmt(8, 'PCMA')
    middle = RtpAudioReceiver(second, previous=RtpAudioReceiver(first))
    newest = RtpAudioReceiver(third, previous=middle)
    assert newest.format_for(97) is None
    assert newest.format_for(0) == second


@pytest.mark.parametrize('new', [fmt(97, 'L16', 16000), fmt(97, 'PCM', 8000), fmt(97, 'PCM', 16000, 2)])
def test_rejects_conflicting_payload_mapping(new):
    with pytest.raises(ValueError, match='mapping changed'):
        RtpAudioReceiver(new, previous=RtpAudioReceiver(fmt(97, 'PCM', 16000)))


def test_passthrough_codec_switch_discards_partial_decoded_input():
    selected, alternate = fmt(0, 'PCMU'), fmt(97, 'PCM', 16000)
    receiver = RtpAudioReceiver(selected, (alternate,))
    assert receiver.decode(97, pcm(10000, 160)) == []
    receiver.observe_payload(0)  # Valid PCMU packet relayed without decoding.
    assert receiver._converter is None
    assert receiver.decode(97, pcm(0, 160)) == []
    assert receiver.decode(97, pcm(0, 160)) == [bytes(320)]


def test_passthrough_new_payload_retires_old_without_codec_allocation():
    previous = RtpAudioReceiver(fmt(97, 'PCM', 16000))
    receiver = RtpAudioReceiver(fmt(0, 'PCMU'), previous=previous)
    receiver.observe_payload(0)
    assert receiver.format_for(97) is None
    assert not receiver._decoders
    assert receiver._converter is None


@pytest.mark.parametrize("packet_samples", [
    (320, 320), (160, 160, 160, 160), (80, 240, 80, 240), (160, 320, 160),
])
def test_short_selected_packets_preserve_continuous_pcm_before_relay_resampling(packet_samples):
    # SDP receiver preference20ms allows a sender's legal shorter packets.
    # The relay's existing16->48k converter still consumes nominal20ms frames.
    selected = fmt(96, "L16", 16000, frame_ms=20)
    receiver = RtpAudioReceiver(selected)
    converter_class = _load_intercom_module("core.audio_pcm").PcmFrameConverter
    destination = _load_intercom_module("core.audio_format").AudioFormat(48000, frame_ms=10)
    converter = converter_class(selected.audio_format, destination)
    reference = converter_class(selected.audio_format, destination)
    samples = np.asarray([2000 + index % 97 for index in range(640)], dtype="<i2")
    actual = []
    offset = 0
    decoder = None
    for count in packet_samples:
        wire = samples[offset:offset + count].astype(">i2").tobytes()
        offset += count
        for frame in receiver.decode(96, wire, normalize_frames=True):
            assert len(frame) == selected.audio_format.nominal_frame_bytes
            actual.extend(converter.convert(frame))
        if decoder is not None:
            assert receiver._decoders[96] is decoder
        decoder = receiver._decoders[96]
        assert len(receiver._pending) < selected.audio_format.nominal_frame_bytes
    expected = reference.convert(samples[:320].tobytes()) + reference.convert(samples[320:].tobytes())
    assert actual == expected and len(actual) == 4
    assert all(len(frame) == 960 for frame in actual)
    assert np.max(np.frombuffer(actual[-1], dtype="<i2")) >= 1900
    assert not receiver.has_pending_pcm and not converter.has_pending_pcm


@pytest.mark.parametrize("source_ms,destination_ms,packets,expected_frames", [(20, 10, 1, 2), (10, 20, 2, 1)])
def test_same_pcm_geometry_reframes_variable_receiver_output(source_ms, destination_ms, packets, expected_frames):
    selected = fmt(96, "L16", 16000, frame_ms=source_ms)
    receiver = RtpAudioReceiver(selected)
    converter_class = _load_intercom_module("core.audio_pcm").PcmFrameConverter
    destination = _load_intercom_module("core.audio_format").AudioFormat(16000, frame_ms=destination_ms)
    converter = converter_class(selected.audio_format, destination)
    assert converter._same_pcm_contract()
    samples = np.asarray([2000 + index for index in range(320)], dtype="<i2")
    result = []
    count = 16000 * source_ms // 1000
    for index in range(packets):
        wire = samples[index * count:(index + 1) * count].astype(">i2").tobytes()
        for frame in receiver.decode(96, wire, normalize_frames=not converter._same_pcm_contract()):
            result.extend(converter.convert(frame))
    assert len(result) == expected_frames
    assert b"".join(result) == samples.tobytes()
    assert not receiver.has_pending_pcm and not converter.has_pending_pcm
