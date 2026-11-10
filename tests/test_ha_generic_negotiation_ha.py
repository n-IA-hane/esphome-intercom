"""Public HA origination negotiates generic peers beyond preference tuples."""

import asyncio
import re
import socket
import struct
from unittest.mock import AsyncMock

import pytest

from custom_components.voip_stack import softphone_originate
from custom_components.voip_stack.core import rtp, sip
from custom_components.voip_stack.core.sip_auth import build_digest_authorization
from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
from custom_components.voip_stack.roster import dump_roster_json
from custom_components.voip_stack.rtp_audio_receiver import RtpAudioReceiver
from custom_components.voip_stack.sip_registrar import SipAccount

from .test_sip_capabilities_ha import lab as lab, trunks as trunks, wait_until


pytestmark = pytest.mark.ha


def narrowband_sdp(payload_type, media_port):
    """Independent standard peer SDP: one encoding and an exact RX range."""
    return (
        "v=0\r\no=- 8 8 IN IP4 127.0.0.1\r\ns=Generic narrowband endpoint\r\n"
        "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
        f"m=audio {media_port} RTP/AVP {payload_type}\r\n"
        f"a=rtpmap:{payload_type} L16/8000/1\r\n"
        "a=ptime:32\r\na=minptime:32\r\na=maxptime:32\r\na=sendrecv\r\n"
    ).encode()


class GenericRegisteredPeer(asyncio.DatagramProtocol):
    """Ordinary SIP peer with an independent answer, without media selectors."""

    user_agent = "GenericVoice/1.0"

    def __init__(self, discovery_known, media_port):
        self.discovery_known = discovery_known
        self.media_port = media_port
        self.messages = asyncio.Queue()
        self.options_replied = asyncio.Event()
        self.acked = asyncio.Event()
        self.bye = asyncio.Event()
        self.offers = []
        self.answers = []
        self.options_body = narrowband_sdp(110, 0) if discovery_known else b""

    def connection_made(self, transport):
        self.transport = transport

    def answer(self, request, address, status=200, body=b""):
        headers = [
            (key, request.header(key)) for key in ("Via", "From", "Call-ID", "CSeq")
        ]
        to = request.header("To")
        headers.append(("To", to if ";tag=" in to else to + ";tag=generic"))
        headers.append(("User-Agent", self.user_agent))
        if body:
            port = self.transport.get_extra_info("sockname")[1]
            headers.extend(
                [
                    ("Content-Type", "application/sdp"),
                    ("Contact", f"<sip:caps@127.0.0.1:{port}>"),
                ]
            )
        self.transport.sendto(
            sip.build_response(
                status,
                "OK" if status == 200 else "Method Not Allowed",
                headers,
                body,
            ),
            address,
        )

    def datagram_received(self, raw, address):
        request = sip.parse_message(raw)
        if request.is_response:
            self.messages.put_nowait(request)
        elif request.method == "OPTIONS":
            self.answer(
                request,
                address,
                200 if self.discovery_known else 405,
                self.options_body,
            )
            self.options_replied.set()
        elif request.method == "INVITE":
            self.offers.append(request.body)
            mapping = re.search(rb"a=rtpmap:(\d+) L16/8000(?:/1)?\r\n", request.body)
            if mapping is None:
                self.answer(request, address, 488)
                return
            body = narrowband_sdp(int(mapping[1]), self.media_port)
            self.answers.append(body)
            self.answer(request, address, body=body)
        elif request.method == "ACK":
            self.acked.set()
        elif request.method == "BYE":
            self.answer(request, address)
            self.bye.set()


async def register_generic_peer(lab, peer):
    """Authenticate through the existing registrar with an ordinary User-Agent."""
    uri = f"sip:127.0.0.1:{lab.runtime.transport_config['sip_port']}"
    port = peer.transport.get_extra_info("sockname")[1]

    async def send(cseq, authorization=None):
        headers = [
            (
                "Via",
                f"SIP/2.0/UDP 127.0.0.1:{port};branch=z9hG4bK-generic-register-{cseq};rport",
            ),
            ("From", "<sip:caps@127.0.0.1>;tag=generic-register"),
            ("To", "<sip:caps@127.0.0.1>"),
            ("Call-ID", "generic-registration"),
            ("CSeq", f"{cseq} REGISTER"),
            ("Contact", f"<sip:caps@127.0.0.1:{port}>"),
            ("Expires", "60"),
            ("User-Agent", peer.user_agent),
        ]
        if authorization:
            headers.append(("Authorization", authorization))
        peer.transport.sendto(
            sip.build_request("REGISTER", uri, headers),
            ("127.0.0.1", lab.runtime.transport_config["sip_port"]),
        )
        return await asyncio.wait_for(peer.messages.get(), 2)

    challenge = await send(1)
    assert challenge.status_code == 401
    authorization = build_digest_authorization(
        challenge_header=challenge.header("WWW-Authenticate"),
        username="caps",
        password="fixture-only",
        method="REGISTER",
        uri=uri,
    )
    assert (await send(2, authorization)).status_code == 200


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize(
    "discovery_known", [False, True], ids=["options-405", "options-known"]
)
async def test_public_browser_call_accepts_generic_8000_32ms_peer(
    trunks,
    monkeypatch,
    discovery_known,
    record_property,
):
    lab = trunks
    browser = PhoneEndpoint(
        endpoint_id="browser:generic",
        name="Generic negotiation browser",
        device_id="generic-browser-device",
        kind=EndpointKind.BROWSER,
        availability="available",
    )
    lab.runtime.endpoints.register(browser)
    registrar = lab.calls.component("registrar")
    registrar.enabled = True
    registrar.update_accounts(
        [*registrar.accounts.values(), SipAccount("caps", "Caps", "fixture-only")]
    )
    media = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    media.bind(("127.0.0.1", 0))
    transport, peer = await lab.hass.loop.create_datagram_endpoint(
        lambda: GenericRegisteredPeer(discovery_known, media.getsockname()[1]),
        local_addr=("127.0.0.1", 0),
    )
    monkeypatch.setattr(
        softphone_originate, "_ha_advertise_host", AsyncMock(return_value="127.0.0.1")
    )
    monkeypatch.setattr(
        softphone_originate, "_resolve_target_device", AsyncMock(return_value=None)
    )
    client = None

    def current_client():
        call_id = lab.runtime.endpoints.require(browser.endpoint_id).active_call_id
        return lab.calls.sip_client_for(call_id) if call_id else None

    try:
        await register_generic_peer(lab, peer)
        await asyncio.wait_for(peer.options_replied.wait(), 2)
        await wait_until(lambda: not lab.runtime.capability_probes)
        (binding,) = registrar.registered_contacts("caps")
        assert binding.user_agent == peer.user_agent
        assert (binding.capabilities is not None) is discovery_known
        if discovery_known:
            assert binding.capabilities["sdp_features"] == []
            assert binding.capabilities["sip_audio_rx_formats"] == ["L16/8000/1/32"]
        assert not lab.calls.sessions
        lab.roster.extend(registrar.registered_roster_entries())
        lab.hass.states.async_set(
            "sensor.voip_phonebook",
            "ready",
            {
                "roster_json": dump_roster_json(lab.roster),
            },
        )
        await lab.hass.services.async_call(
            "voip_stack",
            "call",
            {
                "device_id": browser.device_id,
                "destination": "caps",
            },
            blocking=True,
        )
        await wait_until(
            lambda: (
                (active := current_client()) is not None and active.dialog is not None
            )
        )
        client = current_client()
        dialog = client.dialog
        await asyncio.wait_for(peer.acked.wait(), 2)
        assert len(peer.offers) == len(peer.answers) == 1
        record_property("wire_options_status", 200 if discovery_known else 405)
        record_property("wire_options_sdp", peer.options_body.decode())
        record_property("wire_offer", peer.offers[0].decode())
        record_property("wire_answer", peer.answers[0].decode())
        record_property("registered_user_agent", binding.user_agent)
        assert b"a=x-voip-stack-" not in peer.answers[0]
        assert (
            dialog.send_format.encoding,
            dialog.send_format.sample_rate,
            dialog.send_format.channels,
            dialog.send_format.frame_ms,
        ) == ("L16", 8000, 1, 32)
        assert (
            dialog.recv_format.encoding,
            dialog.recv_format.sample_rate,
            dialog.recv_format.channels,
        ) == ("L16", 8000, 1)
        assert all(
            fmt.sample_rate == 8000
            for fmt in (*dialog.recv_formats, *dialog.send_formats)
        )
        samples = tuple(range(-160, 160))
        packet = rtp.parse_packet(
            rtp.build_packet(
                rtp.RtpPacket(
                    dialog.recv_format.payload_type,
                    10,
                    800,
                    1234,
                    struct.pack("!256h", *samples[:256]),
                )
            )
        )
        assert len(packet.payload) == 512
        receiver = RtpAudioReceiver(dialog.recv_format, dialog.recv_formats)
        frames = receiver.decode(packet.payload_type, packet.payload)
        frames.extend(
            receiver.decode(packet.payload_type, struct.pack("!64h", *samples[256:]))
        )
        assert b"".join(frames) == struct.pack("<320h", *samples)
        record_property("accepted_rtp_packet_bytes", len(packet.payload))
        await lab.hass.services.async_call(
            "voip_stack",
            "hangup",
            {
                "device_id": browser.device_id,
                "call_id": dialog.call_id,
            },
            blocking=True,
        )
        await asyncio.wait_for(peer.bye.wait(), 2)
        await wait_until(lambda: not lab.calls.sessions)
        assert not lab.runtime.endpoints.require(browser.endpoint_id).active_call_id
        assert not lab.calls.sip_clients_snapshot()
        assert not lab.calls.client_watchers_snapshot()
        assert not lab.calls.leg_index
        assert not any(lab.runtime.media.active_sessions.values())
        assert not any(lab.runtime.media.owners.values())
        assert client.dialog is None and client.audio_rtp_socket is None
        assert client.media_reservation is None
    finally:
        call_id = lab.runtime.endpoints.require(browser.endpoint_id).active_call_id
        if call_id:
            await lab.hass.services.async_call(
                "voip_stack",
                "hangup",
                {
                    "device_id": browser.device_id,
                    "call_id": call_id,
                },
                blocking=True,
            )
        transport.close()
        media.close()
