"""Actual libpcap/tshark/SDP plus an independent directional L16 decoder."""

from ipaddress import IPv4Address
import struct

import pytest

from qualification.final_session import AudioWireFormat, decode_pcap_l16


def ipv4_udp(source, destination, sport, dport, body):
    udp = struct.pack("!HHHH", sport, dport, 8 + len(body), 0) + body
    header = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 1, 0, 64, 17, 0,
                         IPv4Address(source).packed, IPv4Address(destination).packed)
    checksum = sum(struct.unpack("!10H", header))
    checksum = (checksum & 65535) + (checksum >> 16)
    checksum = (checksum & 65535) + (checksum >> 16)
    header = header[:10] + struct.pack("!H", (~checksum) & 65535) + header[12:]
    return header + udp


@pytest.mark.parametrize("rate,ptime,shared", [(48000,10,False),(16000,10,False),(16000,16,False),(16000,20,False),(16000,10,True)])
def test_real_pcap_sdp_and_samples_agree_before_wav_is_admitted(tmp_path, rate, ptime, shared):
    packets = []
    for source, destination, port, first in [("192.0.2.1", "192.0.2.2", 40000, "INVITE sip:b@192.0.2.2 SIP/2.0"),
                                           ("192.0.2.2", "192.0.2.1", 40002, "SIP/2.0 200 OK")]:
        sdp = (f"v=0\r\no=- 1 1 IN IP4 {source}\r\ns=test\r\nc=IN IP4 {source}\r\nt=0 0\r\n"
               f"m=audio {port} RTP/AVP 97"+(" 120" if shared else "")+f"\r\na=rtpmap:97 L16/{rate}/1\r\na=ptime:{ptime}\r\n")
        if shared:
            sdp += "a=rtpmap:120 telephone-event/8000\r\n"
        sip = (first + "\r\nVia: SIP/2.0/UDP 192.0.2.1;branch=z9hG4bK-a\r\n"
            "From: <sip:a@192.0.2.1>;tag=a\r\nTo: <sip:b@192.0.2.2>\r\nCall-ID: actual-host-pcap\r\n"
            "CSeq: 1 INVITE\r\nContent-Type: application/sdp\r\n" + f"Content-Length: {len(sdp)}\r\n\r\n" + sdp).encode()
        packets.append((len(packets) * 1000, ipv4_udp(source, destination, 5060, 5060, sip)))
    count = rate * ptime // 1000
    payload = struct.pack(f">{count}h", *([1234] * count))
    for number in range(4):
        seq=number*2 if shared else number
        rtp = struct.pack("!BBHII", 0x80, 97, seq, number * count, 0x12345678) + payload
        packets.append((10000 + number * ptime * 1000, ipv4_udp("192.0.2.1", "192.0.2.2", 40000, 40002, rtp)))
        if shared:
            event=struct.pack("!BBHII",0x80,120|(0x80 if number==0 else 0),seq+1,40,0x12345678)+struct.pack("!BBH",1,0x8a if number==3 else 0x0a,80*(number+1))
            packets.append((15000+number*ptime*1000,ipv4_udp("192.0.2.1","192.0.2.2",40000,40002,event)))
    capture = tmp_path / "actual.pcap"
    with capture.open("wb") as stream:
        stream.write(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 101))
        for microseconds, packet in packets:
            stream.write(struct.pack("<IIII", 1, microseconds, len(packet), len(packet)))
            stream.write(packet)
    selected = {"source": "192.0.2.1:40000", "destination": "192.0.2.2:40002", "ssrc": "0x12345678", "payload_type": 97}
    result = decode_pcap_l16(capture, selected, AudioWireFormat(rate, ptime, 97), tmp_path / "valid.wav", minimum_seconds=.03)
    assert result["rms"] == 1234 and result["samples_per_channel"] == count * 4
    assert result["physical"] is False and result["proof_layer"] == "software"
    assert result["shared_ssrc_sequence_verified"] is shared
    with pytest.raises(ValueError, match="SDP-bound"):
        decode_pcap_l16(capture, selected, AudioWireFormat(16000 if rate == 48000 else 48000, ptime, 97),
                        tmp_path / "wrong-clock.wav", minimum_seconds=.03)
    assert not (tmp_path / "wrong-clock.wav").exists()
    if shared:
        missing=tmp_path/"missing-event.pcap"
        with missing.open("wb") as stream:
            stream.write(struct.pack("<IHHIIII",0xA1B2C3D4,2,4,0,0,65535,101))
            for microseconds,packet in packets[:3]+packets[4:]:
                stream.write(struct.pack("<IIII",1,microseconds,len(packet),len(packet))+packet)
        with pytest.raises(ValueError,match="SDP-bound"):
            decode_pcap_l16(missing,selected,AudioWireFormat(rate,ptime,97),tmp_path/"unknown-lost-pt.wav",minimum_seconds=.03)
        assert not (tmp_path/"unknown-lost-pt.wav").exists()
