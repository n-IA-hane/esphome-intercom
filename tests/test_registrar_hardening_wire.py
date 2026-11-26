"""Independent SIP peers qualify registrar admission and cleanup on real flows."""
from __future__ import annotations

import asyncio
import socket

import pytest

from .voip_phase1_support import audio_format, sip_endpoint, sip_listener, sip_registrar
from .test_linphone_registrar_wire import _digest, _response


class RegistrarPeer:
    def __init__(self, transport, registrar):
        self.transport = transport
        self.registrar = registrar
        self.writer = self.sock = None
        self.sequence = 0

    async def start(self):
        async def unexpected(_invite):
            raise AssertionError("REGISTER must not allocate an INVITE")
        self.manager = sip_endpoint.SipEndpointManager(
            host="127.0.0.1", port=0, local_ip="127.0.0.1", local_rtp_port=44000,
            supported_formats=[audio_format.AudioFormat(8000, frame_ms=20)],
            on_invite=unexpected, on_register=self.registrar.handle_register,
            udp_enabled=self.transport == "UDP", tcp_enabled=self.transport == "TCP")
        assert await self.manager.start()
        # A zero port is independently assigned by the real socket owner.
        if self.transport == "UDP":
            address = self.manager.udp_server.transport.get_extra_info("sockname")
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.bind(("127.0.0.1", 0))
            self.sock.setblocking(False)
            self.source_port = self.sock.getsockname()[1]
        else:
            address = self.manager.tcp_server.server.sockets[0].getsockname()
            self.reader, self.writer = await asyncio.open_connection(*address[:2])
            self.source_port = self.writer.get_extra_info("sockname")[1]
        self.address = address[:2]
        self.uri = f"sip:127.0.0.1:{address[1]}"
        return self

    async def exchange(self, *, password=None, challenge="", expires=120, contacts=None, raw=None):
        if raw is None:
            self.sequence += 1
            lines = [f"REGISTER {self.uri} SIP/2.0",
                f"Via: SIP/2.0/{self.transport} 127.0.0.1:{self.source_port};branch=z9hG4bK-harden-{self.sequence};rport",
                "From: <sip:phone@127.0.0.1>;tag=hardening", "To: <sip:phone@127.0.0.1>",
                "Call-ID: hardening-registration", f"CSeq: {self.sequence} REGISTER", f"Expires: {expires}"]
            for contact in contacts if contacts is not None else ["sip:phone@10.0.0.1:5060"]:
                lines.append("Contact: *" if contact == "*" else f"Contact: <{contact}>")
            if password is not None:
                lines.append("Authorization: " + _digest(challenge, self.uri, "phone", password, nonce_count=self.sequence))
            raw = ("\r\n".join(lines) + "\r\nContent-Length: 0\r\n\r\n").encode()
        if self.writer is not None:
            self.writer.write(raw)
            await self.writer.drain()
            reply = await asyncio.wait_for(sip_listener._read_sip_stream_message(self.reader), 2)
        else:
            loop = asyncio.get_running_loop()
            await loop.sock_sendto(self.sock, raw, self.address)
            reply = await asyncio.wait_for(loop.sock_recv(self.sock, 65535), 2)
        return (*_response(reply), raw)

    async def close(self):
        if self.writer is not None:
            self.writer.close()
            await self.writer.wait_closed()
        if self.sock is not None:
            self.sock.close()
        await self.manager.stop()


def registrar(**kwargs):
    return sip_registrar.SipRegistrar(enabled=True,
        accounts=[sip_registrar.SipAccount("phone", "Phone", "wire-only-password")],
        local_ip="127.0.0.1", local_sip_port=5060, **kwargs)


@pytest.mark.parametrize("transport", ["UDP", "TCP"])
async def test_registration_minimum_and_zero_unregistration_on_real_flow(transport):
    owner = registrar()
    peer = await RegistrarPeer(transport, owner).start()
    try:
        status, headers, _ = await peer.exchange()
        assert status == 401
        challenge = headers["www-authenticate"]
        status, headers, _ = await peer.exchange(password="wire-only-password", challenge=challenge, expires=5)
        assert status == 423
        assert headers["min-expires"] == "60"
        assert not owner.registrations
        status, headers, _ = await peer.exchange()
        challenge = headers["www-authenticate"]
        status, _, _ = await peer.exchange(password="wire-only-password", challenge=challenge, expires=60)
        assert status == 200
        assert len(owner.registered_contacts("phone")) == 1
        _, headers, _ = await peer.exchange()
        status, _, _ = await peer.exchange(password="wire-only-password", challenge=headers["www-authenticate"], expires=0)
        assert status == 200
        assert not owner.registrations
    finally:
        await peer.close()


@pytest.mark.parametrize("transport", ["UDP", "TCP"])
async def test_wrong_digest_blocks_source_without_challenge_or_retransmit_penalty(transport):
    owner = registrar()
    peer = await RegistrarPeer(transport, owner).start()
    try:
        for _ in range(12):
            status, headers, _ = await peer.exchange()
            assert status == 401
        challenge = headers["www-authenticate"]
        for _ in range(9):
            status, _, raw = await peer.exchange(password="incorrect", challenge=challenge)
            assert status == 401
            for _ in range(3):
                assert (await peer.exchange(raw=raw))[0] == 401
        status, _, _ = await peer.exchange(password="incorrect", challenge=challenge)
        assert status == 403
        status, _, _ = await peer.exchange(password="wire-only-password", challenge=challenge)
        assert status == 403
        assert not owner.registrations
        assert len(owner.nonces) <= sip_registrar.MAX_ACTIVE_NONCES
    finally:
        await peer.close()


@pytest.mark.parametrize("transport", ["UDP", "TCP"])
async def test_contacts_limit_rejects_whole_registration_atomically(transport):
    owner = registrar()
    peer = await RegistrarPeer(transport, owner).start()
    try:
        _, headers, _ = await peer.exchange()
        contacts = [f"sip:client{index}@10.0.0.1:5060" for index in range(17)]
        status, _, _ = await peer.exchange(password="wire-only-password", challenge=headers["www-authenticate"], contacts=contacts)
        assert status == 403
        assert not owner.registrations
    finally:
        await peer.close()
