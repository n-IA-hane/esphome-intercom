"""SIP capture lifecycle and actual UDP/TCP boundary witnesses."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import types

import pytest

PKG = "voip_capture_test"
package = types.ModuleType(PKG)
package.__path__ = [str(Path(__file__).parents[1] / "custom_components/voip_stack")]
sys.modules[PKG] = package
capture_module = importlib.import_module(f"{PKG}.sip_capture")
udp = importlib.import_module(f"{PKG}.sip_udp_io")
tcp = importlib.import_module(f"{PKG}.sip_tcp_io")

INVITE = (
    b"INVITE sip:callee@example.test SIP/2.0\r\n"
    b"Via: SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bK-one\r\n"
    b"From: <sip:caller@example.test>;tag=a\r\nTo: <sip:callee@example.test>\r\n"
    b"Call-ID: diagnostic-call\r\nCSeq: 1 INVITE\r\n"
    b'Proxy-Authorization: Digest username="secret-user",\r\n response="secret-digest"\r\n'
    b"Content-Length: 0\r\n\r\n"
)
BYE = INVITE.replace(b"INVITE", b"BYE").replace(b"1 BYE", b"2 BYE")
OK = (
    b"SIP/2.0 200 OK\r\nVia: SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bK-one\r\n"
    b"From: <sip:caller@example.test>;tag=a\r\nTo: <sip:callee@example.test>;tag=b\r\n"
    b"Call-ID: diagnostic-call\r\nCSeq: 2 BYE\r\nContent-Length: 0\r\n\r\n"
)


def packets(data):
    assert struct.unpack_from("<IHHIIII", data)[-1] == 252
    pos = 24
    result = []
    while pos < len(data):
        _seconds, usec, size, original = struct.unpack_from("<IIII", data, pos)
        assert 0 <= usec < 1000000 and size == original
        pos += 16
        result.append(bytes(data[pos : pos + size]))
        pos += size
    assert pos == len(data)
    return result


@pytest.mark.asyncio
async def test_bounds_redaction_restart_expiry_and_disabled_path(monkeypatch):
    c = capture_module.SipCapture()
    c.start(120, max_bytes=4096)
    first_id = c.capture_id
    with pytest.raises(ValueError, match="already running"):
        c.start()
    assert c.capture_id == first_id
    for _ in range(30):
        capture_module.capture_io(
            INVITE,
            types.SimpleNamespace(get_extra_info=lambda _: ("::1", 5060)),
            ("::1", 5061),
        )
    assert not c.active and c.reason == "size_limit"
    assert len(c.buffer) <= 4096 and c.dropped == 1
    assert b"secret-user" not in c.buffer and b"secret-digest" not in c.buffer
    assert b"Proxy-Authorization: [redacted]" in c.buffer
    assert packets(c.buffer)
    expiry = c._expiry
    c.start(120)
    assert expiry.cancelled() and c.capture_id != first_id
    c.stop("duration_limit")
    assert not c.active
    c.clear()
    assert not c.buffer and not c.capture_id
    # Disabled capture does not even query endpoint metadata.
    capture_module.capture_io(INVITE, object(), ("invalid", 0))


@pytest.mark.asyncio
async def test_udp_wire_roundtrip_and_wireshark_decode(tmp_path):
    c = capture_module.SipCapture()
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue(maxsize=8)
    transport, _ = await loop.create_datagram_endpoint(
        lambda: udp.SipDatagramQueueProtocol(queue), local_addr=("127.0.0.1", 0)
    )

    class Peer(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, data, addr):
            assert data == BYE  # Capture redaction must not touch the wire.
            self.transport.sendto(OK, addr)

    peer, _ = await loop.create_datagram_endpoint(Peer, local_addr=("127.0.0.1", 0))
    try:
        c.start()
        capture_module.send_sip_datagram(
            transport, BYE, peer.get_extra_info("sockname")
        )
        raw, _ = await asyncio.wait_for(queue.get(), 2)
        assert raw == OK
        c.stop()
        assert c.messages == 2 and c.dropped == 0
        assert len(packets(c.buffer)) == 2
        target = tmp_path / "sip.pcap"
        target.write_bytes(c.buffer)
        if shutil.which("tshark"):
            result = await asyncio.to_thread(
                subprocess.run,
                [
                    "tshark",
                    "-r",
                    str(target),
                    "-T",
                    "fields",
                    "-e",
                    "sip.Method",
                    "-e",
                    "sip.Status-Code",
                    "-e",
                    "sip.Call-ID",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            assert result.stdout.splitlines() == [
                "BYE\t\tdiagnostic-call",
                "\t200\tdiagnostic-call",
            ]
    finally:
        c.clear()
        transport.close()
        peer.close()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_tcp_stream_has_one_record_per_message_not_per_chunk():
    c = capture_module.SipCapture()
    done = asyncio.Event()

    async def peer(reader, writer):
        try:
            assert await tcp.read_sip_stream_message(reader) == BYE
            writer.write(OK[:19])
            await writer.drain()
            writer.write(OK[19:])
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            done.set()

    server = await asyncio.start_server(peer, "127.0.0.1", 0)
    reader, writer = await asyncio.open_connection(*server.sockets[0].getsockname())
    sender = tcp.SipTcpWriter(writer, label="capture test")
    try:
        c.start()
        assert await sender.send(BYE)
        assert (
            await asyncio.wait_for(
                tcp.read_sip_stream_message(reader, writer=writer), 2
            )
            == OK
        )
        c.stop()
        assert c.messages == 2 and c.dropped == 0
        assert len(packets(c.buffer)) == 2
    finally:
        c.clear()
        await sender.close()
        writer.close()
        await writer.wait_closed()
        await done.wait()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_registered_trunk_capture_contains_authenticated_bye_once_per_io():
    """Real reused UDP flow, including a provider challenge during hangup."""
    from .voip_phase1_support import sip, sip_client, sip_trunk, audio_format

    module = importlib.import_module(f"{sip_client.__package__}.sip_capture")
    capture = module.SipCapture()
    wire = []
    ended = asyncio.Event()

    class Provider(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, raw, addr):
            wire.append(raw)
            message = sip.parse_message(raw)
            if message.method == "ACK":
                return
            challenged = message.method in {"INVITE", "BYE"} and not message.header(
                "Proxy-Authorization"
            )
            response = sip.build_uas_response(
                message,
                407 if challenged else 200,
                "Proxy Authentication Required" if challenged else "OK",
                to_tag="capture-peer",
                contact_uri=f"sip:peer@127.0.0.1:{self.transport.get_extra_info('sockname')[1]}",
                body=message.body
                if message.method == "INVITE" and not challenged
                else b"",
            )
            if challenged:
                response = response.replace(
                    b"Content-Length:",
                    b'Proxy-Authenticate: Digest realm="capture-test", nonce="challenge", qop="auth"\r\nContent-Length:',
                )
            self.transport.sendto(response, addr)
            wire.append(response)
            if message.method == "BYE" and not challenged:
                ended.set()

    loop = asyncio.get_running_loop()
    peer, _ = await loop.create_datagram_endpoint(Provider, local_addr=("127.0.0.1", 0))
    port = peer.get_extra_info("sockname")[1]
    trunk = sip_trunk.SipTrunkClient(
        config=sip_trunk.SipTrunkConfig(
            enabled=True,
            transport="udp",
            server="127.0.0.1",
            port=port,
            domain="example.test",
            auth_username="test",
            username="test",
            password="test-password",
            expires=300,
        ),
        local_ip="127.0.0.1",
        local_sip_port=5060,
    )
    client = None
    try:
        capture.start()
        await trunk._connect_udp()
        trunk._ensure_receive_task()
        assert await trunk.register(timeout=2) == "registered"
        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=trunk._udp_local_port,
            local_rtp_port=41000,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)],
            username="test",
            password="test-password",
        )
        cid = client.dialog_ids.call_id
        send, responses = trunk.open_outbound_dialog(cid)
        client.use_reused_signaling_flow(
            send=send,
            responses=responses,
            close=lambda: trunk.close_outbound_dialog(cid),
        )
        assert (
            await client.invite(
                target="peer", remote_host="127.0.0.1", remote_sip_port=port, timeout=2
            )
            == "in_call"
        )
        await client.close()
        await asyncio.wait_for(ended.wait(), 2)
        capture.stop()
        assert capture.dropped == 0
        saved = packets(capture.buffer)
        assert len(saved) == len(wire)
        byes = [message for message in saved if b"BYE sip:" in message]
        assert len(byes) == 2
        assert (
            sum(b"Proxy-Authorization: [redacted]" in message for message in byes) == 1
        )
        assert not trunk._outbound_dialogs
    finally:
        capture.clear()
        if client is not None:
            await client.close()
        trunk.registered = False
        await trunk.stop()
        peer.close()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_automatic_stop_and_header_whitespace_redaction():
    c = capture_module.SipCapture()
    c.start(duration=0.01)
    data = INVITE.replace(b"Proxy-Authorization:", b"Proxy-Authorization \t:")
    c.record(data, ("127.0.0.1", 5060), ("127.0.0.2", 5060), False, True)
    await asyncio.sleep(0.02)
    assert not c.active and c.reason == "duration_limit"
    assert b"secret-digest" not in c.buffer and b"secret-user" not in c.buffer
    assert c.messages == 1
    # Exercise the actual retention expiry callback without waiting 15 minutes.
    c._expiry._run()
    assert not c.capture_id and not c.buffer
