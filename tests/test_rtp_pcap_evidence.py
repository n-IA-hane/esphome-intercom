from __future__ import annotations

import importlib.util
from pathlib import Path


PATH = Path(__file__).parents[1] / "tools" / "rtp_pcap_evidence.py"
SPEC = importlib.util.spec_from_file_location("rtp_pcap_evidence", PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def row(time: float, sequence: int, timestamp: int) -> str:
    return "\t".join(
        (
            str(time),
            "192.0.2.1",
            "40000",
            "192.0.2.2",
            "40002",
            "0x12345678",
            "96",
            str(sequence),
            str(timestamp),
        )
    )


def test_continuous_rtp_wrap_reports_no_loss() -> None:
    streams = MODULE.analyze_rows(
        (
            row(0.00, 65534, 1000),
            row(0.02, 65535, 1320),
            row(0.04, 0, 1640),
            row(0.06, 1, 1960),
        )
    )

    assert len(streams) == 1
    assert streams[0]["lost_packets"] == 0
    assert streams[0]["duplicate_packets"] == 0
    assert streams[0]["timestamp_step"] == 320
    assert streams[0]["max_delta_ms"] == 20.0


def test_rtp_gap_and_duplicate_are_reported_independently() -> None:
    streams = MODULE.analyze_rows(
        (
            row(0.00, 10, 1000),
            row(0.02, 11, 1320),
            row(0.03, 11, 1320),
            row(0.06, 13, 1960),
        )
    )

    assert streams[0]["lost_packets"] == 1
    assert streams[0]["duplicate_packets"] == 1
    assert streams[0]["packets"] == 4


def test_sdp_audio_clock_rates_bind_payload_to_advertised_port(
    monkeypatch, tmp_path
) -> None:
    class Result:
        stdout = (
            "192.0.2.1\taudio 40000 RTP/AVP 97,video 40002 RTP/AVP 103"
            "\trtpmap:97 L16/16000/1,rtpmap:103 H264/90000\n"
        )

    monkeypatch.setattr(MODULE.subprocess, "run", lambda *args, **kwargs: Result())

    assert MODULE._sdp_audio_clock_rates("tshark", tmp_path / "call.pcap") == {
        ("192.0.2.1", 40000, 97): 16000,
    }


def test_video_sequence_gap_is_optional_during_direction_change() -> None:
    streams = [
        {
            "source": "192.0.2.1:40000",
            "destination": "192.0.2.2:40002",
            "media_type": "audio",
            "lost_packets": 0,
            "cadence_ratio": 1.0,
        },
        {
            "source": "192.0.2.1:40004",
            "destination": "192.0.2.2:40006",
            "media_type": "video",
            "lost_packets": 20,
        },
    ]

    assert not MODULE.evaluate_streams(
        streams,
        require_streams=2,
        max_audio_loss=0,
        max_video_loss=None,
        max_cadence_ratio=1.25,
    )
    assert MODULE.evaluate_streams(
        streams,
        require_streams=2,
        max_audio_loss=0,
        max_video_loss=0,
        max_cadence_ratio=1.25,
    ) == ["192.0.2.1:40004->192.0.2.2:40006 lost 20 video packets"]
