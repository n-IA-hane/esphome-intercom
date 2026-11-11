"""Advisory OPTIONS uses real signaling without owning a media session."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.voip_stack.core import sdp, sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.sip_listener import SipTcpServer, SipUdpEndpoint
from custom_components.voip_stack.sip_client import SipCallClient
from custom_components.voip_stack.sip_udp_io import SipDatagramQueueProtocol
from custom_components.voip_stack.sip_registrar import SipAccount, SipRegistrar
from custom_components.voip_stack.core.sip_auth import build_digest_authorization
from .test_trunk_calls_ha import lab as lab, trunks as trunks


pytestmark = pytest.mark.ha


class WirePeer(asyncio.DatagramProtocol):
    def __init__(self):
        self.messages = asyncio.Queue()

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, raw, address):
        self.messages.put_nowait((sip.parse_message(raw), address))


CAPS = ("v=0\r\no=- 3 3 IN IP4 127.0.0.1\r\ns=Independent capabilities\r\n"
    "c=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio 0 RTP/AVP 96 97\r\n"
    "a=rtpmap:96 L16/16000/1\r\na=rtpmap:97 L16/48000/1\r\n"
    "a=x-voip-stack-flow:96 send\r\na=x-voip-stack-flow:97 recv\r\n").encode()


INDEPENDENT_CAPS = (
    b"a=x-voip-stack-independent-ptime:1\r\n"
    b"a=x-voip-stack-audio-tx:L16/16000/1/20,L16/16000/1/10\r\n"
    b"a=x-voip-stack-audio-rx:L16/48000/1/10,L16/16000/1/20,L16/16000/1/10\r\n"
)


class RegisteredPeer(WirePeer):
    def __init__(self, outcome, *, independent=False):
        super().__init__()
        self.outcome = outcome
        self.independent = independent
        self.options = asyncio.Queue()
        self.invites = asyncio.Queue()

    def answer(self, request, address, status=200, body=b"", content_type="application/sdp"):
        headers = [(key, request.header(key)) for key in ("Via", "From", "Call-ID", "CSeq")]
        headers.append(("To", request.header("To") if ";tag=" in request.header("To")
            else request.header("To") + ";tag=independent"))
        if body:
            headers.append(("Content-Type", content_type))
        self.transport.sendto(sip.build_response(status, "OK" if status == 200 else "Rejected",
            headers, body), address)

    def datagram_received(self, raw, address):
        request = sip.parse_message(raw)
        if request.is_response:
            self.messages.put_nowait((request, address))
        elif request.method == "OPTIONS":
            self.options.put_nowait((request, address))
            if self.outcome == "timeout":
                return
            if self.outcome in {401, 405}:
                self.answer(request, address, self.outcome)
            elif self.outcome == "empty":
                self.answer(request, address)
            else:
                self.answer(request, address, body=(CAPS.replace(b"flow:96 send\r\n", b"flow:96 sendrecv\r\n")
                    if self.independent else CAPS) + (INDEPENDENT_CAPS if self.independent else b"")
                    + (b"a=ptime:10\r\n" if self.outcome == "known" else b""),
                    content_type="text/plain" if self.outcome == "wrong-type" else "application/sdp")
        elif request.method == "INVITE":
            self.invites.put_nowait(request)
            # Deliberately ignore cached preferences. The final answer selects
            # ordinary L16/16000/20, which must remain in the complete offer.
            import re
            match = re.search(rb"a=rtpmap:(\d+) L16/16000/1", request.body)
            if match is None:
                self.answer(request, address, 488)
                return
            pt = match.group(1).decode()
            body = ("v=0\r\no=- 4 4 IN IP4 127.0.0.1\r\ns=Final answer\r\n"
                "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
                f"m=audio 35000 RTP/AVP {pt}\r\na=rtpmap:{pt} L16/16000/1\r\n"
                "a=ptime:20\r\na=sendrecv\r\n").encode()
            self.answer(request, address, body=body)
        elif request.method != "ACK":
            self.answer(request, address)


async def register_peer(lab, peer, username="caps", *, call_id="registration", cseq=1,
                        contact=None, expires=60):
    uri = f"sip:127.0.0.1:{lab.runtime.transport_config['sip_port']}"
    port = peer.transport.get_extra_info("sockname")[1]
    contact = contact or f"sip:{username}@127.0.0.1:{port}"

    async def send(sequence, authorization=None):
        headers = [("Via", f"SIP/2.0/UDP 127.0.0.1:{port};branch=z9hG4bK-{call_id}-{sequence};rport"),
            ("From", f"<sip:{username}@127.0.0.1>;tag=register"), ("To", f"<sip:{username}@127.0.0.1>"),
            ("Call-ID", call_id), ("CSeq", f"{sequence} REGISTER"), ("Contact", f"<{contact}>"),
            ("Expires", str(expires))]
        if authorization:
            headers.append(("Authorization", authorization))
        peer.transport.sendto(sip.build_request("REGISTER", uri, headers),
            ("127.0.0.1", lab.runtime.transport_config["sip_port"]))
        response, _ = await asyncio.wait_for(peer.messages.get(), 2)
        return response

    challenge = await send(cseq)
    assert challenge.status_code == 401
    authorization = build_digest_authorization(challenge_header=challenge.header("WWW-Authenticate"),
        username=username, password="fixture-only", method="REGISTER", uri=uri)
    response = await send(cseq + 1, authorization)
    assert response.status_code == 200


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("source", ["managed-capabilities", "registered-contact"])
@pytest.mark.parametrize("independent", [False, True])
async def test_public_browser_originate_keeps_directional_pcm_and_receiver_ptime(
    trunks, monkeypatch, source, independent, record_property,
):
    """Real public origination and answer parsing cannot downgrade48/10 to16/10."""
    import re
    import struct

    from custom_components.voip_stack import softphone_originate
    from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
    from custom_components.voip_stack.roster import dump_roster_json
    from custom_components.voip_stack.core import rtp
    from custom_components.voip_stack.rtp_audio_receiver import RtpAudioReceiver

    lab = trunks
    source_phone = PhoneEndpoint(endpoint_id="browser:pcm", name="Browser PCM",
        device_id="browser-pcm-device", kind=EndpointKind.BROWSER, availability="available")
    lab.runtime.endpoints.register(source_phone)
    offers = []

    def independent_answer(request, media_port):
        offers.append(request.body.decode())
        # The peer reads the actual offer's payload identities, but independently
        # selects48k receive/10ms and16k transmit/20ms. No production SDP builder.
        mappings = {int(rate): int(pt) for pt, rate in re.findall(
            rb"a=rtpmap:(\d+) L16/(16000|48000)/1\r\n", request.body)}
        wide, narrow = mappings[48000], mappings[16000]
        return ("v=0\r\no=- 7 1 IN IP4 127.0.0.1\r\ns=Independent PCM answer\r\n"
            "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
            f"m=audio {media_port} RTP/AVP {wide} {narrow}\r\n"
            f"a=rtpmap:{wide} L16/48000/1\r\na=rtpmap:{narrow} L16/16000/1\r\n"
            f"a=x-voip-stack-flow:{wide} recv\r\na=x-voip-stack-flow:{narrow} send\r\n"
            + ("a=x-voip-stack-independent-ptime:1\r\n" if independent else "")
            + "a=ptime:10\r\na=maxptime:10\r\na=sendrecv\r\n").encode()

    transport = None
    if source == "registered-contact":
        registrar = lab.calls.component("registrar")
        registrar.enabled = True
        registrar.update_accounts([*registrar.accounts.values(), SipAccount("caps", "Caps", "fixture-only")])
        transport, peer = await lab.hass.loop.create_datagram_endpoint(
            lambda: RegisteredPeer("known", independent=independent), local_addr=("127.0.0.1", 0))
        receive = peer.datagram_received

        def datagram_received(raw, address):
            request = sip.parse_message(raw)
            if request.method == "INVITE":
                peer.answer(request, address, body=independent_answer(request, 35000))
            else:
                receive(raw, address)

        monkeypatch.setattr(peer, "datagram_received", datagram_received)
        await register_peer(lab, peer)
        await wait_until(lambda: registrar.registered_contacts("caps")[0].capabilities is not None)
        lab.roster.extend(registrar.registered_roster_entries())
        destination = "caps"
    else:
        phone = lab.phones[0]
        lab.runtime.endpoints.update("sip:bedroom0", availability="available")

        def answer():
            phone.respond(phone.last_request, phone.remote, 200,
                independent_answer(phone.last_request, phone.rtp_socket.getsockname()[1]))

        monkeypatch.setattr(phone, "answer", answer)
        lab.roster[1] = replace(lab.roster[1], metadata={
            **lab.roster[1].metadata, "endpoint_kind": "esphome",
            "tx_formats": (["16000:s16le:1:20"] if independent else []) + ["16000:s16le:1:10"],
            "rx_formats": ["48000:s16le:1:10", "16000:s16le:1:20", "16000:s16le:1:10"],
            "sdp_features": ["directional_audio_v1"] + (["independent_audio_ptime_v1"] if independent else []),
        })
        destination = "bedroom0"
    lab.hass.states.async_set("sensor.voip_phonebook", "ready", {"roster_json": dump_roster_json(lab.roster)})
    monkeypatch.setattr(softphone_originate, "_ha_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(softphone_originate, "_resolve_target_device", AsyncMock(return_value=None))

    def current_client():
        active = lab.runtime.endpoints.require(source_phone.endpoint_id).active_call_id
        return lab.calls.sip_client_for(active) if active else None

    call_id = ""
    try:
        await lab.hass.services.async_call("voip_stack", "call", {
            "device_id": source_phone.device_id, "destination": destination,
        }, blocking=True)
        await wait_until(lambda: (client := current_client()) is not None and client.dialog is not None)
        call_id = lab.runtime.endpoints.require(source_phone.endpoint_id).active_call_id
        dialog = lab.calls.sip_client_for(call_id).dialog
        record_property("wire_offer", offers[0])
        assert (dialog.send_format.sample_rate, dialog.send_format.frame_ms) == (48000, 10)
        assert (dialog.recv_format.sample_rate, dialog.recv_format.frame_ms) == (16000, 20 if independent else 10)
        receiver = RtpAudioReceiver(dialog.recv_format, dialog.recv_formats)
        for count in ((160, 320) if independent else (160,)):
            samples = [1000, -2000] * (count // 2)
            assert b"".join(receiver.decode(dialog.recv_format.payload_type,
                struct.pack(f">{count}h", *samples))) == struct.pack(f"<{count}h", *samples)
        limit = rtp.audio_payload_size_limit(dialog.recv_format)
        with pytest.raises(rtp.RtpError, match="too large"):
            receiver.decode(dialog.recv_format.payload_type, b"\0" * (limit + 2))
        assert f"a=ptime:{20 if independent else 10}\r\n" in offers[0]
        mapping_order = re.findall(r"a=rtpmap:\d+ L16/(16000|48000)/1", offers[0])
        assert mapping_order.index("48000") < mapping_order.index("16000")
    finally:
        call_id = call_id or lab.runtime.endpoints.require(source_phone.endpoint_id).active_call_id
        if call_id:
            await lab.hass.services.async_call("voip_stack", "hangup", {
                "device_id": source_phone.device_id, "call_id": call_id,
            }, blocking=True)
        if transport is not None:
            transport.close()
    assert not lab.calls.sessions


@pytest.mark.parametrize("independent", [False, True])
async def test_peer_origin_full_sdp_uses_ha_receiver_quantum_and_rejects_only_video(socket_enabled, independent):
    from custom_components.voip_stack.core.audio_format import (
        HA_SIP_PCM_FORMATS, HA_SIP_PCM_TX_FORMATS, HA_SIP_PCM_RX_FORMATS,
    )
    from custom_components.voip_stack.sip_listener import SipInviteResult

    accepted = []

    async def answer(invite):
        accepted.append(invite)
        body = sdp.build_answer_directional("127.0.0.1", "127.0.0.1", 35000,
            invite.send_format, invite.recv_format, remote_sdp=invite.remote_sdp)
        return SipInviteResult(200, "OK", answer_sdp=body)

    endpoint = SipUdpEndpoint(local_ip="127.0.0.1", local_rtp_port=35000,
        supported_formats=list(HA_SIP_PCM_FORMATS),
        supported_send_formats=list(HA_SIP_PCM_TX_FORMATS),
        supported_recv_formats=list(HA_SIP_PCM_RX_FORMATS), on_invite=answer)
    loop = asyncio.get_running_loop()
    server, _ = await loop.create_datagram_endpoint(lambda: endpoint, local_addr=("127.0.0.1", 0))
    transport, peer = await loop.create_datagram_endpoint(WirePeer, local_addr=("127.0.0.1", 0))
    listener = server.get_extra_info("sockname")
    address = transport.get_extra_info("sockname")
    endpoint.local_sip_port = listener[1]
    uri = f"sip:ha@127.0.0.1:{listener[1]}"
    offer = ("v=0\r\no=- 7 1 IN IP4 127.0.0.1\r\ns=Independent ESP-shaped offer\r\n"
        "c=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio 36000 RTP/AVP 96 97\r\n"
        "a=rtpmap:96 L16/16000/1\r\na=rtpmap:97 L16/48000/1\r\n"
        "a=x-voip-stack-flow:96 send\r\na=x-voip-stack-flow:97 recv\r\n"
        + ("a=x-voip-stack-independent-ptime:1\r\n" if independent else "")
        + "a=ptime:10\r\na=maxptime:10\r\na=sendrecv\r\n"
        "m=video 36002 RTP/AVP 102\r\na=rtpmap:102 H264/90000\r\na=sendrecv\r\n")

    async def response(status):
        async with asyncio.timeout(2):
            while True:
                message, _ = await peer.messages.get()
                if message.status_code == status:
                    return message

    headers = [("Via", f"SIP/2.0/UDP 127.0.0.1:{address[1]};branch=z9hG4bK-ptime;rport"),
        ("From", "<sip:independent@127.0.0.1>;tag=peer"), ("To", "<sip:ha@127.0.0.1>"),
        ("Call-ID", "independent-receiver-ptime"), ("CSeq", "1 INVITE"),
        ("Contact", f"<sip:independent@127.0.0.1:{address[1]}>"), ("Content-Type", "application/sdp")]
    try:
        transport.sendto(sip.build_request("INVITE", uri, headers, offer.encode()), listener)
        answered = await response(200)
        invite, = accepted
        assert (invite.send_format.sample_rate, invite.send_format.frame_ms) == (48000, 10)
        assert (invite.recv_format.sample_rate, invite.recv_format.frame_ms) == (16000, 20 if independent else 10)
        assert f"a=ptime:{20 if independent else 10}\r\n" in answered.body.decode()
        assert "m=video 0 RTP/AVP 102\r\n" in answered.body.decode()
        dialog_headers = [(key, value) for key, value in headers if key not in {"To", "CSeq", "Content-Type"}]
        dialog_headers.append(("To", answered.header("To")))
        transport.sendto(sip.build_request("ACK", uri, [*dialog_headers, ("CSeq", "1 ACK")]), listener)
        transport.sendto(sip.build_request("BYE", uri, [*dialog_headers, ("CSeq", "2 BYE")]), listener)
        assert (await response(200)).header("CSeq") == "2 BYE"
        assert not endpoint.pending_invites and not endpoint.active_dialogs
    finally:
        transport.close()
        server.close()


async def wait_until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(.005)


def authorized_register(registrar, *, username="caps", call_id="registration", cseq=1,
                        contact="sip:caps@127.0.0.1:41000", transport="UDP"):
    uri = "sip:127.0.0.1:5060"
    authorization = build_digest_authorization(challenge_header=registrar._challenge()[1],
        username=username, password="fixture-only", method="REGISTER", uri=uri)
    return sip.parse_message(sip.build_request("REGISTER", uri, [
        ("Via", f"SIP/2.0/{transport} 127.0.0.1:41000;branch=z9hG4bK-{cseq};rport"),
        ("From", f"<sip:{username}@127.0.0.1>;tag=register"),
        ("To", f"<sip:{username}@127.0.0.1>"), ("Call-ID", call_id),
        ("CSeq", f"{cseq} REGISTER"), ("Contact", f"<{contact}>"), ("Expires", "60"),
        ("Authorization", authorization)]))


@pytest.mark.parametrize("change", ["same", "contact", "host", "port", "transport", "call-id"])
async def test_authenticated_renewal_preserves_only_identical_binding_cache(change):
    registrar = SipRegistrar(enabled=True, accounts=[SipAccount("caps", "Caps", "fixture-only")],
        local_ip="127.0.0.1", local_sip_port=5060)
    first = authorized_register(registrar)
    assert (await registrar.handle_register(first, ("127.0.0.1", 41000), "UDP")).status == 200
    old = registrar.registered_contacts("caps")[0]
    old.capabilities = sdp.options_capabilities(CAPS + b"a=ptime:10\r\n")
    request = authorized_register(registrar, cseq=2,
        contact="sip:caps@127.0.0.1:42000" if change == "contact" else "sip:caps@127.0.0.1:41000",
        call_id="new-registration" if change == "call-id" else "registration",
        transport="TCP" if change == "transport" else "UDP")
    source = ("127.0.0.2" if change == "host" else "127.0.0.1", 42000 if change == "port" else 41000)
    assert (await registrar.handle_register(request, source, "TCP" if change == "transport" else "UDP")).status == 200
    updated = next(item for item in registrar.registered_contacts("caps") if item.cseq == 2)
    assert (updated.capabilities == old.capabilities) == (change == "same")
    assert (updated.capability_generation == old.capability_generation) == (change == "same")


@pytest.mark.parametrize("change", ["renewal", "contact", "flow", "call-id", "generation", "aba", "registrar", "manager"])
async def test_late_discovery_publishes_only_to_its_current_binding_lifetime(monkeypatch, change):
    from custom_components.voip_stack import sip_capabilities as module
    from custom_components.voip_stack import config_entry_runtime

    registrar = SipRegistrar(enabled=True, accounts=[SipAccount("caps", "Caps", "fixture-only")],
        local_ip="127.0.0.1", local_sip_port=5060)
    assert (await registrar.handle_register(authorized_register(registrar), ("127.0.0.1", 41000), "UDP")).status == 200
    binding = registrar.registered_contacts("caps")[0]
    future = asyncio.get_running_loop().create_future()
    async def options(*_args):
        return await future
    manager = SimpleNamespace(async_options=options)
    current = {"registrar": registrar, "manager": manager}
    monkeypatch.setattr(module, "sip_registrar", lambda _hass: current["registrar"])
    monkeypatch.setattr(module, "sip_endpoint_manager", lambda _hass: current["manager"])
    refresh = AsyncMock()
    monkeypatch.setattr(config_entry_runtime, "async_refresh_and_push_phonebook", refresh)
    task = asyncio.create_task(module.async_probe(object(), "caps"))
    await asyncio.sleep(0)
    if change in {"renewal", "contact", "flow", "call-id"}:
        request = authorized_register(registrar, cseq=2,
            contact="sip:caps@127.0.0.1:42000" if change == "contact" else binding.advertised_contact_uri,
            call_id="new-call-id" if change == "call-id" else "registration")
        if change == "contact":
            registrar.registrations.clear()
        assert (await registrar.handle_register(request,
            ("127.0.0.1", 42000 if change == "flow" else 41000), "UDP")).status == 200
    elif change == "generation":
        registrar.registrations = {"caps": replace(binding, capability_generation="replacement-generation")}
    elif change == "aba":
        registrar.registrations.clear()
        assert (await registrar.handle_register(authorized_register(registrar, cseq=2),
            ("127.0.0.1", 41000), "UDP")).status == 200
    elif change == "registrar":
        current["registrar"] = SipRegistrar(enabled=True, accounts=[], local_ip="127.0.0.1", local_sip_port=5060)
    elif change == "manager":
        current["manager"] = object()
    future.set_result(sip.parse_message(sip.build_response(200, "OK", [("Content-Type", "application/sdp")], CAPS)))
    await task
    if change == "renewal":
        assert registrar.registered_contacts("caps")[0].capabilities["sdp_features"] == ["directional_audio_v1"]
        refresh.assert_awaited_once()
    else:
        assert all(item.capabilities is None for item in registrar.registered_contacts("caps"))
        refresh.assert_not_awaited()


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_two_accounts_on_same_udp_tuple_do_not_share_capabilities(trunks):
    lab = trunks
    registrar = lab.calls.component("registrar")
    registrar.enabled = True
    registrar.update_accounts([SipAccount(name, name, "fixture-only") for name in ("first", "second")])
    transport, peer = await lab.hass.loop.create_datagram_endpoint(
        lambda: RegisteredPeer("timeout"), local_addr=("127.0.0.1", 0))
    try:
        await register_peer(lab, peer, "first", call_id="first-register")
        first_request, first_address = await asyncio.wait_for(peer.options.get(), 1)
        await register_peer(lab, peer, "second", call_id="second-register")
        second_request, second_address = await asyncio.wait_for(peer.options.get(), 1)
        assert "first@" in first_request.uri and "second@" in second_request.uri
        peer.answer(first_request, first_address, body=CAPS + b"a=ptime:10\r\n")
        peer.answer(second_request, second_address, 405)
        await wait_until(lambda: not lab.runtime.capability_probes)
        assert registrar.registered_contacts("first")[0].capabilities["sdp_features"] == ["directional_audio_v1"]
        assert registrar.registered_contacts("second")[0].capabilities is None
        first = registrar.registered_contacts("first")[0]
        generation, cached = first.capability_generation, first.capabilities
        await register_peer(lab, peer, "first", call_id="first-register", cseq=3)
        current = registrar.registered_contacts("first")[0]
        assert current.capability_generation == generation and current.capabilities is cached
        assert peer.options.empty()
    finally:
        transport.close()


@pytest.mark.parametrize("ptime", [None, 0])
async def test_incomplete_options_never_invents_packet_quantum(ptime):
    body = CAPS + (f"a=ptime:{ptime}\r\n".encode() if ptime is not None else b"")
    capabilities = sdp.options_capabilities(body)
    assert capabilities == {"sip_audio_tx_formats": [], "sip_audio_rx_formats": [],
        "sdp_features": ["directional_audio_v1"], "audio_mode": "full_duplex"}
    with pytest.raises(sdp.SdpError):
        sdp.parse_sdp(body)


async def test_directional_provenance_cannot_come_from_video_section():
    body = CAPS.replace(b"a=x-voip-stack-flow:96 send\r\na=x-voip-stack-flow:97 recv\r\n", b"")
    body += b"m=video 0 RTP/AVP 96 97\r\na=x-voip-stack-flow:96 send\r\na=x-voip-stack-flow:97 recv\r\n"
    assert sdp.options_capabilities(body)["sdp_features"] == []


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("outcome", ["timeout", 401, 405, "empty", "no-ptime", "known", "wrong-type"])
async def test_register_discovery_is_advisory_and_final_invite_remains_authoritative(trunks, outcome):
    lab = trunks
    registrar = lab.calls.component("registrar")
    registrar.enabled = True
    registrar.update_accounts([*registrar.accounts.values(), SipAccount("caps", "Caps", "fixture-only")])
    transport, peer = await lab.hass.loop.create_datagram_endpoint(
        lambda: RegisteredPeer(outcome), local_addr=("127.0.0.1", 0))
    caller = SipCallClient(local_ip="127.0.0.2", local_name="Independent caller", local_sip_port=0,
        local_rtp_port=36000, supported_formats=[AudioFormat(16000, frame_ms=20)])
    caller.transport, caller.protocol = await lab.hass.loop.create_datagram_endpoint(
        lambda: SipDatagramQueueProtocol(caller.queue), local_addr=("127.0.0.2", 0))
    caller.local_sip_port = caller.transport.get_extra_info("sockname")[1]
    try:
        await register_peer(lab, peer)
        request, _ = await asyncio.wait_for(peer.options.get(), 1)
        assert request.header("Accept") == "application/sdp"
        assert not lab.calls.sessions
        if outcome in {"known", "no-ptime"}:
            await wait_until(lambda: registrar.registered_contacts("caps")[0].capabilities is not None)
        elif outcome != "timeout":
            await wait_until(lambda: not lab.runtime.capability_probes)
        binding = registrar.registered_contacts("caps")[0]
        if outcome in {"known", "no-ptime"}:
            assert binding.capabilities["sdp_features"] == ["directional_audio_v1"]
            assert binding.capabilities["sip_audio_tx_formats"] == (["L16/16000/1/10"] if outcome == "known" else [])
        else:
            assert binding.capabilities is None
        # Use the real registrar export as routing input; no manual capability
        # injection or endpoint-brand classification participates.
        lab.roster.extend(registrar.registered_roster_entries())
        result = await caller.invite(target="caps", remote_host="127.0.0.1",
            remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=2)
        if result == "ringing":
            result = await caller.wait_for_final(timeout=2)
        assert result == "in_call"
        offer = await asyncio.wait_for(peer.invites.get(), 1)
        assert (b"a=x-voip-stack-flow:" in offer.body) == (outcome in {"known", "no-ptime"})
        assert caller.dialog.send_format.sample_rate == 16000
        assert caller.dialog.send_format.frame_ms == 20
        assert caller.dialog.recv_format.frame_ms == 20
        caller.bye()
        await wait_until(lambda: not lab.calls.sessions)
        if outcome == "timeout":
            await asyncio.wait_for(asyncio.gather(*lab.runtime.capability_probes.values()), 4)
            assert binding.capabilities is None
            assert not lab.runtime.capability_probes
    finally:
        await caller.close()
        transport.close()


async def test_discovery_response_from_other_socket_cannot_establish_provenance(socket_enabled):
    async def unexpected_invite(_invite):
        pytest.fail("Probe entered call admission")
    loop = asyncio.get_running_loop()
    endpoint = SipUdpEndpoint(local_ip="127.0.0.1", local_rtp_port=34000,
        supported_formats=[AudioFormat(16000, frame_ms=10)], on_invite=unexpected_invite)
    server, _ = await loop.create_datagram_endpoint(lambda: endpoint, local_addr=("127.0.0.1", 0))
    transport, peer = await loop.create_datagram_endpoint(WirePeer, local_addr=("127.0.0.1", 0))
    foreign, _ = await loop.create_datagram_endpoint(WirePeer, local_addr=("127.0.0.1", 0))
    task = None
    try:
        address = transport.get_extra_info("sockname")
        task = asyncio.create_task(endpoint.async_options(f"sip:peer@127.0.0.1:{address[1]}", address, timeout=.3))
        request, received_at = await asyncio.wait_for(peer.messages.get(), 1)
        headers = [(name, request.header(name)) for name in ("Via", "From", "Call-ID", "CSeq")]
        headers += [("To", request.header("To") + ";tag=peer"), ("Content-Type", "application/sdp")]
        foreign.sendto(sip.build_response(200, "OK", headers, CAPS), received_at)
        # The legitimate response arrives later with no usable capabilities.
        await asyncio.sleep(.01)
        assert not task.done()
        transport.sendto(sip.build_response(405, "Method Not Allowed", headers), received_at)
        assert (await task).status_code == 405
        assert not endpoint._client_transaction_responses and not endpoint.active_dialogs
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        foreign.close()
        transport.close()
        server.close()
        await asyncio.sleep(0)


async def read_stream_message(reader):
    header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 1)
    length = next((int(line.split(b":", 1)[1]) for line in header.split(b"\r\n")
        if line.lower().startswith(b"content-length:")), 0)
    return sip.parse_message(header + await reader.readexactly(length))


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_registered_tcp_discovery_reuses_authenticated_connection_and_has_no_media_owner(trunks):
    lab = trunks
    registrar = lab.calls.component("registrar")
    registrar.enabled = True
    registrar.update_accounts([SipAccount("caps", "Caps", "fixture-only")])
    reader, writer = await asyncio.open_connection("127.0.0.1", lab.runtime.transport_config["sip_port"])
    try:
        # Authenticate an actual REGISTER over the accepted TCP connection.
        request = authorized_register(registrar, transport="TCP")
        writer.write(sip.build_request(request.method, request.uri, request.headers, request.body))
        await writer.drain()
        messages = [await read_stream_message(reader), await read_stream_message(reader)]
        response = next(item for item in messages if item.is_response)
        assert response.status_code == 200 and response.header("CSeq").endswith(" REGISTER")
        probe = next(item for item in messages if item.is_request)
        assert probe.method == "OPTIONS" and probe.header("Accept") == "application/sdp"
        assert not lab.calls.sessions
        binding = registrar.registered_contacts("caps")[0]
        assert (binding.source_host, binding.source_port) == writer.get_extra_info("sockname")
        headers = [(key, probe.header(key)) for key in ("Via", "From", "Call-ID", "CSeq")]
        headers += [("To", probe.header("To") + ";tag=tcp"), ("Content-Type", "application/sdp")]
        writer.write(sip.build_response(200, "OK", headers, CAPS))
        await writer.drain()
        await wait_until(lambda: binding.capabilities is not None)
        assert binding.capabilities["sdp_features"] == ["directional_audio_v1"]
        assert not binding.capabilities["sip_audio_tx_formats"]
        assert not lab.calls.sessions
        from custom_components.voip_stack.runtime_data import sip_endpoint_manager
        manager = sip_endpoint_manager(lab.hass)
        assert len(manager.tcp_server._tcp_writers) == 1
        assert not manager.tcp_server.endpoint._client_transaction_responses
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.parametrize("signaling", ["UDP", "TCP"])
@pytest.mark.parametrize("accept,wants_sdp", [
    pytest.param(None, True, id="absent"),
    pytest.param("", False, id="empty"),
    pytest.param("text/plain", False, id="other-type"),
    pytest.param("application/sdp;q=0", False, id="excluded"),
    pytest.param("application/sdp", True, id="explicit"),
    pytest.param("application/sdp;q=0.5", True, id="weighted"),
])
async def test_options_wire_describes_audio_without_allocating_a_call(socket_enabled, tmp_path, signaling, accept, wants_sdp):
    async def unexpected_invite(_invite):
        pytest.fail("OPTIONS entered the call admission owner")

    loop = asyncio.get_running_loop()
    config = dict(local_ip="127.0.0.1", local_rtp_port=34000,
        supported_formats=[AudioFormat(16000, frame_ms=10)],
        supported_send_formats=[AudioFormat(16000, frame_ms=10)],
        supported_recv_formats=[AudioFormat(48000, frame_ms=10)],
        on_invite=unexpected_invite)
    if signaling == "UDP":
        endpoint = SipUdpEndpoint(**config)
        server, _ = await loop.create_datagram_endpoint(lambda: endpoint, local_addr=("127.0.0.1", 0))
        transport, peer = await loop.create_datagram_endpoint(WirePeer, local_addr=("127.0.0.1", 0))
        address = server.get_extra_info("sockname")
        source = transport.get_extra_info("sockname")
    else:
        server = SipTcpServer(host="127.0.0.1", port=0, **config)
        assert await server.start()
        endpoint = server.endpoint
        address = server.server.sockets[0].getsockname()
        reader, writer = await asyncio.open_connection(*address)
        source = writer.get_extra_info("sockname")
    try:
        request_uri = f"sip:ha@127.0.0.1:{address[1]}"
        headers = [("Via", f"SIP/2.0/{signaling} 127.0.0.1:{source[1]};branch=z9hG4bK-caps;rport"),
            ("From", "<sip:peer@127.0.0.1>;tag=peer"), ("To", f"<{request_uri}>"),
            ("Call-ID", "capabilities-only"), ("CSeq", "1 OPTIONS")]
        if accept is not None:
            headers.append(("Accept", accept))
        request = sip.build_request("OPTIONS", request_uri, headers)
        (tmp_path / "request.sip").write_bytes(request)
        if signaling == "UDP":
            transport.sendto(request, address)
            response, _ = await asyncio.wait_for(peer.messages.get(), 1)
        else:
            writer.write(request)
            await writer.drain()
            response = await read_stream_message(reader)
        (tmp_path / "response.json").write_text(json.dumps({
            "status": response.status_code, "headers": response.headers,
            "body": response.body.decode(),
        }, indent=2))
        assert response.status_code == 200
        if wants_sdp:
            assert response.header("Content-Type") == "application/sdp"
            assert b"m=audio 0 RTP/AVP " in response.body
            assert b"L16/16000/1" in response.body and b"L16/48000/1" in response.body
            assert b"a=x-voip-stack-flow:" in response.body
        else:
            assert not response.body and not response.header("Content-Type")
        assert b"m=video" not in response.body
        assert not endpoint.active_dialogs and not endpoint.pending_invites
        assert not endpoint._client_transaction_responses
    finally:
        if signaling == "UDP":
            transport.close()
            server.close()
        else:
            writer.close()
            await writer.wait_closed()
            await server.stop()
        await asyncio.sleep(0)


@pytest.mark.parametrize("independent", [False, True])
@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_capability_upgrade_replaces_previous_contact_generation(trunks, independent):
    lab = trunks
    registrar = lab.calls.component("registrar")
    registrar.enabled = True
    registrar.update_accounts([*registrar.accounts.values(), SipAccount("caps", "Caps", "fixture-only")])
    transport, peer = await lab.hass.loop.create_datagram_endpoint(
        lambda: RegisteredPeer("no-ptime", independent=not independent), local_addr=("127.0.0.1", 0))
    try:
        await register_peer(lab, peer, call_id="firmware-before")
        await wait_until(lambda: registrar.registered_contacts("caps")[0].capabilities is not None)
        old = registrar.registered_contacts("caps")[0]
        assert ("independent_audio_ptime_v1" in old.capabilities["sdp_features"]) == (not independent)
        peer.independent = independent
        await register_peer(lab, peer, call_id="firmware-after")
        await wait_until(lambda: (current := registrar.registered_contacts("caps")[0]).capability_generation
            != old.capability_generation and current.capabilities is not None)
        current = registrar.registered_contacts("caps")[0]
        assert current.call_id == "firmware-after"
        assert ("independent_audio_ptime_v1" in current.capabilities["sdp_features"]) == independent
        assert current.capabilities is not old.capabilities
    finally:
        await register_peer(lab, peer, call_id="firmware-after", cseq=3, expires=0)
        transport.close()


@pytest.mark.parametrize("independent", [False, True])
def test_options_packet_quanta_override_stale_managed_peer(independent):
    from custom_components.voip_stack.endpoint_routing import sip_target_rtp_audio_profile
    from custom_components.voip_stack.peer import Peer

    # Fresh authenticated Contact capabilities have a10ms receiver. Its
    # transmitter preferences must come from its explicit TX list, not ptime.
    body = (CAPS.replace(b"flow:96 send\r\n", b"flow:96 sendrecv\r\n")
        if independent else CAPS) + (INDEPENDENT_CAPS if independent else b"") + b"a=ptime:10\r\n"
    capabilities = sdp.options_capabilities(body)
    stale = Peer(name="managed", host="192.0.2.2",
        sip_audio_tx_formats=("L16/16000/1/10",),
        sip_audio_rx_formats=("L16/48000/1/10",),
        sdp_features=frozenset({"directional_audio_v1"}
            | ({"independent_audio_ptime_v1"} if not independent else set())))
    selected_contact = SimpleNamespace(metadata={**capabilities, "sip_capabilities_known": True})
    profile = sip_target_rtp_audio_profile(stale, selected_contact)
    assert profile is not None
    assert ("independent_audio_ptime_v1" in profile.sdp_features) == independent
    assert (profile.send_rtp_formats[0].sample_rate, profile.send_rtp_formats[0].frame_ms) == (48000, 10)
    assert (profile.recv_rtp_formats[0].sample_rate, profile.recv_rtp_formats[0].frame_ms) == (16000, 20 if independent else 10)


def test_options_valid_packet_time_is_not_limited_by_offer_presets():
    result = sdp.options_capabilities(CAPS + b"a=ptime:30\r\n")
    assert result["sip_audio_tx_formats"] == ["L16/16000/1/30"]
    assert result["sip_audio_rx_formats"] == ["L16/48000/1/30"]
