from __future__ import annotations

import importlib.util
from pathlib import Path
from xml.etree import ElementTree

import pytest


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


def sdp_pdml(*descriptions):
    root = ElementTree.Element("pdml")
    for source, fields in descriptions:
        packet = ElementTree.SubElement(root, "packet")
        network = ElementTree.SubElement(packet, "proto", name="ip")
        ElementTree.SubElement(network, "field", name="ip.src", show=source)
        sdp = ElementTree.SubElement(packet, "proto", name="sdp")
        for name, value in fields:
            ElementTree.SubElement(sdp, "field", name=name, show=value)
    return ElementTree.tostring(root, encoding="unicode")


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
        stdout = sdp_pdml(("192.0.2.1", [
            ("sdp.media", "audio 40000 RTP/AVP 97"),
            ("sdp.media_attr", "rtpmap:97 L16/16000/1"),
            ("sdp.media", "video 40002 RTP/AVP 103"),
            ("sdp.media_attr", "rtpmap:103 H264/90000"),
        ]))

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


def test_contiguous_opus_packets_cannot_hide_sender_cpu_starvation(monkeypatch, tmp_path):
    """The generic S3 regression lost PCM before RTP sequence numbers existed."""
    from types import SimpleNamespace

    monkeypatch.setattr(MODULE.shutil, "which", lambda _: "tshark")
    interval = 0.063420327

    def capture_result(command, **kwargs):
        if command[command.index("-Y") + 1] == "sdp":
            return SimpleNamespace(stdout=sdp_pdml(("192.0.2.1", [
                ("sdp.media", "audio 40000 RTP/AVP 96"),
                ("sdp.media_attr", "rtpmap:96 opus/48000/2"),
                ("sdp.media_attr", "ptime:20"),
            ])))
        return SimpleNamespace(stdout="\n".join(
            row(index * interval, 100 + index, 960 * index)
            for index in range(285)
        ))

    monkeypatch.setattr(MODULE.subprocess, "run", capture_result)
    streams = MODULE.analyze_pcap(tmp_path / "call.pcap")
    assert streams[0]["lost_packets"] == 0
    assert streams[0]["clock_rate"] == 48000
    assert streams[0]["expected_delta_ms"] == 20.0
    assert MODULE.evaluate_streams(
        streams, require_streams=1, max_audio_loss=0,
        max_video_loss=None, max_cadence_ratio=1.02,
    ) == ["192.0.2.1:40000->192.0.2.2:40002 cadence 3.171x negotiated clock"]

    interval = 0.02
    assert not MODULE.evaluate_streams(
        MODULE.analyze_pcap(tmp_path / "call.pcap"),
        require_streams=1, max_audio_loss=0,
        max_video_loss=None, max_cadence_ratio=1.02,
    )


@pytest.mark.parametrize("video_first", [False, True])
def test_same_payload_number_in_separate_media_keeps_its_audio_clock(monkeypatch, tmp_path, video_first):
    from types import SimpleNamespace
    audio = [("sdp.media", "audio 40000 RTP/AVP 96"),
             ("sdp.media_attr", "rtpmap:96 L16/16000/1")]
    video = [("sdp.media", "video 40002 RTP/AVP 96"),
             ("sdp.media_attr", "rtpmap:96 H264/90000")]
    data = sdp_pdml(("192.0.2.1", video + audio if video_first else audio + video))
    monkeypatch.setattr(MODULE.subprocess, "run", lambda *_a, **_k: SimpleNamespace(stdout=data))
    assert MODULE._sdp_audio_clock_rates("tshark", tmp_path / "call.pcap") == {
        ("192.0.2.1", 40000, 96): 16000,
    }


def test_multiple_audio_sections_and_rejected_media_keep_independent_payloads(monkeypatch, tmp_path):
    from types import SimpleNamespace
    data = sdp_pdml(("192.0.2.1", [
        ("sdp.media", "audio 40000 RTP/AVP 96"),
        ("sdp.media_attr", "rtpmap:96 L16/16000/1"),
        ("sdp.media", "audio 40002 RTP/AVP 96"),
        ("sdp.media_attr", "rtpmap:96 L16/48000/1"),
        ("sdp.media", "video 0 RTP/AVP 96"),
        ("sdp.media_attr", "rtpmap:96 H264/90000"),
    ]))
    monkeypatch.setattr(MODULE.subprocess, "run", lambda *_a, **_k: SimpleNamespace(stdout=data))
    assert MODULE._sdp_audio_clock_rates("tshark", tmp_path / "call.pcap") == {
        ("192.0.2.1", 40000, 96): 16000, ("192.0.2.1", 40002, 96): 48000,
    }


@pytest.mark.parametrize("conflicting_fields", [
    [("sdp.media", "video 40000 RTP/AVP 96"), ("sdp.media_attr", "rtpmap:96 H264/90000")],
    [("sdp.media_attr", "rtpmap:96 L16/48000/1")],
    [("sdp.media_attr", "rtpmap:96 PCMU/16000/1")],
])
def test_ambiguous_same_tuple_payload_fails_closed(monkeypatch, tmp_path, conflicting_fields):
    from types import SimpleNamespace
    data = sdp_pdml(("192.0.2.1", [
        ("sdp.media", "audio 40000 RTP/AVP 96"),
        ("sdp.media_attr", "rtpmap:96 L16/16000/1"), *conflicting_fields,
    ]))
    monkeypatch.setattr(MODULE.subprocess, "run", lambda *_a, **_k: SimpleNamespace(stdout=data))
    with pytest.raises(ValueError, match="Ambiguous SDP"):
        MODULE._sdp_audio_clock_rates("tshark", tmp_path / "call.pcap")


def test_consistent_repeated_sdp_and_static_audio_clocks_remain_usable(monkeypatch, tmp_path):
    from types import SimpleNamespace
    fields = [("sdp.media", "audio 40000 RTP/AVP 0 8 9 96"),
              ("sdp.media_attr", "rtpmap:96 L16/16000/1")]
    data = sdp_pdml(("192.0.2.1", fields), ("192.0.2.1", fields))
    monkeypatch.setattr(MODULE.subprocess, "run", lambda *_a, **_k: SimpleNamespace(stdout=data))
    assert MODULE._sdp_audio_clock_rates("tshark", tmp_path / "call.pcap") == {
        ("192.0.2.1", 40000, 0): 8000, ("192.0.2.1", 40000, 8): 8000,
        ("192.0.2.1", 40000, 9): 8000, ("192.0.2.1", 40000, 96): 16000,
    }
