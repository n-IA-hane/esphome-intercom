"""Real inbound factories preserve outbound flows and advisory codec offers."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import time

import pytest

from custom_components.voip_stack import sip_runtime
from custom_components.voip_stack.core import sdp, sip
from custom_components.voip_stack.core.codec_policy import CodecPolicy
from custom_components.voip_stack.core.sdp import RtpPcmFormat
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.inbound_routing import bridge
from custom_components.voip_stack.roster import RosterEntry
from custom_components.voip_stack.router import RouteAction, RouteDecision
from custom_components.voip_stack.sip_client import SipCallClient
from custom_components.voip_stack.sip_listener import SipInvite, SipUdpEndpoint
from custom_components.voip_stack.sip_registrar import SipAccount, SipRegistrar, SipRegistration


pytestmark = pytest.mark.ha


class _FactoryCompleted(BaseException):
    """Stop before unrelated bridge-media commit, after genuine SIP delivery."""


class _WirePeer(asyncio.DatagramProtocol):
    def __init__(self, encoding):
        self.encoding = encoding
        self.messages = []
        self.acked = asyncio.Event()
        self.contact = ""

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, raw, address):
        request = sip.parse_message(raw)
        self.messages.append((request, address))
        if request.method == "ACK":
            self.acked.set()
            return
        if request.method == "INVITE":
            # Independent offer oracle: match only the literal advertised
            # mapping, not the production negotiator's format selection.
            if self.encoding == "PCMU":
                media = next(line for line in request.body.splitlines() if line.startswith(b"m=audio "))
                offered = b"0" in media.split()[3:]
                pt, mapping = 0, "PCMU/8000"
            else:
                offered = b"L16/16000/1" in request.body
                import re
                match = re.search(rb"a=rtpmap:(\d+) L16/16000/1", request.body)
                pt, mapping = (int(match[1]) if match else 96), "L16/16000/1"
            body = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Wire peer\r\n"
                    "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
                    f"m=audio 35000 RTP/AVP {pt}\r\na=rtpmap:{pt} {mapping}\r\n"
                    "a=ptime:20\r\na=sendrecv\r\n").encode() if offered else b""
            response = sip.build_uas_response(request, 200 if offered else 488,
                "OK" if offered else "Not Acceptable Here", to_tag="wire-peer",
                contact_uri=self.contact, body=body,
                extra_headers=(("Record-Route", f"<{self.contact};lr>"),))
        else:
            response = sip.build_uas_response(request, 200, "OK", to_tag="wire-peer")
        self.transport.sendto(response, address)


async def _exercise_factory(monkeypatch, *, factory, outbound=False, advisory=None, encoding="L16", codec_policy=None):
    loop = asyncio.get_running_loop()
    transports, clients = [], []
    peers = []
    owner = None
    observed = {}
    try:
        for _ in range(2):
            transport, peer = await loop.create_datagram_endpoint(lambda: _WirePeer(encoding), local_addr=("127.0.0.1", 0))
            transports.append(transport)
            peers.append(peer)
        a, b = peers
        source_port = a.transport.get_extra_info("sockname")[1]
        contact_port = b.transport.get_extra_info("sockname")[1]
        contact = f"sip:phone@127.0.0.1:{contact_port}"
        a.contact = b.contact = contact
        owner = SipUdpEndpoint(local_ip="127.0.0.1", local_rtp_port=44000,
            supported_formats=[AudioFormat(16000, frame_ms=20)], on_invite=AsyncMock())
        transport, _ = await loop.create_datagram_endpoint(lambda: owner, local_addr=("127.0.0.1", 0))
        transports.append(transport)
        listener_port = transport.get_extra_info("sockname")[1]
        registrar = SipRegistrar(enabled=True, accounts=[SipAccount("phone", "Phone", "test-only")],
            local_ip="127.0.0.1", local_sip_port=listener_port)
        registrar.registrations["phone"] = SipRegistration(username="phone", contact_uri=contact,
            source_host="127.0.0.1", source_port=source_port, transport="UDP", outbound=outbound,
            expires_at=time.time() + 60, call_id="authenticated-registration")
        manager = SimpleNamespace(udp_server=SimpleNamespace(endpoint=owner))
        monkeypatch.setattr(sip_runtime, "sip_registrar", lambda _: registrar)
        monkeypatch.setattr(sip_runtime, "sip_endpoint_manager", lambda _: manager)
        monkeypatch.setattr(sip_runtime, "configure_local_contact", lambda *_: None)
        metadata = {"registered": True, "endpoint_kind": "sip_account"}
        if advisory is not None:
            metadata.update(sip_audio_tx_formats=["L16/16000/1/20"],
                sip_audio_rx_formats=["L16/16000/1/20"], sip_capabilities_known=advisory)
        elif encoding == "L16":
            # Configured capability metadata also remains advisory.
            metadata.update(sip_audio_tx_formats=["L16/16000/1/20"], sip_audio_rx_formats=["L16/16000/1/20"])
        entry = RosterEntry("phone", sip_uri=contact, metadata=metadata)
        decision = RouteDecision(RouteAction.BRIDGE, target="phone", entry=entry)
        media = RtpPcmFormat(96, "L16", 16000, 1, 20)
        source_offer = sdp.build_offer_directional("127.0.0.9", "127.0.0.9", 34000,
            [media.audio_format], [media.audio_format], send_rtp_formats=(media,), recv_rtp_formats=(media,))
        invite = SipInvite(call_id="inbound-factory", caller="Caller", target="phone", cseq="1 INVITE",
            request_uri=sip.parse_sip_uri("sip:phone@127.0.0.1"),
            caller_uri=sip.parse_sip_uri("sip:Caller@127.0.0.9"),
            source_host="127.0.0.9", source_port=5090, remote_sdp=source_offer.encode(),
            remote_rtp_host="127.0.0.9", remote_rtp_port=34000,
            send_format=media, recv_format=media, recv_formats=(media,), send_formats=(media,))
        artifacts = SimpleNamespace(trunk_closed=False, trunk_info_queue=None)
        registry = Mock()
        registry.resource_for.return_value = None
        registry.current_generation.return_value = 1
        ports = SimpleNamespace(ports=(44000, 44002), bind_audio_sockets=Mock(), release=Mock())
        hass = SimpleNamespace()
        monkeypatch.setattr(bridge, "take_delayed_offer_ports", lambda *_: ports)
        route_resolver = SimpleNamespace(route=lambda *_: decision, is_ha_target=lambda _: False,
            is_local_listener_uri=lambda _: False)
        runtime = SimpleNamespace(hass=hass, config={"sip_port": listener_port}, local_ip="127.0.0.1",
            ha_peer_name=lambda _: "HA", route_resolver=route_resolver,
            sip_uri_transport=sip_runtime.uri_transport,
            enable_reused_sip_tcp_connection=sip_runtime.enable_reused_signaling_flow)
        module = bridge
        real_profile = bridge.bridge_audio_profile

        def profile(*args, **kwargs):
            result = real_profile(*args, **kwargs)
            observed["profile_rtp_formats"] = result.send_rtp_formats
            return result

        monkeypatch.setattr(bridge, "bridge_audio_profile", profile)

        def client_factory(**kwargs):
            observed["client_rtp_formats"] = kwargs["supported_send_rtp_formats"]
            if codec_policy is not None:
                kwargs["codec_policy"] = codec_policy
            client = SipCallClient(**kwargs)
            clients.append(client)
            real_invite = client.invite

            async def invite_peer(**kwargs):
                observed["result"] = await real_invite(**kwargs, **({"timeout": 2} if "timeout" not in kwargs else {}))
                await asyncio.sleep(0)
                raise _FactoryCompleted

            client.invite = invite_peer
            return client

        monkeypatch.setattr(module, "SipCallClient", client_factory)
        with pytest.raises(_FactoryCompleted):
            await module.route_sip_bridge(runtime=runtime, invite=invite, decision=decision, peers=[],
                trunk_config={}, bridge_to_trunk=False, source_endpoint=None, target_endpoint=None,
                resolved_callee="phone", trunk_invite=False, registry=registry)
        await asyncio.sleep(0)
        observed.update(peers=peers, contact=contact, clients=clients)
        if outbound:
            assert [m.method for m, _ in b.messages] == [], "Contact must not replace the authenticated flow"
            delivered = a
            assert all(address[1] == listener_port for _, address in a.messages)
        else:
            assert not a.messages
            delivered = b
        policy_allowed = codec_policy is None or codec_policy.audio is None or encoding in codec_policy.audio
        assert observed["result"] == ("in_call" if policy_allowed else "media_incompatible")
        invite_wire = next(m for m, _ in delivered.messages if m.method == "INVITE")
        offered = sdp.offered_pcm_formats(invite_wire.body)
        offered_codecs = {fmt.encoding for fmt in offered}
        if codec_policy is not None:
            assert offered_codecs <= set(codec_policy.audio)
            assert ("PCMU" in offered_codecs) is policy_allowed
        if observed["result"] == "in_call":
            await asyncio.wait_for(delivered.acked.wait(), .5)
            ack = next(m for m, _ in delivered.messages if m.method == "ACK")
            assert invite_wire.uri == contact and ack.uri == contact
            assert ack.header("Route") == f"<{contact};lr>"
        assert not any(m.method == "OPTIONS" for peer in peers for m, _ in peer.messages)
        if encoding == "PCMU" and codec_policy is None:
            assert {"PCMU", "PCMA", "L16"} <= offered_codecs
            assert {16000, 48000} <= {fmt.sample_rate for fmt in offered if fmt.encoding == "L16"}
            assert {"PCMU", "PCMA", "L16"} <= {fmt.encoding for fmt in observed["client_rtp_formats"]}
            if factory == "bridge":
                assert {"PCMU", "PCMA", "L16"} <= {fmt.encoding for fmt in observed["profile_rtp_formats"]}
    finally:
        for client in clients:
            await client.close()
        if owner is not None:
            assert not owner._dialog_queues
        for transport in reversed(transports):
            transport.close()
        await asyncio.sleep(0)




@pytest.mark.parametrize("factory", ["bridge"])
@pytest.mark.parametrize("advisory", [False, True, None], ids=["configured-capabilities", "cached-options", "no-cache"])
async def test_inbound_factories_keep_common_audio_with_advisory_metadata(monkeypatch, socket_enabled, factory, advisory):
    await _exercise_factory(monkeypatch, factory=factory, advisory=advisory, encoding="PCMU")


@pytest.mark.parametrize("factory", ["bridge"])
@pytest.mark.parametrize("allowed", [True, False], ids=["policy-permits-pcmu", "policy-l16-only"])
async def test_inbound_factories_preserve_explicit_codec_policy(monkeypatch, socket_enabled, factory, allowed):
    policy = CodecPolicy(audio=("PCMU",) if allowed else ("L16",))
    await _exercise_factory(monkeypatch, factory=factory, advisory=True, encoding="PCMU", codec_policy=policy)
