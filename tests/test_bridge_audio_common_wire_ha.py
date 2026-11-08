"""The real B2BUA prefers a common audio payload before answering the caller."""

import asyncio
from dataclasses import replace
import re
import socket
import struct

import pytest

from custom_components.voip_stack.core import rtp, sdp, sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.runtime_data import sip_endpoint_manager
from custom_components.voip_stack.sip_client import SipCallClient
from custom_components.voip_stack.sip_udp_io import SipDatagramQueueProtocol
from custom_components.voip_stack.peer_snapshot import async_build_peer_snapshot as build_real_peer_snapshot
from tests.test_sip_capabilities_ha import CAPS, register_peer, wait_until
from tests.test_trunk_calls_ha import lab as lab, trunks as trunks


pytestmark = pytest.mark.ha


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("source_rate,receive_rates,source_receive_rates,known,directional", [
    pytest.param(16000, (48000, 16000), (48000, 16000), True, True, id="explicit-capabilities"),
    pytest.param(16000, (48000, 16000), (48000, 16000), False, True, id="unknown-capabilities"),
    pytest.param(16000, (48000,), (48000, 16000), False, True, id="receiver-requires-48k"),
    pytest.param(48000, (48000, 16000), (48000, 16000), False, True, id="source-sends-48k"),
    pytest.param(48000, (48000, 16000), (48000,), False, True, id="browser-48k-both-directions"),
    pytest.param(16000, (48000, 16000), (16000,), False, False, id="ordinary-sip"),
])
async def test_bridge_prefers_common_audio_without_losing_conversion_fallbacks(
    trunks, monkeypatch, source_rate, receive_rates, source_receive_rates, known, directional,
    caller_discovery=None,
):
    lab = trunks
    target = lab.phones[0]
    metadata = {"endpoint_id": "sip:bedroom0", "endpoint_kind": "sip_account", "registered": True}
    if known:
        metadata.update(sip_audio_tx_formats=["L16/16000/1/10"],
            sip_audio_rx_formats=[f"L16/{rate}/1/10" for rate in receive_rates],
            sdp_features=["directional_audio_v1"])
    lab.roster[1] = replace(lab.roster[1], metadata=metadata)
    lab.runtime.endpoints.update("sip:bedroom0", availability="available")
    observed = {}

    def answer():
        request = target.last_request
        # A registered account alone never authorizes the private D1 profile.
        if not known:
            assert b"a=x-voip-stack-flow:" not in request.body
        offered = [fmt for fmt in sdp.offered_pcm_formats(request.body) if fmt.encoding == "L16"]
        # Independent endpoint: receive the first compatible payload, transmit
        # the fixed 16 kHz microphone stream. No production negotiation helper.
        receive = next(fmt for fmt in offered if fmt.sample_rate in receive_rates)
        send = next(fmt for fmt in offered if fmt.sample_rate == 16000)
        if not directional:
            send = receive
        observed.update(receive=receive, send=send, offer=request.body)
        selected = list(dict.fromkeys((receive, send)))
        payloads = " ".join(str(fmt.payload_type) for fmt in selected)
        mappings = "".join(f"a=rtpmap:{fmt.payload_type} L16/{fmt.sample_rate}/1\r\n" for fmt in selected)
        if directional:
            mappings += "".join(f"a=x-voip-stack-flow:{fmt.payload_type} "
                f"{'sendrecv' if send == receive else 'recv' if fmt == receive else 'send'}\r\n"
                for fmt in selected)
        body = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Fixed microphone phone\r\n"
            "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
            f"m=audio {target.rtp_socket.getsockname()[1]} RTP/AVP {payloads}\r\n"
            f"{mappings}a=ptime:10\r\na=sendrecv\r\n").encode()
        target.respond(request, target.remote, 200, body)
        target.answered.set()

    monkeypatch.setattr(target, "answer", answer)
    audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    audio.bind(("127.0.0.2", 0))
    audio.setblocking(False)
    caller = SipCallClient(local_ip="127.0.0.2", local_name="managed_caller" if caller_discovery else "Asymmetric caller",
        local_sip_port=0, local_rtp_port=audio.getsockname()[1],
        supported_send_formats=[AudioFormat(source_rate, frame_ms=10)],
        supported_recv_formats=[AudioFormat(rate, frame_ms=10) for rate in source_receive_rates],
        allow_directional_audio_payloads=directional)
    class CallerProtocol(SipDatagramQueueProtocol):
        def __init__(self):
            super().__init__(caller.queue)
            self.messages = asyncio.Queue()
            self.responses = []

        def datagram_received(self, raw, address):
            message = sip.parse_message(raw)
            if message.method == "OPTIONS":
                body = CAPS
                if caller_discovery == "incompatible":
                    body = ("v=0\r\no=- 3 3 IN IP4 127.0.0.2\r\ns=Capabilities\r\n"
                        "c=IN IP4 127.0.0.2\r\nt=0 0\r\nm=audio 0 RTP/AVP 96\r\n"
                        "a=rtpmap:96 L16/8000/1\r\na=sendrecv\r\n").encode()
                if caller_discovery != "no-ptime":
                    body += b"a=ptime:10\r\n"
                headers = [(key, message.header(key)) for key in ("Via", "From", "Call-ID", "CSeq")]
                headers += [("To", message.header("To") + ";tag=capabilities"),
                            ("Content-Type", "application/sdp")]
                self.transport.sendto(sip.build_response(200, "OK", headers, body), address)
                return
            self.messages.put_nowait((message, address))
            if message.is_response:
                self.responses.append(message)
            super().datagram_received(raw, address)

    caller.transport, caller.protocol = await lab.hass.loop.create_datagram_endpoint(
        CallerProtocol, local_addr=("127.0.0.2", 0))
    caller.local_sip_port = caller.transport.get_extra_info("sockname")[1]
    if caller_discovery is not None:
        build_request = caller._build_pending_invite

        def native_offer():
            # The actual caller explicitly limits receive packets to 10 ms.
            # SipCallClient otherwise omits redundant maxptime in D1 offers.
            if b"a=maxptime:" not in caller._pending_invite_body:
                caller._pending_invite_body = caller._pending_invite_body.replace(
                    b"a=ptime:10\r\n", b"a=ptime:10\r\na=maxptime:10\r\n")
                caller._local_sdp_body = caller._pending_invite_body.decode()
            return build_request()

        monkeypatch.setattr(caller, "_build_pending_invite", native_offer)
    try:
        if caller_discovery is not None:
            await restore_managed_caller(lab, caller, caller_discovery)
        result = await caller.invite(target="bedroom0", remote_host="127.0.0.1",
            remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3)
        if result == "ringing":
            result = await caller.wait_for_final(timeout=3)
        if caller_discovery == "incompatible":
            assert result == "media_incompatible"
            assert any(response.status_code == 488 for response in caller.protocol.responses)
            assert not target.invites and not lab.calls.sessions
            assert not lab.runtime.rtp_port_pool.get("used")
            return
        assert result == "in_call" and caller.dialog is not None
        session = lab.calls.get_session(caller.dialog_ids.call_id)
        relay = lab.calls.resource_for(session.call_id, "relay")
        expected_receive = source_rate if source_rate in receive_rates else receive_rates[0]
        assert observed["receive"].sample_rate == expected_receive
        assert caller.dialog.send_format.sample_rate == source_rate
        target_rate = observed["send"].sample_rate
        expected_return_rate = target_rate if target_rate in source_receive_rates else source_receive_rates[0]
        assert caller.dialog.recv_format.sample_rate == expected_return_rate
        # Static RX capacity may exceed the peer's TX packet budget. Keep the
        # existing fallback ready; actual compatible10ms packets still bypass it.
        if not relay.left_to_right_passthrough:
            assert relay.left_decoder is not None and relay.right_encoder is not None
            assert relay.left_to_right is not None
        if expected_return_rate == target_rate and not relay.right_to_left_passthrough:
            # Unknown peers retain legal 20 ms codec fallbacks in HA's offer.
            # A 10 ms caller therefore needs the existing packet reframer,
            # while both PCM contracts remain identical (no sample conversion).
            assert relay.right_to_left.src == relay.right_to_left.dst
        # Both final SDP contracts must agree with media and the UAS dialog.
        endpoint = sip_endpoint_manager(lab.hass)
        uas = next(server.active_dialogs[session.call_id] for server in
            endpoint._dialog_endpoints() if session.call_id in server.active_dialogs)
        assert uas.invite.send_format.sample_rate == caller.dialog.recv_format.sample_rate
        assert uas.invite.recv_format.sample_rate == caller.dialog.send_format.sample_rate
        if caller_discovery is not None:
            assert caller.dialog.send_format.frame_ms == caller.dialog.recv_format.frame_ms == 10
            assert sdp.parse_sdp(uas.invite.remote_sdp)["maxptime"] == 10
        source_samples = source_rate // 100
        source_payload = struct.pack(f"!{source_samples}h", *([2000] * source_samples))
        destination = (caller.dialog.remote_rtp_host, caller.dialog.remote_rtp_port)
        for index in range(6):
            await lab.hass.loop.sock_sendto(audio, rtp.build_packet(rtp.RtpPacket(
                caller.dialog.send_format.payload_type, index, index * source_samples, 901, source_payload)), destination)
        async with asyncio.timeout(3):
            packet = rtp.parse_packet((await target.audio.get())[0])
        assert packet.payload_type == observed["receive"].payload_type
        assert len(packet.payload) == expected_receive // 100 * 2
        assert max(struct.unpack(f"!{len(packet.payload)//2}h", packet.payload)) >= 1900
        assert relay.left_to_right_passthrough == (source_rate == expected_receive)
        if source_rate == expected_receive:
            assert packet.payload == source_payload
        target_samples = observed["send"].sample_rate // 100
        target_payload = struct.pack(f"!{target_samples}h", *([3000] * target_samples))
        target_port = int(re.search(rb"m=audio (\d+)", observed["offer"])[1])
        for index in range(1):
            await lab.hass.loop.sock_sendto(target.rtp_socket, rtp.build_packet(rtp.RtpPacket(
                observed["send"].payload_type, index, index * target_samples, 902, target_payload)),
                ("127.0.0.1", target_port))
        async with asyncio.timeout(3):
            returned = rtp.parse_packet(await lab.hass.loop.sock_recv(audio, 4096))
        assert returned.payload_type == caller.dialog.recv_format.payload_type
        assert len(returned.payload) == expected_return_rate // 100 * 2
        assert relay.right_to_left_passthrough == (expected_return_rate == target_rate)
        if expected_return_rate == target_rate:
            assert returned.payload == target_payload
        else:
            assert max(struct.unpack(f"!{len(returned.payload)//2}h", returned.payload)) >= 2900
        if not known:
            await lab.hass.loop.sock_sendto(target.rtp_socket, rtp.build_packet(rtp.RtpPacket(
                observed["send"].payload_type, 1, target_samples, 902, target_payload * 2)),
                ("127.0.0.1", target_port))
            async with asyncio.timeout(3):
                reframed = [rtp.parse_packet(await lab.hass.loop.sock_recv(audio, 4096)) for _ in range(2)]
            if expected_return_rate == target_rate:
                assert [packet.payload for packet in reframed] == [target_payload, target_payload]
            else:
                assert all(len(packet.payload) == expected_return_rate // 100 * 2 for packet in reframed)
                assert all(max(struct.unpack(f"!{len(packet.payload)//2}h", packet.payload)) >= 2900 for packet in reframed)
        assert caller.bye()
        await asyncio.wait_for(session.termination_started.wait(), 3)
        barrier = lab.calls.request_termination(session.call_id, session.termination_intent)
        if barrier is not None:
            await barrier
        assert not lab.calls.sessions and not lab.runtime.rtp_port_pool.get("used")
    finally:
        await caller.close()
        for session in tuple(lab.calls.sessions.values()):
            await lab.calls.request_termination(session.call_id, TerminationIntent("test_done"))
        audio.close()


async def restore_managed_caller(lab, caller, discovery):
    """Recreate a persisted managed owner, then learn its real REGISTER/OPTIONS."""
    from homeassistant.helpers import device_registry as dr, entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.voip_stack import peer_snapshot
    from custom_components.voip_stack.managed_phones import ManagedPhone, managed_records
    from custom_components.voip_stack.runtime_data import sip_registrar
    from custom_components.voip_stack.store import config_entry

    origin = MockConfigEntry(domain="esphome", data={"host": "127.0.0.2"})
    origin.add_to_hass(lab.hass)
    device = dr.async_get(lab.hass).async_get_or_create(config_entry_id=origin.entry_id,
        identifiers={("esphome", "84c7bb000001")},
        connections={(dr.CONNECTION_NETWORK_MAC, "84:c7:bb:00:00:01")}, name="Managed caller")
    state_entity = er.async_get(lab.hass).async_get_or_create("sensor", "esphome", "managed_caller_voip_state",
        device_id=device.id, config_entry=origin)
    lab.hass.states.async_set(state_entity.entity_id, "idle")
    phone = ManagedPhone(device.id, origin.entry_id, "84c7bb000001", "fixture-only",
        username="managed_caller", display_name="Managed caller", api_host="127.0.0.2", extension="430")
    entry = config_entry(lab.hass)
    lab.hass.config_entries.async_update_entry(entry, data={**entry.data,
        "managed_esphome_phones": {device.id: phone.record()}})
    restored = managed_records(entry)[device.id]
    registrar = sip_registrar(lab.hass)
    registrar.enabled = True
    registrar.update_accounts([restored.account()])
    # The runtime captured this fixture callable at startup. Restore the actual
    # resolver/projection behind that same callable, without replacing its owner.
    peer_snapshot.async_build_peer_snapshot.side_effect = build_real_peer_snapshot
    await register_peer(lab, caller.protocol, restored.username,
        contact=f"sip:opaque-contact@127.0.0.2:{caller.local_sip_port}")
    await wait_until(lambda: registrar.registered_contacts(restored.username)[0].capabilities is not None)
    entry = registrar.registered_roster_entries()[0]
    assert entry.metadata["sip_capabilities_known"] is True
    if discovery == "no-ptime":
        assert entry.metadata["sip_audio_tx_formats"] == entry.metadata["sip_audio_rx_formats"] == []
        assert entry.metadata["sdp_features"] == ["directional_audio_v1"]
    peers = await build_real_peer_snapshot(lab.hass)
    peer = next(item for item in peers if item.endpoint_id == restored.endpoint_id)
    assert peer.host == "127.0.0.2" and peer.sip_uri_user == restored.username
    assert peer.device["managed"] and peer.device["registered"]
    assert peer.capabilities == ("audio", "dtmf")
    while not caller.queue.empty():
        caller.queue.get_nowait()


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("discovery", ["no-ptime", "compatible", "incompatible"])
async def test_restored_managed_caller_uses_invite_when_discovery_has_no_profile(trunks, monkeypatch, discovery):
    await test_bridge_prefers_common_audio_without_losing_conversion_fallbacks(
        trunks, monkeypatch, 16000, (48000, 16000), (48000, 16000), False, True,
        caller_discovery=discovery)
