"""Real offline STUN/SIP/RTP witnesses for NAT tuples and RFC6263 keepalives."""

from ipaddress import IPv4Address
import struct

import pytest

from .test_rtp_pcap_evidence import MODULE, strict_failures, row


def _udp(source, destination, sport, dport, body):
    payload = struct.pack("!HHHH", sport, dport, 8 + len(body), 0) + body
    return struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(payload), 1, 0, 64, 17, 0,
                       IPv4Address(source).packed, IPv4Address(destination).packed) + payload


def _capture(tmp_path, *, stun="valid", keepalive=b"", header=0x80, marker=0, pt=127,
             keepalive_destination=42002, advertise_keepalive_pt=False):
    packets = []
    local, public, remote, server = "192.0.2.1", "203.0.113.7", "198.51.100.2", "198.51.100.10"
    for index, (local_port, mapped_port) in enumerate(((40000, 51003), (40002, 61001))):
        if stun == "ambiguous_binding":
            mapped_port = 51003
        txid = bytes([index + 1]) * 12
        request = struct.pack("!HHI", 1, 0, 0x2112A442) + txid
        if stun != "missing_request":
            if stun == "malformed_request":
                request = request[:2] + b"\0\x04" + request[4:]
            packets.append(_udp(local, server, local_port, 3478, request))
        port = mapped_port ^ 0x2112
        address = int(IPv4Address(public)) ^ 0x2112A442
        attribute = struct.pack("!HHBBHI", 0x0020, 8, 0, 1, port, address)
        response = struct.pack("!HHI", 0x0101, len(attribute), 0x2112A442)
        response += (b"\xff" * 12 if stun == "wrong_transaction" else txid) + attribute
        packets.append(_udp("198.51.100.11" if stun == "wrong_server" else server,
                            "192.0.2.99" if stun == "wrong_destination" else local,
                            3479 if stun == "wrong_server_port" else 3478,
                            local_port + 2 if stun == "wrong_destination_port" else local_port,
                            response))
    for response, source, destination, connection, audio_port, video_port in (
        (False, local, remote, public, 51003, 61001),
        (True, remote, local, remote, 42000, 42002),
    ):
        extra_pt = " 127" if response and advertise_keepalive_pt else ""
        body = (f"v=0\r\no=- 1 1 IN IP4 {connection}\r\ns=offline\r\nc=IN IP4 {connection}\r\nt=0 0\r\n"
                f"m=audio {audio_port} RTP/AVP 9\r\na=rtpmap:9 G722/8000/1\r\na=ptime:20\r\n"
                f"m=video {video_port} RTP/AVP 105{extra_pt}\r\na=rtpmap:105 H264/90000\r\n")
        first = "SIP/2.0 200 OK" if response else f"INVITE sip:peer@{remote} SIP/2.0"
        message = (f"{first}\r\nVia: SIP/2.0/UDP {local};branch=z9hG4bK-offline\r\n"
                   "From: <sip:a@example.invalid>;tag=a\r\nTo: <sip:b@example.invalid>;tag=b\r\n"
                   "Call-ID: nat-observer@offline.invalid\r\nCSeq: 1 INVITE\r\n"
                   f"Content-Type: application/sdp\r\nContent-Length: {len(body)}\r\n\r\n{body}").encode()
        packets.append(_udp(source, destination, 5060, 5060, message))
    for index in range(3):
        for sport, dport, payload_type, stamp, ssrc in (
            (42000, 40000, 9, index * 160, 1),
            (42002, 40002, 105, index * 1800, 2),
        ):
            packet = struct.pack("!BBHII", 0x80, payload_type, 100 + index, stamp, ssrc) + b"\x55" * 160
            packets.append(_udp(remote, local, sport, dport, packet))
    # Same tuple+SSRC shares sequence space with a negotiated video packet.
    packets.append(_udp(local, remote, 40002, keepalive_destination,
                        struct.pack("!BBHII", header, marker | pt, 200, 9000, 3) + keepalive))
    packets.append(_udp(local, remote, 40002, keepalive_destination,
                        struct.pack("!BBHII", 0x80, 105, 201, 10800, 3) + b"\x55" * 160))
    packets.append(_udp(local, remote, 40002, keepalive_destination,
                        struct.pack("!BBHII", header, marker | pt, 202, 12600, 3) + keepalive))
    path = tmp_path / "stun-nat-keepalive.pcap"
    with path.open("wb") as file:
        file.write(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 101))
        for index, packet in enumerate(packets):
            # Audio and video in each generated pair share one20ms epoch.
            at = index * 20000 if index < 6 else (120000 + (index - 6) // 2 * 20000
                                                 if index < 12 else 200000 + (index - 12) * 20000)
            file.write(struct.pack("<IIII", 1700000000, at, len(packet), len(packet)) + packet)
    return path


def test_paired_stun_inverts_public_receiver_sdp_without_source_assumptions(tmp_path):
    streams = MODULE.analyze_pcap(_capture(tmp_path), sip_ports=(5060,))
    incoming = [stream for stream in streams if stream["destination"].startswith("192.0.2.1:")]
    assert sorted(stream["media_type"] for stream in incoming) == ["audio", "video"]
    assert all(stream["sdp_binding"] == "stun_mapped_receiver_endpoint" for stream in incoming)
    assert {stream["stun_binding"]["mapped_endpoint"] for stream in incoming} == {
        "203.0.113.7:51003", "203.0.113.7:61001",
    }
    assert all(stream["stun_binding"]["transaction_id"] for stream in incoming)
    assert strict_failures(streams) == []


@pytest.mark.parametrize("failure", ["missing_request", "wrong_transaction", "wrong_server", "wrong_server_port",
                                     "wrong_destination", "wrong_destination_port", "malformed_request"])
def test_unpaired_or_noninverse_stun_cannot_invent_receiver_alias(tmp_path, failure):
    streams = MODULE.analyze_pcap(_capture(tmp_path, stun=failure), sip_ports=(5060,))
    assert all(stream["media_type"] == "unknown" for stream in streams
               if stream["destination"].startswith("192.0.2.1:"))
    assert any("unknown SDP" in failure for failure in strict_failures(streams))


def test_exact_empty_dynamic_keepalive_keeps_shared_ssrc_loss_ledger(tmp_path):
    streams = MODULE.analyze_pcap(_capture(tmp_path), sip_ports=(5060,))
    outgoing = [stream for stream in streams if stream["source"] == "192.0.2.1:40002"]
    assert {stream["media_type"] for stream in outgoing} == {"video", "keepalive"}
    assert len({stream["sequence_ledger_id"] for stream in outgoing}) == 1
    ledger = MODULE.sequence_statistics(outgoing[0], streams)
    assert ledger["lost_packets"] == 0 and ledger["packets"] == 3
    assert ledger["payload_types"] == [105, 127]
    assert all(stream["lost_packets"] is None for stream in outgoing)
    keepalive = next(stream for stream in outgoing if stream["media_type"] == "keepalive")
    assert "clock_rate" not in keepalive and "cadence_ratio" not in keepalive


def test_one_public_tuple_cannot_alias_two_distinct_local_sockets(tmp_path):
    with pytest.raises(ValueError, match="Ambiguous STUN"):
        MODULE.analyze_pcap(_capture(tmp_path, stun="ambiguous_binding"), sip_ports=(5060,))


@pytest.mark.parametrize("options", [
    {"keepalive": b"x"}, {"header": 0xA0}, {"header": 0x90}, {"header": 0x81},
    {"marker": 0x80}, {"pt": 95}, {"keepalive_destination": 42999},
])
def test_unknown_payload_or_nonminimal_header_does_not_become_keepalive(tmp_path, monkeypatch, options):
    if "keepalive_destination" in options:
        # An unadvertised port has no SDP-driven RTP dissector. Decode the
        # known fixture RTP explicitly to test the classifier's fail-closed
        # receiver check, rather than accidentally testing invisible packets.
        run = MODULE.subprocess.run
        monkeypatch.setattr(MODULE.subprocess, "run", lambda command, **kwargs:
                            run([command[0], "-d", "udp.port==42999,rtp", *command[1:]], **kwargs))
    streams = MODULE.analyze_pcap(_capture(tmp_path, **options), sip_ports=(5060,))
    assert not any(stream.get("media_type") == "keepalive" for stream in streams)
    assert strict_failures(streams)


def test_legacy_nine_field_rows_remain_unchanged_and_do_not_guess_keepalive():
    assert len(MODULE.FIELDS) == 9
    stream = MODULE.analyze_rows([row(0, 1, 0), row(.02, 2, 320)])[0]
    assert stream["lost_packets"] == 0
    assert "empty_rtp_only" not in stream


def test_advertised_but_unmapped_payload_is_not_a_keepalive(tmp_path):
    streams = MODULE.analyze_pcap(_capture(tmp_path, advertise_keepalive_pt=True), sip_ports=(5060,))
    candidate = next(stream for stream in streams if stream["payload_type"] == 127)
    assert candidate["media_type"] == "unknown"
    assert any("unknown SDP" in reason for reason in strict_failures(streams))


def test_negotiated_video_payload_is_not_classified_as_keepalive(tmp_path):
    streams = MODULE.analyze_pcap(_capture(tmp_path, pt=105), sip_ports=(5060,))
    assert not any(stream["media_type"] == "keepalive" for stream in streams)
    assert all(stream["media_type"] == "video" for stream in streams if stream["source"] == "192.0.2.1:40002")
