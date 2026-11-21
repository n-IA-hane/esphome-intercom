"""Validate the external official ITU oracle before G72640 media qualification."""

from pathlib import Path
import hashlib
import json
import os

import numpy as np
import pytest

from custom_components.voip_stack.core.g726_codec import G726Encoder, G726Decoder
from tests.support.g726_itu_reference import (
    qualified_reference,
    pack_rtp,
    unpack_rtp,
    _g711,
)
from tests.support.shared_codec_peer import measure_tone, tone_pcm

pytestmark = [pytest.mark.ha, pytest.mark.diagnostic_reference]


def reference():
    value = qualified_reference()
    if value is None:
        pytest.skip(
            "G72640 requires the opt-in corpus-validated external ITU reference"
        )
    return value


def itu_values(path):
    raw = bytes.fromhex(path.read_text().rstrip("\x1a\n\r"))
    assert sum(raw[:-1]) % 255 == raw[-1]
    return raw[:-1]


def test_official_reference_and_production_rtp_packing_match_itu_bits(tmp_path):
    model = reference()
    assert model.golden["samples"] == 16384
    assert model.golden["encoder_bit_exact"] and model.golden["decoder_bit_exact"]
    root = Path(os.environ["VOIP_CODEC_REFERENCE_DIR"]) / "itu-g726/DISK1"
    source = itu_values(root / "INPUT/NRM.M")
    codewords = itu_values(root / "RESET/40/RN40FM.I")
    expected_output = itu_values(root / "RESET/40/RN40FM.O")
    pcm = _g711(source, encode=False)
    encoder = G726Encoder("G726-40", 20)
    packets = []
    try:
        for offset in range(0, len(pcm), 320):
            packets.append(encoder.encode(pcm[offset : offset + 320].ljust(320, b"\0")))
    finally:
        encoder.close()
    expected = pack_rtp(codewords)
    actual = b"".join(packets)[: len(expected)]
    assert actual == expected
    assert model.symbols("dec", unpack_rtp(actual)) == expected_output
    aal2 = sum(
        code << ((len(codewords) - index - 1) * 5)
        for index, code in enumerate(codewords)
    ).to_bytes(len(expected), "big")
    assert aal2 != actual
    assert model.symbols("dec", unpack_rtp(aal2)) != expected_output
    (tmp_path / "golden-packing.json").write_text(
        json.dumps(
            {
                "reference": model.provenance,
                "itu_lsb_rtp_bit_exact": True,
                "aal2_negative_control": True,
                "payload_sha256": hashlib.sha256(actual).hexdigest(),
                "samples": len(codewords),
            },
            indent=2,
        )
        + "\n"
    )


@pytest.mark.parametrize("frequency", [450, 650])
def test_itu_reference_arbitrary_tones_match_production_without_ffmpeg_g726(
    frequency, tmp_path
):
    model = reference()
    pcm = tone_pcm(8000, frequency)
    payload = model.encode_pcm(pcm)
    expected = model.decode_payload(payload)
    encoder, decoder = G726Encoder("G726-40", 20), G726Decoder("G726-40", 20)
    actual = []
    encoded = []
    quantized_input = _g711(_g711(pcm, encode=True), encode=False)
    try:
        for offset in range(0, len(payload), 100):
            actual.append(decoder.decode(payload[offset : offset + 100]))
        for offset in range(0, len(quantized_input), 320):
            encoded.append(encoder.encode(quantized_input[offset : offset + 320]))
    finally:
        encoder.close()
        decoder.close()
    assert b"".join(encoded) == payload
    x = np.frombuffer(b"".join(actual), dtype="<i2").astype(float)
    y = np.frombuffer(expected, dtype="<i2").astype(float)
    correlation = float(np.corrcoef(x, y)[0, 1])
    ratio = float(np.sqrt(np.mean(x * x) / np.mean(y * y)))
    assert correlation > 0.995 and 0.95 < ratio < 1.05
    result = {
        "reference": model.provenance,
        "frequency": frequency,
        "encoder_rtp_bit_exact": True,
        "decoder_correlation": correlation,
        "decoder_rms_ratio": ratio,
        "reference_tone": measure_tone(expected, 8000, frequency),
        "production_tone": measure_tone(b"".join(actual), 8000, frequency),
    }
    (tmp_path / f"tone-{frequency}.json").write_text(
        json.dumps(result, indent=2) + "\n"
    )


def test_missing_optin_reference_remains_an_explicit_gap(monkeypatch, tmp_path):
    monkeypatch.setenv("VOIP_CODEC_REFERENCE_DIR", str(tmp_path))
    assert qualified_reference() is None


def test_changed_reference_binary_fails_instead_of_becoming_a_skip(
    monkeypatch, tmp_path
):
    root = Path(os.environ.get("VOIP_CODEC_REFERENCE_DIR", ""))
    manifest = root / "itu-stl-g726/reference.json"
    if not manifest.is_file():
        pytest.skip("No opted-in reference manifest to test tamper rejection")
    data = json.loads(manifest.read_text())
    data["binary"]["sha256"] = "0" * 64
    target = tmp_path / "itu-stl-g726"
    target.mkdir()
    (target / "reference.json").write_text(json.dumps(data))
    monkeypatch.setenv("VOIP_CODEC_REFERENCE_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="binary hash mismatch"):
        qualified_reference()
