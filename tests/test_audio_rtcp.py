"""Independent RFC 3550 wire layouts qualify the production audio RTCP owner."""
from __future__ import annotations

import asyncio
import socket
import struct
import time

import pytest

from .voip_phase1_support import sip_rtp_bridge, audio_format, sdp
from custom_components.voip_stack.audio_rtcp import AudioRtcpSession, ReceptionReport, report_interval, audio_rtcp_target


def packets(raw):
    output = []
    while raw:
        first, kind, words = struct.unpack_from("!BBH", raw)
        length = (words + 1) * 4
        assert first >> 6 == 2 and len(raw) >= length
        output.append((kind, first & 31, raw[4:length]))
        raw = raw[length:]
    return output


def header(kind, count, body):
    return struct.pack("!BBH", 0x80 | count, kind, len(body) // 4) + body


def cname(ssrc):
    # Independent golden SDES: SSRC + CNAME("peer") + END + padding.
    return header(202, 1, struct.pack("!I", ssrc) + b"\x01\x04peer\x00\x00")


def test_minimum_interval_uses_rfc_initial_halving_randomization_and_compensation():
    assert report_interval(5, initial=False, random_value=0) == pytest.approx(2.5 / 1.21828)
    assert report_interval(5, initial=False, random_value=1) == pytest.approx(7.5 / 1.21828)
    assert report_interval(5, initial=True, random_value=.5) == pytest.approx(2.5 / 1.21828)


async def test_real_rtp_octets_produce_golden_sr_rr_sdes_lsr_dlsr_and_bye():
    loop = asyncio.get_running_loop()
    local_rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer_rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer_rtcp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for sock in (local_rtp, peer_rtp, peer_rtcp):
        sock.bind(("127.0.0.1", 0))
        sock.setblocking(False)
    # The test obtains a real free port without changing any shared media pool.
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    local_port = probe.getsockname()[1]
    probe.close()
    owner = AudioRtcpSession(local_port=local_port, remote_host="127.0.0.1",
        remote_port=peer_rtcp.getsockname()[1], local_ssrc=0x11223344,
        clock_rate=8000, minimum_interval=5)
    try:
        await owner.start()
        incoming_ssrc = 0xAABBCCDD
        for sequence, timestamp in ((65534, 1000), (65535, 1160), (1, 1480), (1, 1480)):
            raw = struct.pack("!BBHII", 0x80, 8, sequence, timestamp, incoming_ssrc) + bytes(160)
            await loop.sock_sendto(peer_rtp, raw, local_rtp.getsockname())
            actual, _ = await loop.sock_recvfrom(local_rtp, 2048)
            owner.observe_receive(actual)
        for sequence, timestamp in ((100, 8000), (101, 8160)):
            raw = struct.pack("!BBHII", 0x80, 8, sequence, timestamp, 0x11223344) + b"\xff" * 160
            await loop.sock_sendto(local_rtp, raw, peer_rtp.getsockname())
            actual, _ = await loop.sock_recvfrom(peer_rtp, 2048)
            assert actual == raw
            owner.observe_send(actual)
        # Independent peer SR gives the receiver report its LSR/DLSR values.
        peer_sr = header(200, 0, struct.pack("!IIIIII", incoming_ssrc, 0x12345678,
            0x9ABCDEF0, 1480, 3, 480)) + cname(incoming_ssrc)
        await loop.sock_sendto(peer_rtcp, peer_sr, ("127.0.0.1", local_port))
        async with asyncio.timeout(2):
            while owner.reports_received != 1:
                await asyncio.sleep(.001)
        owner.send_report()
        raw, _ = await asyncio.wait_for(loop.sock_recvfrom(peer_rtcp, 4096), 2)
        compound = packets(raw)
        assert [kind for kind, _, _ in compound] == [200, 202]
        kind, count, body = compound[0]
        assert count == 1
        ssrc, seconds, fraction, timestamp, sent, octets = struct.unpack_from("!IIIIII", body)
        assert ssrc == 0x11223344 and sent == 2 and octets == 320
        assert abs(seconds - (int(time.time()) + 2208988800)) <= 1
        assert 8160 <= timestamp < 8160 + 8000
        report_ssrc, loss, highest, jitter, lsr, dlsr = struct.unpack_from("!IIIIII", body, 24)
        assert report_ssrc == incoming_ssrc and loss >> 24 == 0 and loss & 0xFFFFFF == 0
        assert highest == 65537 and jitter >= 0
        assert lsr == 0x56789ABC and dlsr < 65536
        assert compound[1][2][:4] == struct.pack("!I", ssrc)
        # A peer receiver report is decoded without affecting call lifetime.
        peer_rr = header(201, 1, struct.pack("!IIIIIII", incoming_ssrc,
            ssrc, 0x20000002, 101, 16, 0, 0)) + cname(incoming_ssrc)
        await loop.sock_sendto(peer_rtcp, peer_rr, ("127.0.0.1", local_port))
        async with asyncio.timeout(2):
            while owner.reports_received != 2:
                await asyncio.sleep(.001)
        assert owner.peer_report["cumulative_lost"] == 2 and owner.peer_report["fraction_lost"] == 32
        assert owner.peer_report["highest_sequence"] == 101
        owner.update_sender(0x44332211, 16000)
        raw, _ = await asyncio.wait_for(loop.sock_recvfrom(peer_rtcp, 4096), 2)
        previous = packets(raw)
        assert [kind for kind, _, _ in previous] == [200, 202, 203]
        assert previous[-1][2] == struct.pack("!I", ssrc)
        assert owner.sent_packets == owner.sent_octets == 0
        # Separate telephone-event SSRC never replaces the committed audio SSRC.
        owner.observe_send(struct.pack("!BBHII", 0x80, 101, 10, 10, 0x99999999) + bytes(4))
        assert owner.local_ssrc == 0x44332211 and owner.sent_packets == 0
        raw = struct.pack("!BBHII", 0x80, 8, 102, 10000, 0x44332211) + bytes(320)
        await loop.sock_sendto(local_rtp, raw, peer_rtp.getsockname())
        actual, _ = await loop.sock_recvfrom(peer_rtp, 2048)
        owner.observe_send(actual, clock_rate=16000)
        owner.send_report()
        raw, _ = await asyncio.wait_for(loop.sock_recvfrom(peer_rtcp, 4096), 2)
        current = packets(raw)
        ssrc, _seconds, _fraction, _timestamp, sent, octets = struct.unpack_from("!IIIIII", current[0][2])
        assert ssrc == 0x44332211 and sent == 1 and octets == 320
        auxiliary_local, auxiliary_peer = 0x99999999, 0xEEEEEEEE
        for sequence in (5, 7):
            raw = struct.pack("!BBHII", 0x80, 101, sequence, 48000, auxiliary_peer) + b"\x01\x80\x12\xc0"
            await loop.sock_sendto(peer_rtp, raw, local_rtp.getsockname())
            actual, _ = await loop.sock_recvfrom(local_rtp, 2048)
            owner.observe_auxiliary_receive(actual, clock_rate=48000)
            raw = struct.pack("!BBHII", 0x80, 101, sequence, 96000, auxiliary_local) + b"\x01\x80\x12\xc0"
            await loop.sock_sendto(local_rtp, raw, peer_rtp.getsockname())
            actual, _ = await loop.sock_recvfrom(peer_rtp, 2048)
            owner.observe_auxiliary_send(actual, clock_rate=48000)
        owner.send_report()
        raw, _ = await asyncio.wait_for(loop.sock_recvfrom(peer_rtcp, 4096), 2)
        reports = packets(raw)
        assert [kind for kind, _, _ in reports] == [200, 200, 202, 202]
        assert reports[0][1] == 2
        aux_source, aux_loss, aux_highest, *_rest = struct.unpack_from("!IIIIII", reports[0][2], 48)
        assert aux_source == auxiliary_peer and aux_loss & 0xFFFFFF == 1 and aux_highest == 7
        aux_ssrc, _seconds, _fraction, _timestamp, aux_sent, aux_octets = struct.unpack("!IIIIII", reports[1][2])
        assert aux_ssrc == auxiliary_local and aux_sent == 2 and aux_octets == 8
        assert reports[2][2][6:] == reports[3][2][6:]
        # Timer is owned and alive; local close sends one legal compound BYE.
        assert owner.task is not None and not owner.task.done()
        await owner.close()
        raw, _ = await asyncio.wait_for(loop.sock_recvfrom(peer_rtcp, 4096), 2)
        compound = packets(raw)
        assert [kind for kind, _, _ in compound] == [200, 200, 202, 202, 203]
        assert compound[-1][2] == struct.pack("!II", ssrc, auxiliary_local)
        assert owner.task is owner.transport is None
        await owner.close()
    finally:
        await owner.close()
        for sock in (local_rtp, peer_rtp, peer_rtcp):
            sock.close()


def test_receive_only_report_is_rr_and_rejects_malformed_or_other_source_rtcp():
    owner = AudioRtcpSession(local_port=40001, remote_host="192.0.2.1", remote_port=50001,
        local_ssrc=0x11111111, clock_rate=8000, minimum_interval=5)
    owner.observe_receive(struct.pack("!BBHII", 0x80, 8, 1, 100, 0x22222222) + bytes(160))
    compound = packets(owner.build_report())
    assert [kind for kind, _, _ in compound] == [201, 202]
    assert compound[0][1] == 1 and len(compound[0][2]) == 28
    owner.datagram_received(b"invalid", ("192.0.2.1", 50001))
    owner.datagram_received(b"invalid", ("192.0.2.2", 50001))
    assert owner.invalid_packets == 1 and owner.reports_received == 0


@pytest.mark.parametrize("attribute,expected", [("", ("192.0.2.10", 40001)),
    ("a=rtcp:45001 IN IP4 192.0.2.20\r\n", ("192.0.2.20", 45001)),
    ("a=rtcp-mux\r\n", ("192.0.2.10", 40001))])
def test_actual_sdp_parser_projects_separate_audio_rtcp_target(attribute, expected):
    body = ("v=0\r\no=- 1 1 IN IP4 192.0.2.1\r\ns=Audio RTCP\r\n"
        "c=IN IP4 192.0.2.10\r\nt=0 0\r\nm=audio 40000 RTP/AVP 8\r\n"
        "a=rtpmap:8 PCMA/8000\r\n" + attribute)
    assert audio_rtcp_target(body) == expected


async def test_relay_reports_actual_admitted_and_forwarded_audio_on_both_owned_ports():
    loop = asyncio.get_running_loop()
    sockets = []
    def datagram():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.setblocking(False)
        sockets.append(sock)
        return sock
    def pair():
        for _attempt in range(100):
            audio = datagram()
            control = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                control.bind(("127.0.0.1", audio.getsockname()[1] + 1))
            except (OSError, OverflowError):
                audio.close()
                control.close()
                continue
            control.setblocking(False)
            sockets.append(control)
            return audio, control
        raise AssertionError("No adjacent audio RTP/RTCP ports")
    left_rtp, left_rtcp, right_rtp, right_rtcp = [datagram() for _ in range(4)]
    left_pair, right_pair = pair(), pair()
    fmt = audio_format.AudioFormat(8000, frame_ms=20)
    wire = sdp.RtpPcmFormat(8, "PCMA", 8000, 1, 20)
    relay = sip_rtp_bridge.SipRtpRelay(
        left=sip_rtp_bridge.RtpPeer("127.0.0.1", left_rtp.getsockname()[1], 8, fmt,
            rtcp_host="127.0.0.1", rtcp_port=left_rtcp.getsockname()[1], rtcp_minimum_interval=5, rtp_format=wire, send_rtp_format=wire),
        right=sip_rtp_bridge.RtpPeer("127.0.0.1", right_rtp.getsockname()[1], 8, fmt,
            rtcp_host="127.0.0.1", rtcp_port=right_rtcp.getsockname()[1], rtcp_minimum_interval=5, rtp_format=wire, send_rtp_format=wire),
        left_port=left_pair[0].getsockname()[1], right_port=right_pair[0].getsockname()[1])
    relay.adopt_audio_sockets((*left_pair, *right_pair))
    try:
        await relay.start()
        for sequence in (100, 102):
            raw = struct.pack("!BBHII", 0x80, 8, sequence, sequence * 160, 0xAABBCCDD) + b"\xd5" * 160
            await loop.sock_sendto(left_rtp, raw, ("127.0.0.1", relay.left_port))
            forwarded, _ = await asyncio.wait_for(loop.sock_recvfrom(right_rtp, 2048), 2)
            assert forwarded[12:] == raw[12:]
        raw = struct.pack("!BBHII", 0x80, 8, 55, 8800, 0xDDCCBBAA) + b"\xd5" * 160
        await loop.sock_sendto(right_rtp, raw, ("127.0.0.1", relay.right_port))
        forwarded, _ = await asyncio.wait_for(loop.sock_recvfrom(left_rtp, 2048), 2)
        assert forwarded[12:] == raw[12:]
        for side, control, sent, received_ssrc, expected_loss in (
            ("left", left_rtcp, 1, 0xAABBCCDD, 1), ("right", right_rtcp, 2, 0xDDCCBBAA, 0)):
            adapter = relay.audio_rtcp[side]
            adapter.send_report()
            report, address = await asyncio.wait_for(loop.sock_recvfrom(control, 4096), 2)
            assert address[1] == getattr(relay, f"{side}_port") + 1
            values = packets(report)
            assert [kind for kind, _, _ in values] == [200, 202]
            body = values[0][2]
            ssrc, _seconds, _fraction, _timestamp, count, octets = struct.unpack_from("!IIIIII", body)
            assert ssrc == getattr(relay, side).ssrc and count == sent and octets == sent * 160
            source, loss, *_rest = struct.unpack_from("!IIIIII", body, 24)
            assert source == received_ssrc and loss & 0xFFFFFF == expected_loss
        owners = tuple(relay.audio_rtcp.values())
        await relay.stop()
        for control in (left_rtcp, right_rtcp):
            raw, _ = await asyncio.wait_for(loop.sock_recvfrom(control, 4096), 2)
            assert [kind for kind, _, _ in packets(raw)] == [200, 202, 203]
        assert not relay.audio_rtcp and all(owner.closed and owner.task is None for owner in owners)
    finally:
        await relay.stop()
        for sock in sockets:
            sock.close()


def test_reception_jitter_matches_independent_8000hz_arrival_timeline():
    state = ReceptionReport()
    for sequence, timestamp, arrival in ((10, 100, 0.0), (11, 260, .02),
            (12, 420, .05), (13, 580, .07)):
        packet = sip_rtp_bridge.rtp.RtpPacket(8, sequence, timestamp, 0x01020304, bytes(160))
        state.observe(packet, 8000, arrival)
    # One packet arrives10ms late: transit error80ticks; its 1/16 EWMA=5,
    # then a regular interval decays that value to4.6875ticks.
    assert state.jitter == pytest.approx(4.6875)
    assert struct.unpack("!IIIIII", state.block(.08))[3] == 4


def test_malformed_sdes_cannot_commit_a_partial_sender_report():
    owner = AudioRtcpSession(local_port=40001, remote_host="192.0.2.1", remote_port=50001,
        local_ssrc=0x11111111, clock_rate=8000, minimum_interval=5)
    source = 0x22222222
    sr = header(200, 0, struct.pack("!IIIIII", source, 0x12345678, 0x9ABCDEF0, 100, 1, 160))
    bad_sdes = header(202, 1, struct.pack("!I", source) + b"\x01\xffxx")
    owner.datagram_received(sr + bad_sdes, ("192.0.2.1", 50001))
    assert owner.invalid_packets == 1 and owner.reception.last_sr == 0
    owner.datagram_received(sr + cname(source), ("192.0.2.1", 50001))
    assert owner.reception.last_sr == 0x56789ABC and owner.peer_cname == "peer"
    owner.observe_receive(sip_rtp_bridge.rtp.RtpPacket(8, 1, 100, source, bytes(160)))
    assert owner.reception.last_sr == 0x56789ABC


async def test_close_before_queued_bind_owns_and_closes_provided_descriptor():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    owner = AudioRtcpSession(local_port=sock.getsockname()[1], remote_host="127.0.0.1",
        remote_port=59999, local_ssrc=123, clock_rate=16000, minimum_interval=5)
    try:
        starting = asyncio.create_task(owner.start(sock=sock))
        await asyncio.sleep(0)
        await owner.close()
        await asyncio.gather(starting, return_exceptions=True)
        assert sock.fileno() == -1
    finally:
        sock.close()
        await owner.close()


async def test_failed_bind_closes_the_provided_descriptor():
    # An incompatible explicit socket makes the real asyncio bind fail.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    owner = AudioRtcpSession(local_port=sock.getsockname()[1], remote_host="127.0.0.1",
        remote_port=59999, local_ssrc=123, clock_rate=16000, minimum_interval=5)
    try:
        with pytest.raises(ValueError):
            await owner.start(sock=sock)
        assert sock.fileno() == -1
    finally:
        sock.close()
        await owner.close()


async def test_concurrent_factory_retains_the_same_descriptor_and_closes_only_unused_fd():
    from custom_components.voip_stack.audio_rtcp import async_get_or_create
    from custom_components.voip_stack.endpoint_session import EndpointCallSession, TerminationIntent
    sockets = [socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for _ in range(2)]
    for sock in sockets:
        sock.bind(("127.0.0.1", 0))
    session = EndpointCallSession("rtcp-factory", 1)
    arguments = dict(remote_host="127.0.0.1", remote_port=59999, local_ssrc=123,
                     clock_rate=16000, minimum_interval=5)
    rtp_port = sockets[0].getsockname()[1] - 1
    try:
        first, second = await asyncio.gather(*(
            async_get_or_create(None, session, None, rtp_port, rtcp_socket=sock, **arguments)
            for sock in sockets))
        assert first is second and first.bound_socket is sockets[0]
        assert sockets[0].fileno() >= 0 and sockets[1].fileno() == -1
        reused = await async_get_or_create(None, session, None, rtp_port, rtcp_socket=sockets[0], **arguments)
        assert reused is first and sockets[0].fileno() >= 0
    finally:
        await session.terminate(TerminationIntent("test_complete"))
        for sock in sockets:
            sock.close()

@pytest.mark.asyncio
async def test_negotiated_quiet_owner_keeps_fd_counters_and_reenables_without_rebind():
    peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer.bind(("127.0.0.1", 0))
    peer.setblocking(False)
    bound = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    bound.bind(("127.0.0.1", 0))
    bound.setblocking(False)
    owner = AudioRtcpSession(local_port=bound.getsockname()[1], remote_host="127.0.0.1",
        remote_port=peer.getsockname()[1], local_ssrc=1, clock_rate=16000, minimum_interval=5)
    try:
        await owner.start(sock=bound)
        transport = owner.transport
        fd = transport.get_extra_info("socket").fileno()
        owner.set_reporting_allowed(False)
        from custom_components.voip_stack.core.rtp import RtpPacket
        owner.observe_send(RtpPacket(96, 1, 320, 1, b"abcd"))
        owner.send_report(bye=True)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.get_running_loop().sock_recvfrom(peer, 4096), .03)
        owner.set_reporting_allowed(True)
        await asyncio.sleep(0)
        owner.send_report()
        raw, _source = await asyncio.wait_for(asyncio.get_running_loop().sock_recvfrom(peer, 4096), 1)
        assert packets(raw)[0][0] == 200
        assert owner.sent_packets == 1 and owner.transport is transport
        assert transport.get_extra_info("socket").fileno() == fd
        owner.set_reporting_allowed(False)
        await owner.close()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.get_running_loop().sock_recvfrom(peer, 4096), .03)
    finally:
        await owner.close()
        peer.close()


def test_report_disable_uses_existing_sdp_document_and_bandwidth_inheritance():
    from custom_components.voip_stack.core.sdp_document import SdpDocument
    from custom_components.voip_stack.core.sdp_parser import audio_rtcp_allowed
    body = "v=0\r\nb=RS:0\r\nb=RR:0\r\nm=audio 40000 RTP/AVP 0\r\n"
    assert not audio_rtcp_allowed(SdpDocument.parse(body))
    assert audio_rtcp_allowed(body + "b=RR:100\r\n")
    assert not audio_rtcp_allowed("v=0\r\nm=audio 40000 RTP/AVP 0\r\nb=RS:0\r\nb=RR:0\r\n")
