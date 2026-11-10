"""Authenticated Contact discovery reaches public group and forward offers."""

import asyncio
import json
import re
import socket
import struct
from unittest.mock import AsyncMock

import pytest

from custom_components.voip_stack import call_forwarder
from custom_components.voip_stack.core import rtp, sdp, sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.endpoint_routing import roster_from_peers
from custom_components.voip_stack.phone_endpoint import EndpointAvailability, EndpointKind, PhoneEndpoint
from custom_components.voip_stack.roster import RosterEntry
from custom_components.voip_stack.runtime_data import sip_endpoint_manager
from custom_components.voip_stack.sip_client import SipCallClient
from custom_components.voip_stack.sip_registrar import SipAccount
from custom_components.voip_stack.sip_udp_io import SipDatagramQueueProtocol
from .test_sip_capabilities_ha import RegisteredPeer, register_peer, wait_until
from .test_trunk_calls_ha import lab as lab, trunks as trunks


pytestmark = pytest.mark.ha


class ContactPeer(RegisteredPeer):
    def __init__(self, outcome, rtp_socket):
        super().__init__(outcome)
        self.offers = []
        self.rtp_socket = rtp_socket

    def datagram_received(self, raw, address):
        message = sip.parse_message(raw)
        if message.method == "INVITE":
            self.offers.append((message, address))
            self.answer(message, address, 180)
        elif message.method == "CANCEL":
            self.answer(message, address)
            pending = next(request for request, _ in self.offers
                if request.header("Call-ID") == message.header("Call-ID"))
            self.answer(pending, address, 487)
        else:
            super().datagram_received(raw, address)

    def finish(self):
        request, address = self.offers[0]
        payload = re.search(rb"a=rtpmap:(\d+) L16/16000/1", request.body)
        assert payload is not None, request.body
        self.payload_type = int(payload[1])
        body = ("v=0\r\no=- 9 9 IN IP4 127.0.0.1\r\ns=Final Contact answer\r\n"
            "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
            f"m=audio {self.rtp_socket.getsockname()[1]} RTP/AVP {payload[1].decode()}\r\n"
            f"a=rtpmap:{payload[1].decode()} L16/16000/1\r\n"
            "a=ptime:20\r\na=sendrecv\r\n").encode()
        self.answer(request, address, body=body)


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("route", ["group", "forward"])
@pytest.mark.parametrize("unknown_outcome", [405, "timeout"])
@pytest.mark.parametrize("winner", [0, 1])
async def test_group_and_forward_keep_audio_provenance_per_authenticated_contact(trunks, tmp_path, monkeypatch, route, unknown_outcome, winner):
    runtime = trunks
    registrar = runtime.calls.component("registrar")
    registrar.enabled = True
    runtime.runtime.endpoints.register(PhoneEndpoint(
        endpoint_id="caps", name="Caps", username="caps", kind=EndpointKind.SIP_ACCOUNT))
    registrar.update_accounts([SipAccount("caps", "Caps", "fixture-only")])
    transports = []
    peers = []
    audio_sockets = []

    def audio_socket(host):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((host, 0))
        sock.setblocking(False)
        audio_sockets.append(sock)
        return sock

    caller_audio = audio_socket("127.0.0.2")
    caller = SipCallClient(local_ip="127.0.0.2", local_name="Independent caller", local_sip_port=0,
        local_rtp_port=caller_audio.getsockname()[1], supported_formats=[AudioFormat(16000, frame_ms=20)])
    caller.transport, caller.protocol = await runtime.hass.loop.create_datagram_endpoint(
        lambda: SipDatagramQueueProtocol(caller.queue), local_addr=("127.0.0.2", 0))
    caller.local_sip_port = caller.transport.get_extra_info("sockname")[1]
    forwarding = None
    try:
        for name, outcome in (("a", "known"), ("z", unknown_outcome)):
            peer_audio = audio_socket("127.0.0.1")
            transport, peer = await runtime.hass.loop.create_datagram_endpoint(
                lambda outcome=outcome, audio=peer_audio: ContactPeer(outcome, audio), local_addr=("127.0.0.1", 0))
            transports.append(transport)
            peers.append(peer)
            await register_peer(runtime, peer, call_id=f"registration-{name}",
                contact=f"sip:{name}@127.0.0.1:{transport.get_extra_info('sockname')[1]}")
            await asyncio.wait_for(peer.options.get(), 1)
        await wait_until(lambda: registrar.registered_contacts("caps")[0].capabilities is not None)
        bindings = registrar.registered_contacts("caps")
        assert len(bindings) == 2
        assert bindings[0].capabilities["sdp_features"] == ["directional_audio_v1"]
        assert bindings[1].capabilities is None
        runtime.roster.extend(roster_from_peers(runtime.hass, [], registrar.registered_roster_entries()))
        runtime.roster.append(RosterEntry("ContactGroup", metadata={"group_type": "ring", "members": ["caps"]}))
        if route == "forward":
            runtime.phones[0].auto_answer = False
            runtime.runtime.endpoints.update("sip:bedroom0", availability=EndpointAvailability.AVAILABLE)
            monkeypatch.setattr(call_forwarder, "_async_build_peer_snapshot", AsyncMock(return_value=[]))
            monkeypatch.setattr(call_forwarder, "_roster_from_peers", lambda *_args: runtime.roster)
        result = await caller.invite(target="ContactGroup" if route == "group" else "bedroom0",
            remote_host="127.0.0.1", remote_sip_port=runtime.runtime.transport_config["sip_port"], timeout=2)
        assert result == "ringing"
        if route == "forward":
            forwarding = asyncio.create_task(runtime.calls.forward_call(
                call_id=caller.dialog_ids.call_id, destination="ContactGroup"))
        await wait_until(lambda: all(peer.offers for peer in peers))
        for index, peer in enumerate(peers):
            request, _ = peer.offers[0]
            (tmp_path / f"contact-{index}-offer.json").write_text(json.dumps({
                "uri": request.uri, "headers": request.headers, "body": request.body.decode(),
                "binding": bindings[index].flow_key,
            }, indent=2))
        assert b"a=x-voip-stack-flow:" in peers[0].offers[0][0].body
        assert b"a=x-voip-stack-flow:" not in peers[1].offers[0][0].body
        peers[winner].finish()
        assert await caller.wait_for_final(timeout=2) == "in_call"
        assert caller.dialog.send_format.sample_rate == 16000
        owner = next(endpoint for endpoint in sip_endpoint_manager(runtime.hass)._dialog_endpoints()
            if caller.dialog.call_id in endpoint.active_dialogs)
        answer = sdp.parse_sdp(owner.active_dialogs[caller.dialog.call_id].answer_sdp)
        offer = sdp.parse_sdp(caller.dialog.local_sdp_body)
        # Nominal receive preferences are independent; wire PCM below stays at 20 ms.
        assert caller.dialog.send_format.frame_ms == answer['ptime']
        assert caller.dialog.recv_format.frame_ms == offer['ptime']
        assert answer['maxptime'] >= 20
        if forwarding is not None:
            await asyncio.wait_for(forwarding, 2)
        samples = 16000 * 20 // 1000
        source_pcm = struct.pack(f"!{samples}h", *(1000 + index for index in range(samples)))
        loop = runtime.hass.loop
        await loop.sock_sendto(caller_audio, rtp.build_packet(rtp.RtpPacket(
            caller.dialog.send_format.payload_type, 1, 0, 701, source_pcm)),
            (caller.dialog.remote_rtp_host, caller.dialog.remote_rtp_port))
        async with asyncio.timeout(2):
            received = rtp.parse_packet(await loop.sock_recv(peers[winner].rtp_socket, 4096))
        assert received.payload_type == peers[winner].payload_type
        assert received.payload == source_pcm
        target_pcm = struct.pack(f"!{samples}h", *(3000 + index for index in range(samples)))
        target_port = int(re.search(rb"m=audio (\d+)", peers[winner].offers[0][0].body)[1])
        await loop.sock_sendto(peers[winner].rtp_socket, rtp.build_packet(rtp.RtpPacket(
            peers[winner].payload_type, 1, 0, 702, target_pcm)), ("127.0.0.1", target_port))
        async with asyncio.timeout(2):
            returned = rtp.parse_packet(await loop.sock_recv(caller_audio, 4096))
        assert returned.payload_type == caller.dialog.recv_format.payload_type
        assert returned.payload == target_pcm
        (tmp_path / "forwarded-pcm.bin").write_bytes(received.payload)
        (tmp_path / "returned-pcm.bin").write_bytes(returned.payload)
        (tmp_path / "media.json").write_text(json.dumps({
            "sample_rate": 16000, "frame_ms": 20, "samples_each_direction": samples,
            "forwarded_bytes": len(received.payload), "returned_bytes": len(returned.payload),
        }, indent=2))
        caller.bye()
        await wait_until(lambda: not runtime.calls.sessions)
        assert not runtime.runtime.rtp_port_pool.get("used")
        assert not runtime.calls.endpoint_claims_snapshot()
        assert not runtime.calls.sip_clients_snapshot()
        assert not runtime.calls.relays_snapshot()
        if runtime.runtime.capability_probes:
            await asyncio.wait_for(asyncio.gather(*runtime.runtime.capability_probes.values()), 4)
        assert bindings[1].capabilities is None
    finally:
        if forwarding is not None and not forwarding.done():
            forwarding.cancel()
            await asyncio.gather(forwarding, return_exceptions=True)
        if caller.dialog is not None:
            caller.bye()
        await caller.close()
        for transport in transports:
            transport.close()
        for sock in audio_sockets:
            sock.close()
