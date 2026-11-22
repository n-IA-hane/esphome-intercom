"""Lost terminal requests retain their production SIP owner after local cleanup."""

import asyncio
import socket

import pytest

from custom_components.voip_stack.core import sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.endpoint_session import CleanupStage, TerminationIntent
from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
from custom_components.voip_stack.sip_client import SipCallClient

pytestmark = pytest.mark.ha


class LossPeer(asyncio.DatagramProtocol):
    """Independent UDP peer; tests decide which received datagrams get answers."""

    def __init__(self, *, answered=False):
        self.answered = answered
        self.messages = asyncio.Queue()
        self.invite = None
        self.terminal = []
        self.finishing = False

    def connection_made(self, transport):
        self.transport = transport
        self.port = transport.get_extra_info("sockname")[1]

    def respond(self, request, address, status, reason, body=b""):
        values = [(name, request.header(name)) for name in ("Via", "From", "Call-ID", "CSeq")]
        to = request.header("To")
        values.append(("To", to if ";tag=" in to else to + ";tag=loss-peer"))
        if body:
            values.extend((("Content-Type", "application/sdp"),
                           ("Contact", f"<sip:peer@127.0.0.1:{self.port}>")))
        self.transport.sendto(sip.build_response(status, reason, values, body), address)

    def datagram_received(self, raw, address):
        message = sip.parse_message(raw)
        self.messages.put_nowait((bytes(raw), message, address, asyncio.get_running_loop().time()))
        if message.method == "INVITE":
            self.invite = message, address
            if self.answered:
                body = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Loss peer\r\n"
                        "c=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio 35000 RTP/AVP 96\r\n"
                        "a=rtpmap:96 L16/16000/1\r\na=ptime:20\r\na=sendrecv\r\n").encode()
                self.respond(message, address, 200, "OK", body)
            else:
                self.respond(message, address, 180, "Ringing")
        elif message.method in {"CANCEL", "BYE"}:
            self.terminal.append((message, address))
            if self.finishing:
                self.respond(message, address, 200, "OK")
                if message.method == "CANCEL" and self.invite is not None:
                    self.respond(*self.invite, 487, "Request Terminated")

    async def receive(self, method, *, timeout=5):
        async with asyncio.timeout(timeout):
            while True:
                item = await self.messages.get()
                if item[1].method == method:
                    return item

    def finish(self):
        self.finishing = True
        for request, address in self.terminal:
            self.respond(request, address, 200, "OK")
        if not self.answered and self.invite is not None:
            self.respond(*self.invite, 487, "Request Terminated")


async def test_lost_first_cancel_retransmits_identical_request_until_487(socket_enabled):
    loop = asyncio.get_running_loop()
    transport, peer = await loop.create_datagram_endpoint(LossPeer, local_addr=("127.0.0.1", 0))
    client = SipCallClient(local_ip="127.0.0.1", local_name="caller", local_sip_port=0,
        local_rtp_port=35002, supported_formats=[AudioFormat(16000, frame_ms=20)],
        include_common_codecs=False)
    stopping = None
    try:
        assert await client.invite(target="peer", remote_host="127.0.0.1",
            remote_sip_port=peer.port, timeout=3) == "ringing"
        invite, _ = peer.invite
        stopping = asyncio.create_task(client.terminate(timeout=4))
        first, cancel, address, _ = await peer.receive("CANCEL")
        # Drop the first actual UDP datagram by withholding its response.
        repeated, retry, retry_address, _ = await peer.receive("CANCEL")
        assert repeated == first and retry_address == address
        assert cancel.uri == invite.uri and cancel.header("Via") == invite.header("Via")
        assert cancel.header("Call-ID") == invite.header("Call-ID")
        assert sip.parse_cseq(cancel.header("CSeq")).number == sip.parse_cseq(invite.header("CSeq")).number
        assert not stopping.done()
        peer.respond(retry, retry_address, 200, "OK")
        peer.respond(invite, address, 487, "Request Terminated")
        _, ack, _, _ = await peer.receive("ACK")
        assert ack.header("Call-ID") == invite.header("Call-ID")
        assert await asyncio.wait_for(stopping, 2) == "cancelled"
        assert client._invite_task.done() and not client._invite_transaction_active
        assert all(request.method != "BYE" for request, _ in peer.terminal)
        await client.close()
        assert client.transport is None and client._closed
    finally:
        peer.finish()
        await client.close()
        if stopping is not None:
            await asyncio.gather(stopping, return_exceptions=True)
        if client._close_task is not None:
            await asyncio.wait_for(asyncio.shield(client._close_task), 5)
        transport.close()


async def test_lost_byes_outlive_ui_and_media_close_until_real_200(socket_enabled):
    loop = asyncio.get_running_loop()
    transport, peer = await loop.create_datagram_endpoint(
        lambda: LossPeer(answered=True), local_addr=("127.0.0.1", 0))
    media = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    media.bind(("127.0.0.1", 0))
    client = SipCallClient(local_ip="127.0.0.1", local_name="caller", local_sip_port=0,
        local_rtp_port=media.getsockname()[1], audio_rtp_socket=media,
        supported_formats=[AudioFormat(16000, frame_ms=20)], include_common_codecs=False)
    calls = SipEndpointRuntime()
    calls.activate()
    snapshots = []
    unsubscribe = calls.subscribe_state(snapshots.append)
    cleanup = None
    try:
        assert await client.invite(target="peer", remote_host="127.0.0.1",
            remote_sip_port=peer.port, timeout=3) == "in_call"
        session = calls.upsert(client.dialog_ids.call_id, state="in_call", owner="ha_softphone")
        # The actual session owns local cleanup; the client retains its one
        # signaling task when close() returns at the UI wait budget.
        session.add_resource("sip-client", client, lambda _reason: client.close(), stage=CleanupStage.LEG)
        started = loop.time()
        cleanup = calls.request_termination(session.call_id, TerminationIntent("local_hangup"))
        first, _, _, _ = await peer.receive("BYE")
        await asyncio.wait_for(asyncio.shield(cleanup), 2.5)
        ui_closed = loop.time()
        assert ui_closed - started >= 1.5
        assert calls.get_session(session.call_id) is None and snapshots[-1].phase == "terminated"
        assert media.fileno() == -1 and client.audio_rtp_socket is None
        assert client.transport is not None and not client.transport.is_closing()
        assert client._close_task is not None and not client._close_task.done()
        bye_owner = client._dialog_termination_task
        assert bye_owner is not None and not bye_owner.done()
        # Discard every BYE through the UI boundary, including retransmissions.
        while True:
            repeated, bye, address, received = await peer.receive("BYE")
            assert repeated == first and client._dialog_termination_task is bye_owner
            if received > ui_closed:
                break
        assert received - started > 1.5 and len(peer.terminal) >= 3
        peer.respond(bye, address, 200, "OK")
        assert await asyncio.wait_for(asyncio.shield(bye_owner), 2) == "remote_hangup"
        await asyncio.wait_for(asyncio.shield(client._close_task), 2)
        assert client.transport is None and client.dialog is None and client._closed
        assert not calls.sessions and not calls.sip_clients_snapshot()
    finally:
        peer.finish()
        await client.close()
        if cleanup is not None:
            await asyncio.wait_for(asyncio.shield(cleanup), 5)
        if client._close_task is not None:
            await asyncio.wait_for(asyncio.shield(client._close_task), 5)
        unsubscribe()
        media.close()
        transport.close()


async def test_trunk_423_retry_uses_owned_contact_expiry_and_keeps_udp_owner(socket_enabled):
    import hashlib
    import re
    import time

    from custom_components.voip_stack.sip_trunk import SipTrunkClient, SipTrunkConfig

    requests, errors = [], []
    realm, nonce, password = "provider.test", "registration-fixture", "test-only-password"

    class Registrar(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport
            self.port = transport.get_extra_info("sockname")[1]

        def datagram_received(self, raw, address):
            try:
                request = sip.parse_message(raw)
                assert request.method == "REGISTER"
                requests.append(request)
                expires = int(request.header("Expires"))
                headers = [(name, request.header(name)) for name in ("Via", "From", "Call-ID", "CSeq")]
                headers.append(("To", request.header("To") + ";tag=registrar"))
                if 0 < expires < 300:
                    status, reason = 423, "Interval Too Brief"
                    headers.append(("Min-Expires", "300"))
                elif expires and not request.header("Authorization"):
                    status, reason = 401, "Unauthorized"
                    headers.append(("WWW-Authenticate", f'Digest realm="{realm}", nonce="{nonce}", qop="auth"'))
                else:
                    if expires:
                        auth = {key: value.strip('"') for key, value in re.findall(
                            r'(\w+)=("[^"]*"|[^, ]+)', request.header("Authorization"))}
                        def md5(value):
                            return hashlib.md5(value.encode()).hexdigest()
                        expected = md5(f'{md5(f"line:{realm}:{password}")}:{nonce}:{auth["nc"]}:'
                                       f'{auth["cnonce"]}:auth:{md5("REGISTER:" + request.uri)}')
                        assert auth["username"] == "line" and auth["uri"] == request.uri
                        assert auth["nc"] == "00000001" and auth["response"] == expected
                    status, reason = 200, "OK"
                    own = request.header("Contact")
                    headers.extend((("Expires", "900"), ("Contact",
                        f'<sip:other@127.0.0.1:{self.port}>;expires=5, {own};expires={180 if expires else 0}')))
                self.transport.sendto(sip.build_response(status, reason, headers), address)
            except Exception as error:
                errors.append(error)

    loop = asyncio.get_running_loop()
    transport, provider = await loop.create_datagram_endpoint(Registrar, local_addr=("127.0.0.1", 0))
    trunk = SipTrunkClient(config=SipTrunkConfig(enabled=True, transport="udp", server="127.0.0.1",
        port=provider.port, domain=realm, username="line", auth_username="line", password=password, expires=60),
        local_ip="127.0.0.1", local_sip_port=0)
    try:
        await trunk._connect_udp()
        trunk._ensure_receive_task()
        original_transport, original_reader = trunk.transport, trunk._receive_task
        for attempt in range(2):
            before = time.time()
            assert await trunk.register(timeout=2) == "registered", errors
            after = time.time()
            assert not errors
            assert before + 180 <= trunk.expires_at <= after + 180
            assert trunk.ready and trunk.transport is original_transport and trunk._receive_task is original_reader
            current = requests[attempt * 3:(attempt + 1) * 3]
            assert [int(message.header("Expires")) for message in current] == [60, 300, 300]
            assert not current[0].header("Authorization") and not current[1].header("Authorization")
            assert current[2].header("Authorization")
        assert {message.header("Call-ID") for message in requests} == {trunk.call_id}
        assert len({message.header("From") for message in requests}) == 1
        sequences = [sip.parse_cseq(message.header("CSeq")).number for message in requests]
        assert sequences == sorted(set(sequences))
        assert len({sip.parse_via(message.header("Via")).branch for message in requests}) == len(requests)
        await asyncio.wait_for(trunk.stop(), 2)
        assert requests[-1].header("Expires") == "0" and not errors
        assert trunk.transport is None and trunk._receive_task is None and not trunk.ready
        assert not trunk._outbound_dialogs and trunk.expires_at == 0
    finally:
        await asyncio.wait_for(trunk.stop(), 2)
        transport.close()
