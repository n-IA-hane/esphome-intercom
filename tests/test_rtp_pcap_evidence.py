from __future__ import annotations

import importlib.util
import json
from ipaddress import IPv4Address
from pathlib import Path
import struct
import subprocess
import sys
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
        ElementTree.SubElement(sdp, "field", name="sdp.connection_info", show=f"IN IP4 {source}")
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
            return SimpleNamespace(stdout=sdp_pdml(("192.0.2.2", [
                ("sdp.media", "audio 40002 RTP/AVP 96"),
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


@pytest.mark.parametrize("encoding,clock,step", [("L16", 16000, 320), ("G722", 8000, 160), ("opus", 48000, 960)])
def test_telephone_event_repeated_timestamp_is_not_an_audio_cadence(monkeypatch, tmp_path, encoding, clock, step):
    """RFC4733 retains the event-start timestamp across multiple packets."""
    from types import SimpleNamespace

    monkeypatch.setattr(MODULE.shutil, "which", lambda _: "tshark")
    data = sdp_pdml(("192.0.2.2", [
        ("sdp.media", "audio 40002 RTP/AVP 96 120"),
        ("sdp.media_attr", f"rtpmap:96 {encoding}/{clock}/1"),
        ("sdp.media_attr", "rtpmap:120 telephone-event/8000"),
    ]))
    voice = [row(index * .02, 100 + index*2, 1000 + index * step) for index in range(3)]
    events = [row(.005 + index * .02, 101 + index*2, 1000*8000//clock+40).replace("\t96\t", "\t120\t") for index in range(3)]

    def captured(command, **kwargs):
        return SimpleNamespace(stdout=data if command[command.index("-Y") + 1] == "sdp" else "\n".join(sorted(voice + events,key=lambda x:float(x.split('\t')[0]))))

    monkeypatch.setattr(MODULE.subprocess, "run", captured)
    streams = MODULE.analyze_pcap(tmp_path / "mixed.pcap")
    audio = next(stream for stream in streams if stream["payload_type"] == 96)
    event = next(stream for stream in streams if stream["payload_type"] == 120)
    assert audio["media_type"] == "audio" and audio["expected_delta_ms"] == 20
    assert audio["cadence_ratio"] == 1
    assert event["media_type"] == "telephone-event"
    assert event["clock_rate"] == 8000 and event["timestamp_step"] == 0
    assert "cadence_ratio" not in event
    assert not MODULE.evaluate_streams(streams, require_streams=1, max_audio_loss=0,
        max_video_loss=None, max_cadence_ratio=1.25)
    assert MODULE.evaluate_streams([event], require_streams=1, max_audio_loss=0,
        max_video_loss=None, max_cadence_ratio=1.25) == ["expected at least 1 RTP streams", "Missing or repeated RTP sequence ledger"]


def test_zero_advance_voice_timestamp_still_fails_closed(monkeypatch, tmp_path):
    from types import SimpleNamespace

    monkeypatch.setattr(MODULE.shutil, "which", lambda _: "tshark")
    data = sdp_pdml(("192.0.2.2", [("sdp.media", "audio 40002 RTP/AVP 96"),
        ("sdp.media_attr", "rtpmap:96 L16/16000/1")]))
    monkeypatch.setattr(MODULE.subprocess, "run", lambda command, **_kwargs:
        SimpleNamespace(stdout=data if command[command.index("-Y") + 1] == "sdp" else "\n".join(
            row(index * .02, 100 + index, 1000) for index in range(3))))
    with pytest.raises(ValueError, match="Audio RTP timestamp does not advance"):
        MODULE.analyze_pcap(tmp_path / "broken-voice.pcap")


def wire_capture(tmp_path, name, packets, *, signal_source="192.0.2.10",
                 media_override=False, unknown=False, video=False, ignored_application=False):
    """Real offline SIP/SDP/RTP, independent of the production sequence/mapping parser."""
    records = []
    def udp(source, destination, sport, dport, body):
        payload = struct.pack("!HHHH", sport, dport, 8+len(body), 0)+body
        return struct.pack("!BBHHHBBH4s4s",0x45,0,20+len(payload),1,0,64,17,0,
                           IPv4Address(source).packed,IPv4Address(destination).packed)+payload
    for source, destination, media_ip, port, response in (
            (signal_source,"192.0.2.20","192.0.2.1",40000,False),
            ("192.0.2.20",signal_source,"192.0.2.2",40002,True)):
        connection = "192.0.2.99" if media_override else media_ip
        body = f"v=0\r\no=- 1 1 IN IP4 {media_ip}\r\ns=test\r\nc=IN IP4 {connection}\r\nt=0 0\r\n"
        if ignored_application:
            body += "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\nc=IN IP4 192.0.2.99\r\n"
        body += f"m=audio {port} RTP/AVP 9 101 111\r\n"
        if media_override:
            body += f"c=IN IP4 {media_ip}\r\n"
        body += "a=rtpmap:9 G722/8000/1\r\na=rtpmap:101 telephone-event/8000\r\n"
        if not unknown:
            body += "a=rtpmap:111 opus/48000/2\r\n"
        if video:
            body += f"m=video {port+4} RTP/AVP 26 102\r\n"
            if media_override:
                body += f"c=IN IP4 {media_ip}\r\n"
            body += "a=rtpmap:102 H264/90000\r\n"
        first = "SIP/2.0 200 OK" if response else "INVITE sip:b@192.0.2.20 SIP/2.0"
        sip = (first+"\r\nVia: SIP/2.0/UDP "+signal_source+";branch=z9hG4bK-shared-source\r\n"
               "From: <sip:a@example.invalid>;tag=a\r\nTo: <sip:b@example.invalid>\r\n"
               "Call-ID: "+name+"@offline.invalid\r\nCSeq: 1 INVITE\r\nContent-Type: application/sdp\r\n"
               f"Content-Length: {len(body)}\r\n\r\n"+body).encode()
        records.append((len(records)*50000,udp(source,destination,5060,5060,sip)))
    for index, item in enumerate(packets):
        seq, pt, stamp = item[:3]
        opts = item[3] if len(item)>3 else {}
        source, destination = opts.get("source","192.0.2.1"),opts.get("destination","192.0.2.2")
        sport,dport = opts.get("sport",40000),opts.get("dport",40002)
        payload = struct.pack("!BBH",1,0x8a,480) if pt==101 else bytes([0x55])*160
        raw = struct.pack("!BBHII",0x80,pt,seq,stamp,opts.get("ssrc",0x12345678))+payload
        records.append((1000000+index*20000,udp(source,destination,sport,dport,raw)))
    # A valid RTCP receiver report must never join the RTP sequence ledger.
    records.append((1500000,udp("192.0.2.1","192.0.2.2",40001,40003,struct.pack("!BBHI",0x80,201,1,0x12345678))))
    path = tmp_path/(name+".pcap")
    with path.open("wb") as file:
        file.write(struct.pack("<IHHIIII",0xa1b2c3d4,2,4,0,0,65535,101))
        for at,packet in records:
            sec,usec=divmod(at,1000000)
            file.write(struct.pack("<IIII",sec+1700000000,usec,len(packet),len(packet))+packet)
    return path


def strict_failures(streams):
    return MODULE.evaluate_streams(streams,require_streams=1,max_audio_loss=0,
                                   max_video_loss=None,max_cadence_ratio=1.25)


@pytest.mark.parametrize("missing",[False,True])
def test_actual_shared_audio_event_sequence_uses_one_loss_ledger(tmp_path, missing):
    packets=[(100,9,1000),(101,101,1160),(102,101,1160),(103,9,1480),(104,101,1160),(105,9,1800)]
    if missing:
        packets.pop(2)
    path=wire_capture(tmp_path,"shared",packets,signal_source="192.0.2.1")
    streams=MODULE.analyze_pcap(path,sip_ports=(5060,))
    assert {s["media_type"] for s in streams}=={"audio","telephone-event"}
    if not missing:
        assert strict_failures(streams)==[]
    assert all(s["lost_packets"] is None and s["loss_scope"]=="shared_sequence_space" for s in streams)
    ledgers=[s["sequence_ledger"] for s in streams if "sequence_ledger" in s]
    assert len(ledgers)==1 and ledgers[0]["lost_packets"]==int(missing)
    failures=strict_failures(streams)
    assert len(failures)==int(missing)
    if missing:
        assert "payload type unknown" in failures[0] and "audio packets" not in failures[0]
    cli=subprocess.run([sys.executable,str(PATH),str(path),"--output",str(tmp_path/"shared.json"),
                        "--sip-port","5060","--max-loss","0"],capture_output=True,text=True)
    assert cli.returncode==int(missing),cli.stderr
    assert json.loads(cli.stdout)["status"]==("failed" if missing else "passed")


@pytest.mark.parametrize("signal_source,override,nat_source",[
    ("192.0.2.1",False,False),("192.0.2.10",False,False),
    ("192.0.2.10",True,False),("192.0.2.10",False,True)])
def test_real_sdp_receiver_connection_binds_audio_without_assuming_source(tmp_path, signal_source, override, nat_source):
    opts={"source":"192.0.2.77"} if nat_source else {}
    path=wire_capture(tmp_path,"split",[(100,9,1000,opts),(102,9,1320,opts),(103,9,1480,opts)],
                      signal_source=signal_source,media_override=override)
    streams=MODULE.analyze_pcap(path,sip_ports=(5060,))
    assert streams[0]["media_type"]=="audio" and streams[0]["clock_rate"]==8000
    assert streams[0]["lost_packets"]==1
    assert any("lost 1 audio packets" in failure for failure in strict_failures(streams))
    if signal_source=="192.0.2.10" and not override and not nat_source:
        cli=subprocess.run([sys.executable,str(PATH),str(path),"--output",str(tmp_path/"split.json"),
                            "--sip-port","5060","--max-loss","0"],capture_output=True,text=True)
        assert cli.returncode==1 and json.loads(cli.stdout)["streams"][0]["media_type"]=="audio"


def test_unmapped_dynamic_payload_fails_closed_instead_of_becoming_video(tmp_path):
    path=wire_capture(tmp_path,"unknown",[(1,111,0),(3,111,1920)],unknown=True)
    streams=MODULE.analyze_pcap(path,sip_ports=(5060,))
    assert streams[0]["media_type"]=="unknown"
    assert any("unknown" in failure.lower() for failure in strict_failures(streams))


def test_real_bidirectional_sources_ssrc_and_known_video_remain_separate(tmp_path):
    packets=[(1,9,1000),(2,9,1160),(1,9,1000,{"source":"192.0.2.2","destination":"192.0.2.1","sport":40002,"dport":40000}),
             (2,9,1160,{"source":"192.0.2.2","destination":"192.0.2.1","sport":40002,"dport":40000}),
             (10,26,9000,{"sport":40004,"dport":40006}),(11,26,10800,{"sport":40004,"dport":40006})]
    streams=MODULE.analyze_pcap(wire_capture(tmp_path,"bidirectional",packets,video=True),sip_ports=(5060,))
    assert sorted(s["media_type"] for s in streams)==["audio","audio","video"]
    assert all(s["lost_packets"]==0 and s["packets"]==2 for s in streams)
    assert strict_failures(streams)==[]


def test_sequence_reorder_across_wrap_does_not_invent_a_second_cycle():
    streams=MODULE.analyze_rows([row(i*.02,seq,1000+i*320) for i,seq in enumerate((65534,0,65535,1,2))])
    assert streams[0]["lost_packets"]==0
    assert streams[0]["sequence_ledger"]["reordered_packets"]==1


def test_non_rtp_media_connection_does_not_replace_session_inheritance(tmp_path):
    path=wire_capture(tmp_path,"non-rtp",[(1,9,1000),(2,9,1160)],ignored_application=True)
    streams=MODULE.analyze_pcap(path,sip_ports=(5060,))
    assert streams[0]["media_type"]=="audio"
    assert strict_failures(streams)==[]


def test_shared_unknown_payload_cannot_bypass_the_single_source_loss_gate(tmp_path):
    path=wire_capture(tmp_path,"shared-unknown",[(1,9,1000),(3,111,960),(4,9,1480)],unknown=True)
    streams=MODULE.analyze_pcap(path,sip_ports=(5060,))
    failures=strict_failures(streams)
    assert any("unknown SDP" in reason for reason in failures)
    assert len([reason for reason in failures if "lost 1" in reason])==1
