from __future__ import annotations

import importlib.util
from pathlib import Path
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


def test_contiguous_opus_packets_cannot_hide_sender_cpu_starvation(monkeypatch, tmp_path):
    """The generic S3 regression lost PCM before RTP sequence numbers existed."""
    from types import SimpleNamespace

    monkeypatch.setattr(MODULE.shutil, "which", lambda _: "tshark")
    interval = 0.063420327

    def capture_result(command, **kwargs):
        if command[command.index("-Y") + 1] == "sdp":
            return SimpleNamespace(stdout=(
                "192.0.2.1\taudio 40000 RTP/AVP 96"
                "\trtpmap:96 opus/48000/2,ptime:20\n"
            ))
        return SimpleNamespace(stdout="\n".join(
            row(index * interval, 100 + index, 960 * index)
            for index in range(285)
        ))

    monkeypatch.setattr(MODULE.subprocess, "run", capture_result)
    streams = MODULE.analyze_pcap(tmp_path / "call.pcap")
    assert streams[0]["lost_packets"] == 0
    assert streams[0]["clock_rate"] == 48000
    assert streams[0]["expected_delta_ms"] == 20.0
    failures = MODULE.evaluate_streams(
        streams, require_streams=1, max_audio_loss=0,
        max_video_loss=None, max_cadence_ratio=1.02,
    )
    assert failures == [
        "192.0.2.1:40000->192.0.2.2:40002 audio continuity unqualified: sample count unavailable for the negotiated codec",
        "192.0.2.1:40000->192.0.2.2:40002 cadence 3.171x negotiated clock",
    ]

    interval = 0.02
    assert MODULE.evaluate_streams(
        MODULE.analyze_pcap(tmp_path / "call.pcap"),
        require_streams=1, max_audio_loss=0,
        max_video_loss=None, max_cadence_ratio=1.02,
    ) == ["192.0.2.1:40000->192.0.2.2:40002 audio continuity unqualified: sample count unavailable for the negotiated codec"]


def packet(time, sequence, timestamp, pt=96, *, ssrc="0x12345678", frames=320):
    import struct
    payload = struct.pack(">h", sequence % 32767) * frames if pt in (96, 97) else bytes.fromhex("018a0500")
    return "\t".join((str(time), "192.0.2.1", "40000", "192.0.2.2", "40002",
                       ssrc, str(pt), str(sequence), str(timestamp), payload.hex()))


def capture(monkeypatch, tmp_path, rows, *, extra="", pcm_dir=None):
    from types import SimpleNamespace
    monkeypatch.setattr(MODULE.shutil, "which", lambda name: name)

    def run(command, **kwargs):
        if command[command.index("-Y") + 1] == "sdp":
            return SimpleNamespace(stdout=(
                "192.0.2.2\taudio 40002 RTP/AVP 96 97 109"
                "\trtpmap:96 L16/16000/1,rtpmap:97 L16/16000/1,"
                "rtpmap:109 telephone-event/8000,ptime:20" + extra + "\t192.0.2.2\n"
            ))
        return SimpleNamespace(stdout="\n".join(rows))
    monkeypatch.setattr(MODULE.subprocess, "run", run)
    return MODULE.analyze_pcap(tmp_path / "call.pcap", pcm_dir=pcm_dir)


def failures(streams, require=1):
    return MODULE.evaluate_streams(streams, require_streams=require, max_audio_loss=0,
                                   max_video_loss=None, max_cadence_ratio=1.25)


def test_interleaved_event_sequence_positions_are_not_audio_loss(monkeypatch, tmp_path):
    streams = capture(monkeypatch, tmp_path, [packet(0, 100, 0), packet(.01, 101, 0, 109), packet(.02, 102, 320)])
    assert streams[0]["lost_packets"] == 0
    assert streams[0]["audio_missing_samples"] == 0
    assert streams[1]["media_type"] == "telephone-event"
    assert not failures(streams)
    assert failures(streams, require=2) == ["expected at least 2 RTP streams"]


def test_lost_event_with_contiguous_audio_does_not_invent_audio_loss(monkeypatch, tmp_path):
    streams = capture(monkeypatch, tmp_path, [packet(0, 100, 0), packet(.02, 102, 320)])
    assert streams[0]["lost_packets"] == 1
    assert streams[0]["audio_missing_samples"] == 0
    assert not failures(streams)


def test_missing_pcm_is_preserved_even_with_contiguous_sequences(monkeypatch, tmp_path):
    streams = capture(monkeypatch, tmp_path, [packet(0, 100, 0), packet(.04, 101, 640)])
    assert streams[0]["lost_packets"] == 0
    assert streams[0]["audio_missing_samples"] == 320
    assert "missing 320 audio samples" in failures(streams)[0]


def test_reordering_and_both_wraps_with_late_event_remain_contiguous(monkeypatch, tmp_path):
    streams = capture(monkeypatch, tmp_path, [packet(0, 65534, 0xFFFFFEC0), packet(.02, 0, 0),
        packet(.021, 65535, 0xFFFFFEC0, 109), packet(.04, 1, 320)])
    assert streams[0]["lost_packets"] == 0
    assert streams[0]["audio_missing_samples"] == 0
    assert not failures(streams)


def test_payload_switch_uses_one_audio_timeline_and_one_flow(monkeypatch, tmp_path):
    streams = capture(monkeypatch, tmp_path, [packet(0, 100, 0), packet(.02, 101, 320, 97)])
    assert streams[0]["audio_missing_samples"] == 0
    assert streams[1]["audio_timeline_follower"]
    assert not failures(streams)
    assert failures(streams, require=2) == ["expected at least 2 RTP streams"]


def test_event_clock_and_ssrc_are_separate_from_pcm(monkeypatch, tmp_path):
    streams = capture(monkeypatch, tmp_path, [packet(0, 100, 0), packet(.01, 1, 700, 109, ssrc="0x22334455"),
        packet(.02, 101, 320), packet(.03, 2, 700, 109, ssrc="0x22334455")])
    assert streams[0]["audio_missing_samples"] == 0
    assert not failures(streams)


def test_actual_samples_override_advertised_packet_time(monkeypatch, tmp_path):
    streams = capture(monkeypatch, tmp_path, [packet(0, 100, 0, frames=160), packet(.01, 101, 160, frames=160)])
    assert streams[0]["expected_delta_ms"] == 10
    assert streams[0]["cadence_ratio"] == 1
    assert not failures(streams)


def test_audio_clock_change_is_explicitly_unqualified(monkeypatch, tmp_path):
    streams = capture(monkeypatch, tmp_path, [packet(0, 100, 0), packet(.02, 101, 320, 97)],
        extra=",rtpmap:97 L16/8000/1")
    assert streams[0]["audio_missing_packets"] is None
    assert "audio continuity unqualified" in failures(streams)[0]


def test_wav_is_sorted_and_keeps_real_timestamp_holes(tmp_path):
    import shutil
    import struct
    import wave
    import pytest
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg unavailable")
    rows = [packet(.04, 102, 640), packet(0, 100, 0), packet(.041, 102, 640)]
    streams = MODULE.analyze_rows(rows, with_records=True)
    streams[0]["_format"] = {"encoding": "L16", "clock": 16000, "channels": 1, "ptime": 20}
    MODULE._audio_continuity(streams, tmp_path / "pcm")
    assert streams[0]["audio_missing_samples"] == 320
    with wave.open(streams[0]["pcm_wav"]) as source:
        assert source.getnframes() == 960
        assert source.readframes(960) == struct.pack("<h", 100) * 320 + bytes(640) + struct.pack("<h", 102) * 320


def test_unknown_receiver_payload_cannot_become_a_passing_video_stream(monkeypatch, tmp_path):
    streams = capture(monkeypatch, tmp_path, [packet(0, 100, 0, 110)])
    assert streams[0]["media_type"] == "unknown"
    assert any("absent from receiver SDP" in reason for reason in failures(streams))


@pytest.mark.parametrize("scenario", ["continuous", "lost-event", "lost-audio", "wrap-reorder"])
def test_real_pcap_decodes_both_pcm_directions_with_interleaved_events(tmp_path, scenario):
    import socket
    import struct
    import shutil
    import wave
    if not shutil.which("tshark") or not shutil.which("ffmpeg"):
        pytest.skip("Wireshark and FFmpeg required")
    packets = []
    a, b = "192.0.2.1", "192.0.2.2"

    def udp(time, source, target, source_port, target_port, payload):
        header = struct.pack("!HHHH", source_port, target_port, len(payload) + 8, 0)
        ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, len(payload) + 28, 0, 0, 64, 17, 0,
                         socket.inet_aton(source), socket.inet_aton(target))
        packets.append((time, bytes.fromhex("0200000000010200000000020800") + ip + header + payload))

    def sip(first, host, port, tag):
        body = (f"v=0\r\no=- 1 1 IN IP4 {host}\r\ns=oracle\r\nc=IN IP4 {host}\r\nt=0 0\r\n"
                f"m=audio {port} RTP/AVP 96 109\r\na=rtpmap:96 L16/16000/1\r\n"
                "a=rtpmap:109 telephone-event/8000\r\na=fmtp:109 0-15\r\na=ptime:10\r\n")
        return (first + "\r\nVia: SIP/2.0/UDP 192.0.2.1:5060;branch=z9hG4bKtest\r\n"
                "From: <sip:a@192.0.2.1>;tag=a\r\nTo: <sip:b@192.0.2.2>" + tag + "\r\n"
                "Call-ID: pcap-oracle\r\nCSeq: 1 INVITE\r\nContent-Type: application/sdp\r\n"
                f"Content-Length: {len(body)}\r\n\r\n" + body).encode()
    udp(0, a, b, 5060, 5060, sip("INVITE sip:b@192.0.2.2 SIP/2.0", a, 40000, ""))
    udp(.001, b, a, 5060, 5060, sip("SIP/2.0 200 OK", b, 40002, ";tag=b"))
    seq = 65534 if scenario == "wrap-reorder" else 100
    timestamp = 0xFFFFFF60 if scenario == "wrap-reorder" else 0

    def rtp(pt, sequence, ts, ssrc, payload):
        return struct.pack("!BBHII", 0x80, pt, sequence & 65535, ts & 0xFFFFFFFF, ssrc) + payload

    udp(.01, a, b, 40000, 40002, rtp(96, seq, timestamp, 1, struct.pack(">h", 10) * 160))
    for index in range(3):
        if scenario == "lost-event" and index == 1:
            continue
        arrival = .021 if scenario == "wrap-reorder" and index == 2 else .012 + index * .002
        udp(arrival, a, b, 40000, 40002, rtp(109, seq + 1 + index, 100, 1, bytes.fromhex("018a0500")))
    if scenario != "lost-audio":
        udp(.02, a, b, 40000, 40002, rtp(96, seq + 4, timestamp + 160, 1, struct.pack(">h", 20) * 160))
    udp(.03, a, b, 40000, 40002, rtp(96, seq + 5, timestamp + 320, 1, struct.pack(">h", 30) * 160))
    for index in range(3):
        udp(.01 + index * .01, b, a, 40002, 40000,
            rtp(96, 200 + index, index * 160, 2, struct.pack(">h", 40) * 160))
    path = tmp_path / "bidirectional.pcap"
    with path.open("wb") as output:
        output.write(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
        for time, raw in sorted(packets):
            output.write(struct.pack("<IIII", 1, round(time * 1000000), len(raw), len(raw)))
            output.write(raw)
    streams = MODULE.analyze_pcap(path, pcm_dir=tmp_path / "pcm")
    audio = [stream for stream in streams if stream["media_type"] == "audio"]
    assert len(audio) == 2
    assert audio[0]["audio_missing_samples"] == (160 if scenario == "lost-audio" else 0)
    assert audio[1]["audio_missing_samples"] == 0
    assert bool(failures(streams, require=2)) is (scenario == "lost-audio")
    with wave.open(audio[0]["pcm_wav"]) as source:
        middle = bytes(320) if scenario == "lost-audio" else struct.pack("<h", 20) * 160
        assert source.readframes(480) == struct.pack("<h", 10) * 160 + middle + struct.pack("<h", 30) * 160
