"""Qualify RFC3551 G726 packet names and packing against external ITU audio."""

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess

import numpy as np
import pytest

from custom_components.voip_stack.core.g726_codec import G726Encoder
from tests.support.shared_codec_peer import CASES, audio_sdp, decode_named_rtp

pytestmark = pytest.mark.ha


def itu_values(path):
    values = bytes.fromhex(path.read_text().rstrip("\x1a\n\r"))
    assert sum(values[:-1]) % 255 == values[-1]
    return values[:-1]


def rfc3551_pack(codewords, bits):
    """RFC3551 4.5.4: earlier codewords occupy the low available bits."""
    value = sum(code << (index * bits) for index, code in enumerate(codewords))
    return value.to_bytes(len(codewords) * bits // 8, "little")


def decode_itu_mulaw(value):
    return subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "mulaw",
            "-ar",
            "8000",
            "-ac",
            "1",
            "-i",
            "pipe:0",
            "-f",
            "s16le",
            "pipe:1",
        ],
        input=value,
        capture_output=True,
        check=True,
    ).stdout


@pytest.mark.parametrize("rate", [16, 24, 32])
async def test_spandsp_rfc3551_named_rtp_decodes_to_independent_itu_output(
    rate, tmp_path, socket_enabled
):
    """Actual SpanDSP bytes must decode under G726, and fail under AAL2 names."""
    root = os.environ.get("VOIP_CODEC_REFERENCE_DIR")
    if not root:
        pytest.skip("The verified private ITU corpus is required")
    root = Path(root)
    digests = json.loads(
        (Path(__file__).parent / "fixtures/audio_codec_sources.json").read_text()
    )["files"]
    for filename, digest in digests.items():
        assert hashlib.sha256((root / filename).read_bytes()).hexdigest() == digest
    vectors = root / "itu-g726/DISK1"
    pcm = decode_itu_mulaw(itu_values(vectors / "INPUT/NRM.M"))[:16000]
    codes = itu_values(vectors / f"RESET/{rate}/RN{rate}FM.I")[:8000]
    expected = np.frombuffer(
        decode_itu_mulaw(itu_values(vectors / f"RESET/{rate}/RN{rate}FM.O")),
        dtype="<i2",
    )
    case = next(case for case in CASES if case.name == f"G726-{rate}")
    encoder = G726Encoder(case.name, 20)
    try:
        packets = [
            struct.pack("!BBHII", 0x80, case.payload, index, index * 160, 3571)
            + encoder.encode(pcm[index * 320 : (index + 1) * 320])
            for index in range(50)
        ]
    finally:
        encoder.close()
    assert b"".join(raw[12:] for raw in packets) == rfc3551_pack(codes, rate // 8)
    results = {}
    for named_case, label in (
        (case, "G726"),
        (replace(case, name=f"AAL2-G726-{rate}"), "AAL2"),
    ):
        decoded = await decode_named_rtp(
            named_case, packets, audio_sdp(named_case, 10000), tmp_path / label
        )
        actual = np.frombuffer(decoded, dtype="<i2").astype(float)
        oracle = expected[: len(actual)].astype(float)
        correlation = float(np.corrcoef(actual, oracle)[0, 1])
        ratio = float(np.sqrt(np.mean(actual * actual) / np.mean(oracle * oracle)))
        results[label] = {
            "correlation": correlation,
            "rms_ratio": ratio,
            "samples": len(actual),
        }
    (tmp_path / "result.json").write_text(json.dumps(results, indent=2) + "\n")
    assert results["G726"]["correlation"] > 0.995, results
    assert 0.95 < results["G726"]["rms_ratio"] < 1.05, results
    assert results["AAL2"]["correlation"] < 0.90, results
