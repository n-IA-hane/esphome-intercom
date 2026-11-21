"""Actual optional RTP adapters, independent packing and opt-in ITU oracles."""

import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import types

import av
import numpy as np
import pytest

PACKAGE = "voip_optional_audio_codec_test"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(Path(__file__).parents[1] / "custom_components/voip_stack")]
sys.modules[PACKAGE] = package


def load(name):
    return importlib.import_module(PACKAGE + "." + name)


g726 = load("core.g726_codec")
g723 = load("core.g723_codec")
noise = load("core.comfort_noise")
payload = load("core.rtp_payload")
rtp = load("core.rtp")
registry = load("core.media.codecs")
capability = load("core.codec_capabilities")
policy = load("core.codec_policy")
RtpPcmFormat = load("core.sdp_types").RtpPcmFormat


def pack_codes(codes, bits, *, little=True):
    """Independent RFC 3551 codeword packing, with an AAL2 negative control."""
    if little:
        value = sum(int(code) << (index * bits) for index, code in enumerate(codes))
    else:
        value = 0
        for code in codes:
            value = (value << bits) | int(code)
    return value.to_bytes(len(codes) * bits // 8, "little" if little else "big")


@pytest.mark.parametrize("rate,bits,quantum", [(16, 2, 4), (24, 3, 8), (32, 4, 2), (40, 5, 8)])
def test_g726_packets_have_rfc_packing_and_continue_the_same_codec_state(rate, bits, quantum):
    spec = registry.CODECS[f"G726-{rate}"]
    assert (spec.bits_per_sample, spec.sample_quantum) == (bits, quantum)
    if not capability.codec_available(spec.name):
        pytest.skip("The optional real-time SpanDSP encoder is not installed")
    pcm = np.asarray([int(5000 * np.sin(2 * np.pi * 440 * i / 8000)) for i in range(1600)], dtype="<i2")
    encoder = g726.G726Encoder(spec.name)
    decoder = g726.G726Decoder(spec.name)
    decoded = []
    try:
        for offset in range(0, len(pcm), 160):
            wire = encoder.encode(pcm[offset:offset + 160].tobytes())
            assert len(wire) == 160 * bits // 8
            decoded.append(decoder.decode(wire))
        samples = np.frombuffer(b"".join(decoded), dtype="<i2")
        assert len(samples) == len(pcm)
        assert np.corrcoef(samples[320:], pcm[320:])[0, 1] > .99
    finally:
        encoder.close()
        decoder.close()
    with pytest.raises(RuntimeError, match="closed"):
        encoder.encode(pcm[:160].tobytes())


@pytest.mark.parametrize("bits", [2, 3, 4, 5])
def test_pyav_g726_and_g726le_have_distinct_independently_packed_payloads(bits):
    def encode(name):
        context = av.CodecContext.create(name, "w")
        context.sample_rate, context.layout, context.format = 8000, "mono", "s16"
        context.bit_rate = bits * 8000
        context.open()
        pcm = np.asarray([((i * 997) % 16001) - 8000 for i in range(context.frame_size)], dtype="<i2")
        frame = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = 8000
        return b"".join(bytes(packet) for packet in context.encode(frame)), context.frame_size

    aal2, samples = encode("g726")
    standard, _ = encode("g726le")
    value = int.from_bytes(aal2, "big")
    mask = (1 << bits) - 1
    codes = [(value >> ((samples - index - 1) * bits)) & mask for index in range(samples)]
    assert pack_codes(codes, bits) == standard
    assert pack_codes(codes, bits, little=False) == aal2
    assert standard != aal2


def test_directional_capabilities_do_not_invent_a_realtime_g726_encoder(monkeypatch):
    def unavailable():
        raise RuntimeError("optional SpanDSP is absent")
    monkeypatch.setattr(g726, "_spandsp", unavailable)
    capability.codec_available.cache_clear()
    try:
        assert capability.codec_directions("G726-32") == {"send": False, "receive": True}
        assert capability.codec_directions("G726-40") == {"send": False, "receive": False}
        assert "G726-32" not in capability.common_sip_codecs()
        assert "G726-32" not in policy.available_audio_codecs()
    finally:
        capability.codec_available.cache_clear()


def test_codec_switch_releases_the_native_encoder_predictor():
    if not capability.codec_available("G726-32"):
        pytest.skip("The optional real-time SpanDSP encoder is not installed")
    initial = RtpPcmFormat(96, "G726-32", 8000, 1, 20)
    alternate = RtpPcmFormat(0, "PCMU", 8000, 1, 20)
    encoder = payload.RtpPayloadEncoder(initial, send_formats=(initial, alternate), match_received_codec=True)
    native = encoder._codec
    assert encoder.follow_received(alternate)
    with pytest.raises(RuntimeError, match="closed"):
        native.encode(bytes(320))
    assert len(encoder.encode(bytes(320))) == 160


def test_g723_real_encoder_uses_whole_30_ms_frames_and_decoder_accepts_sid():
    fmt = RtpPcmFormat(4, "G723", 8000, 1, 60)
    encoder, decoder = payload.RtpPayloadEncoder(fmt), payload.RtpPayloadDecoder(fmt)
    for _ in range(2):
        encoded = encoder.encode(bytes(fmt.audio_format.nominal_frame_bytes))
        assert [len(frame) for frame in g723.g723_frames(encoded)] == [24, 24]
        assert len(decoder.decode(encoded)) == 960
    assert len(decoder.decode(bytes([2, 0, 0, 0]))) == 480
    # RFC 4856 expresses the bitrate restriction in kb/s.
    with pytest.raises(ValueError, match=r"bitrate=6\.3"):
        g723.G723Encoder(fmtp="bitrate=5.3")
    with pytest.raises(ValueError, match="30 ms"):
        g723.G723Encoder(20)


@pytest.mark.parametrize("wire", [b"", b"\x03", b"\x00" * 23, b"\x01" * 19, b"\x02\x00\x00"])
def test_g723_rejects_truncated_and_reserved_packets_before_decode(wire):
    with pytest.raises(ValueError):
        g723.G723Decoder().decode(wire)


def test_cn_level_model_has_real_noise_and_retains_one_sid_until_primary_resumes():
    decoder = noise.ComfortNoiseDecoder(seed=17)
    assert decoder.generate() == bytes(320)
    decoder.update(bytes([40, 127, 127]))
    frames = [decoder.generate() for _ in range(20)]
    samples = np.frombuffer(b"".join(frames), dtype="<i2").astype(float)
    expected_rms = 32768 * 10 ** (-40 / 20)
    assert .95 * expected_rms < np.sqrt(np.mean(samples * samples)) < 1.05 * expected_rms
    assert len(set(frames)) == len(frames)
    decoder.reset()
    assert decoder.generate() == bytes(320)
    encoder = noise.ComfortNoiseEncoder()
    assert encoder.encode(bytes(320)) == b"\x7f"
    assert encoder.encode(np.full(160, 1000, dtype="<i2").tobytes()) == bytes([31])


@pytest.mark.parametrize("wire", [b"", b"\x80", b"\x40\xff", bytes(257)])
def test_cn_rejects_invalid_levels_coefficients_and_unbounded_models(wire):
    with pytest.raises(ValueError):
        noise.ComfortNoiseDecoder().decode(wire)


def test_cn_is_auxiliary_and_new_primary_codecs_do_not_change_saved_defaults():
    assert registry.CODECS["CN"].auxiliary
    assert registry.STATIC_RTPMAP[13] == ("CN", 8000, 1)
    assert registry.STATIC_RTPMAP[4] == ("G723", 8000, 1)
    assert policy.CodecPolicy.from_config({}).audio == policy.DEFAULT_AUDIO_CODECS
    assert len(policy.DEFAULT_AUDIO_CODECS) == 7
    primary = RtpPcmFormat(0, "PCMU", 8000, 1, 20)
    cn = RtpPcmFormat(13, "CN", 8000, 1, 20)
    encoder = payload.RtpPayloadEncoder(primary, send_formats=(primary, cn), match_received_codec=True)
    assert not encoder.follow_received(cn) and encoder.fmt == primary
    optional = RtpPcmFormat(4, "G723", 8000, 1, 30)
    assert policy.CodecPolicy().audio_formats((optional, primary, cn)) == (primary,)
    with pytest.raises(ValueError, match="primary"):
        policy.CodecPolicy.from_config({"audio_codecs": ["CN"]}, prefix="")


def test_rtp_envelopes_use_codeword_quanta_and_tagged_frame_sizes():
    for rate, octets in ((16, 40), (24, 60), (32, 80), (40, 100)):
        fmt = RtpPcmFormat(96, f"G726-{rate}", 8000, 1, 20)
        assert rtp.audio_payload_size_limit(fmt) == octets
        rtp.validate_audio_payload_size(bytes(octets), fmt)
    with pytest.raises(rtp.RtpError, match="quantum"):
        rtp.audio_payload_size_limit(RtpPcmFormat(96, "G726-24", 8000, 1, .5))
    with pytest.raises(rtp.RtpError, match="quantum"):
        rtp.validate_audio_payload_size(bytes(59), RtpPcmFormat(96, "G726-24", 8000, 1, 20))
    fmt = RtpPcmFormat(4, "G723", 8000, 1, 30)
    assert rtp.audio_payload_size_limit(fmt) == 24
    rtp.validate_audio_payload_size(bytes([1]) + bytes(19), fmt)
    rtp.validate_audio_payload_size(bytes([2, 0, 0, 0]), fmt)
    with pytest.raises(rtp.RtpError, match="Reserved"):
        rtp.validate_audio_payload_size(bytes([3]), fmt)
    rtp.validate_audio_payload_size(bytes([40]), RtpPcmFormat(13, "CN", 8000, 1, 0))


def reference_root():
    root = os.environ.get("VOIP_CODEC_REFERENCE_DIR")
    if not root:
        pytest.skip("Set VOIP_CODEC_REFERENCE_DIR to the private verified ITU/FATE corpus")
    root = Path(root)
    metadata = json.loads((Path(__file__).parent / "fixtures/audio_codec_sources.json").read_text())
    for filename, digest in metadata["files"].items():
        assert hashlib.sha256((root / filename).read_bytes()).hexdigest() == digest
    return root


def itu_values(path):
    values = bytes.fromhex(path.read_text().rstrip("\x1a\n\r"))
    assert sum(values[:-1]) % 255 == values[-1]
    return values[:-1]


@pytest.mark.parametrize("rate", [16, 24, 32, 40])
def test_optin_itu_encoder_codewords_and_independent_decoder_audio(rate):
    root = reference_root() / "itu-g726/DISK1"
    g711 = load("core.g711")
    samples = g711.ulaw_to_s16le(itu_values(root / "INPUT/NRM.M"))
    codes = itu_values(root / f"RESET/{rate}/RN{rate}FM.I")
    expected = np.frombuffer(g711.ulaw_to_s16le(itu_values(root / f"RESET/{rate}/RN{rate}FM.O")), dtype="<i2")
    encoder, decoder = g726.G726Encoder(f"G726-{rate}"), g726.G726Decoder(f"G726-{rate}")
    encoded, decoded = [], []
    try:
        for offset in range(0, len(samples), 320):
            packet = encoder.encode(samples[offset:offset + 320].ljust(320, b"\x00"))
            encoded.append(packet)
            decoded.append(decoder.decode(packet))
    finally:
        encoder.close()
        decoder.close()
    actual = np.frombuffer(b"".join(decoded), dtype="<i2")[:len(codes)].astype(float)
    expected_wire = pack_codes(codes, rate // 8)
    assert b"".join(encoded)[:len(expected_wire)] == expected_wire
    expected = expected[:len(actual)].astype(float)
    # ITU output is tandem-adjusted 8-bit mu-law, while this API outputs linear
    # PCM. Its quantization envelope is independent of the codec implementation.
    assert np.corrcoef(actual, expected)[0, 1] > .995
    ratio = np.sqrt(np.mean(actual * actual) / np.mean(expected * expected))
    assert .95 < ratio < 1.05


def test_optin_g723_decoder_reads_independent_fate_5300_and_sid_vectors():
    root = reference_root()
    decoder = g723.G723Decoder()
    assert len(decoder.decode((root / "ineqd53.tco").read_bytes())) == 2 * 480
    dtx = (root / "dtx63b.tco").read_bytes()
    assert len(g723.g723_frames(dtx)) == 11
    assert len(decoder.decode(dtx)) == 11 * 480
