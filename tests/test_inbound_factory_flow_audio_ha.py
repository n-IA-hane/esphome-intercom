"""Real inbound factories preserve outbound flows and advisory codec offers."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import time

import pytest

from custom_components.voip_stack import sip_runtime, trunk_inbound_router
from custom_components.voip_stack.core import sip
from custom_components.voip_stack.core.sdp import RtpPcmFormat
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.inbound_routing import bridge
from custom_components.voip_stack.roster import RosterEntry
from custom_components.voip_stack.router import RouteAction, RouteDecision
from custom_components.voip_stack.sip_client import SipCallClient
from custom_components.voip_stack.sip_listener import SipUdpEndpoint
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


async def _exercise_factory(monkeypatch, *, factory, outbound=False, advisory=None, encoding="L16"):
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
            # Explicit policy, without advisory OPTIONS provenance.
            metadata.update(sip_audio_tx_formats=["L16/16000/1/20"], sip_audio_rx_formats=["L16/16000/1/20"])
        entry = RosterEntry("phone", sip_uri=contact, metadata=metadata)
        decision = RouteDecision(RouteAction.BRIDGE, target="phone", entry=entry)
        media = RtpPcmFormat(96, "L16", 16000, 1, 20)
        invite = SimpleNamespace(trunk_id="", call_id="inbound-factory", caller="Caller", target="phone",
            routing_caller="Caller", routing_target="phone", source_host="127.0.0.9", source_port=5090,
            send_format=media, recv_format=media, video_format=None, recv_video_format=None,
            remote_sdp="", signaling_transport="UDP")
        artifacts = SimpleNamespace(trunk_closed=False, trunk_info_queue=None)
        registry = Mock()
        registry.resource_for.return_value = None
        registry.current_generation.return_value = 1
        ports = SimpleNamespace(ports=(44000, 44002), bind_audio_sockets=Mock(), release=Mock())
        hass = SimpleNamespace()
        monkeypatch.setattr(trunk_inbound_router, "call_runtime_artifacts", lambda _: SimpleNamespace(artifacts_for=lambda _: artifacts))
        monkeypatch.setattr(trunk_inbound_router, "call_registry", lambda _: registry)
        monkeypatch.setattr(trunk_inbound_router, "trunk_config", lambda *_args, **_kwargs: {})
        monkeypatch.setattr(trunk_inbound_router, "async_build_peer_snapshot", AsyncMock(return_value=[]))
        monkeypatch.setattr(trunk_inbound_router, "registered_roster_entries", lambda _: [entry])
        monkeypatch.setattr(trunk_inbound_router, "roster_from_peers", lambda *_: [entry])
        monkeypatch.setattr(trunk_inbound_router, "route_inbound_trunk", lambda *_args, **_kwargs: decision)
        monkeypatch.setattr(trunk_inbound_router, "trunk_default_target", lambda _: "phone")
        monkeypatch.setattr(bridge, "take_delayed_offer_ports", lambda *_: ports)
        route_resolver = SimpleNamespace(route=lambda *_: decision, is_ha_target=lambda _: False,
            is_local_listener_uri=lambda _: False)
        runtime = SimpleNamespace(hass=hass, config={"sip_port": listener_port}, local_ip="127.0.0.1",
            ha_peer_name=lambda _: "HA", route_resolver=route_resolver,
            sip_uri_transport=sip_runtime.uri_transport,
            enable_reused_sip_tcp_connection=sip_runtime.enable_reused_signaling_flow)
        module = trunk_inbound_router if factory == "preanswered" else bridge
        real_profile = bridge.bridge_audio_profile

        def profile(*args, **kwargs):
            observed["profile_common"] = kwargs["include_common_codecs"]
            return real_profile(*args, **kwargs)

        monkeypatch.setattr(bridge, "bridge_audio_profile", profile)

        def client_factory(**kwargs):
            observed["client_common"] = kwargs["include_common_codecs"]
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
            if factory == "preanswered":
                runtime.ha_peer_name = "HA"
                await module.async_route_trunk_invite(runtime, invite, bridge_ports=ports)
            else:
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
        common_allowed = advisory is not False
        assert observed["result"] == ("in_call" if encoding == "L16" or common_allowed else "media_incompatible")
        if observed["result"] == "in_call":
            await asyncio.wait_for(delivered.acked.wait(), .5)
            invite_wire = next(m for m, _ in delivered.messages if m.method == "INVITE")
            ack = next(m for m, _ in delivered.messages if m.method == "ACK")
            assert invite_wire.uri == contact and ack.uri == contact
            assert ack.header("Route") == f"<{contact};lr>"
        assert not any(m.method == "OPTIONS" for peer in peers for m, _ in peer.messages)
        if encoding == "PCMU":
            assert observed["client_common"] is common_allowed
            if factory == "bridge":
                assert observed["profile_common"] is common_allowed
    finally:
        for client in clients:
            await client.close()
        if owner is not None:
            assert not owner._dialog_queues
        for transport in reversed(transports):
            transport.close()
        await asyncio.sleep(0)


@pytest.mark.parametrize("outbound", [False, True], ids=["ordinary-udp", "authenticated-flow"])
async def test_preanswered_factory_pins_wire_flow_and_preserves_ack_uri_route(monkeypatch, socket_enabled, outbound):
    await _exercise_factory(monkeypatch, factory="preanswered", outbound=outbound)


@pytest.mark.parametrize("factory", ["bridge", "preanswered"])
@pytest.mark.parametrize("advisory", [False, True, None], ids=["explicit-policy", "cached-options", "no-cache"])
async def test_inbound_factories_keep_common_audio_only_when_permitted(monkeypatch, socket_enabled, factory, advisory):
    await _exercise_factory(monkeypatch, factory=factory, advisory=advisory, encoding="PCMU")
