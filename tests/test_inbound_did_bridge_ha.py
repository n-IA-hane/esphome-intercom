"""Incoming DID wire reaches independent destinations through native HA routing."""

import asyncio
import re
import socket
import struct

import pytest
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.script import Script, async_validate_actions_config
from homeassistant.helpers.trigger import TriggerConfig

from custom_components import voip_stack
from custom_components.voip_stack import services, trigger
from custom_components.voip_stack.config import normalized_trunk_config
from custom_components.voip_stack.core import rtp, sip
from custom_components.voip_stack.sip_trunk import SipTrunkClient
from custom_components.voip_stack.trunk_runtime import trunk_client_config
from custom_components.voip_stack.runtime_data import sip_trunks

from .test_automation_originate_ha import lab as lab
from .test_trunk_calls_ha import trunks as trunks

pytestmark = pytest.mark.ha


@pytest.mark.timeout(60)
@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_100_did_dictionary_exceptions_reach_independent_phones_and_duplex_rtp(trunks, monkeypatch):
    lab = trunks
    hass, registry = lab.hass, lab.calls
    listener = ("127.0.0.1", lab.runtime.transport_config["sip_port"])
    from custom_components.voip_stack.core.sip_auth import build_digest_authorization
    from custom_components.voip_stack.sip_registrar import SipAccount
    registrar = registry.component("registrar")
    registrar.enabled = True
    registrar.update_accounts([SipAccount(f"bedroom{index}", f"bedroom{index}", "fixture-only") for index in range(2)])
    for index, phone in enumerate(lab.phones):
        replies = asyncio.Queue()
        previous = phone.datagram_received
        def receive(raw, address, previous=previous, replies=replies, phone=phone):
            message = sip.parse_message(raw)
            if message.is_response and message.header("CSeq").endswith(" REGISTER"):
                replies.put_nowait(message)
            elif message.method == "OPTIONS":
                headers = [(key, message.header(key)) for key in ("Via", "From", "To", "Call-ID", "CSeq")]
                phone.transport.sendto(sip.build_response(200, "OK", headers), address)
            else:
                previous(raw, address)
        monkeypatch.setattr(phone, "datagram_received", receive)
        uri = f"sip:{listener[0]}:{listener[1]}"
        contact = f"sip:bedroom{index}@127.0.0.1:{phone.transport.get_extra_info('sockname')[1]}"
        headers = [("Via", f"SIP/2.0/UDP 127.0.0.1:{phone.transport.get_extra_info('sockname')[1]};branch=z9hG4bK-did-register-{index};rport"),
            ("From", f"<sip:bedroom{index}@127.0.0.1>;tag=register-{index}"),
            ("To", f"<sip:bedroom{index}@127.0.0.1>"), ("Call-ID", f"did-register-{index}"),
            ("CSeq", "1 REGISTER"), ("Contact", f"<{contact}>"), ("Expires", "300")]
        phone.transport.sendto(sip.build_request("REGISTER", uri, headers), listener)
        challenge = await asyncio.wait_for(replies.get(), 3)
        assert challenge.status_code == 401
        authorization = build_digest_authorization(challenge_header=challenge.header("WWW-Authenticate"),
            username=f"bedroom{index}", password="fixture-only", method="REGISTER", uri=uri)
        headers = [(key, "2 REGISTER" if key == "CSeq" else value.replace(";rport", "-auth;rport") if key == "Via" else value) for key, value in headers]
        phone.transport.sendto(sip.build_request("REGISTER", uri, [*headers, ("Authorization", authorization)]), listener)
        assert (await asyncio.wait_for(replies.get(), 3)).status_code == 200
        assert lab.runtime.endpoints.get(f"sip:bedroom{index}").is_available
    config = normalized_trunk_config({"trunk_enabled": True, "trunk_register": False,
        "trunk_allowed_ips": ["127.0.0.2/32"], "trunk_server": "127.0.0.2",
        "trunk_inbound_default_target": "bedroom0", "trunk_did_source": "to",
        "automation_routing_enabled": False})
    provider = SipTrunkClient(config=trunk_client_config(config), local_ip="127.0.0.1",
                              local_sip_port=listener[1], trunk_id="did-provider")
    lab.runtime.trunk_configs["did-provider"] = config
    registry.adopt_component("trunk:did-provider", provider, closer=provider.stop)
    await services.async_register_services(hass, {
        "select_inbound_destination": voip_stack._handle_select_inbound_destination_service,
    })
    mapping = {f"0009900{index:03}": f"bedroom{index % 2}" for index in range(100)}
    def select(value):
        return {"action": "voip_stack.select_inbound_destination", "data": {"destination": value}}
    actions = [{"choose": [
        {"conditions": "{{ trigger.call.caller_number == '009911' }}", "sequence": [select("bedroom0")]},
        {"conditions": "{{ trigger.call.called_number == '0009900042' }}", "sequence": [select("bedroom1")]},
    ], "default": [select("{{ did_routes.get(trigger.call.called_number, 'bedroom0') }}")]}]
    script = Script(hass, await async_validate_actions_config(hass, cv.SCRIPT_SCHEMA(actions)),
                    "DID independent target witness", "automation", script_mode="parallel")
    tasks, snapshots = [], []
    def runner(payload, _description, context):
        snapshots.append(payload["call"])
        task = hass.async_create_task(script.async_run({"trigger": payload, "did_routes": mapping}, context))
        tasks.append(task)
        return task
    subject = trigger.VoipCallTrigger(hass, TriggerConfig("voip_stack.route_requested", options={"trunk": "did-provider"}))
    detach = await subject.async_attach_runner(runner)
    source = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    source.bind(("127.0.0.2", 0))
    source.setblocking(False)
    media = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    media.bind(("127.0.0.2", 0))
    media.setblocking(False)
    signal_port = source.getsockname()[1]
    source_uri = f"sip:incoming-generator@127.0.0.2:{signal_port}"
    request_uri = f"sip:registration-account@{listener[0]}:{listener[1]}"
    offer = ("v=0\r\no=- 1 1 IN IP4 127.0.0.2\r\ns=Independent source\r\n"
             "c=IN IP4 127.0.0.2\r\nt=0 0\r\n"
             f"m=audio {media.getsockname()[1]} RTP/AVP 96\r\n"
             "a=rtpmap:96 L16/16000/1\r\na=ptime:20\r\na=sendrecv\r\n").encode()

    async def response_for(call_id, statuses):
        async with asyncio.timeout(4):
            while True:
                response = sip.parse_message(await hass.loop.sock_recv(source, 16384))
                if response.header("Call-ID") == call_id and response.status_code in statuses:
                    return response

    try:
        scenarios = [(did, "001111", "bedroom1" if did == "0009900042" else target, False)
                     for did, target in mapping.items()]
        scenarios += [("0009900001", "009911", "bedroom0", False), ("000000unknown", "001111", "bedroom0", False),
                      ("0009900000", "001111", "bedroom0", True), ("0009900001", "001111", "bedroom1", True)]
        for index, (did, caller, expected, answered) in enumerate(scenarios):
            call_id = f"did-bridge-{index}"
            for phone in lab.phones:
                phone.final_status = 200 if answered else 486
                phone.bye.clear()
            before = [len(phone.invites) for phone in lab.phones]
            branch = f"z9hG4bK-did-bridge-{index}"
            from_header = f'"Independent source" <sip:{caller}@provider.example>;tag=source-{index}'
            headers = [("Via", f"SIP/2.0/UDP 127.0.0.2:{signal_port};branch={branch};rport"),
                ("From", from_header), ("To", f"<sip:{did}@provider.example>"),
                ("Call-ID", call_id), ("CSeq", "1 INVITE"), ("Contact", f"<{source_uri}>"),
                ("Content-Type", "application/sdp")]
            await hass.loop.sock_sendto(source, sip.build_request("INVITE", request_uri, headers, offer), listener)
            final = await response_for(call_id, {200, 403, 404, 480, 486, 488, 500, 503})
            assert final.status_code == (200 if answered else 486)
            target_index = int(expected[-1])
            assert [len(phone.invites) - count for phone, count in zip(lab.phones, before)] == [int(target_index == 0), int(target_index == 1)]
            target_invite = lab.phones[target_index].invites[-1]
            assert sip.parse_sip_uri(target_invite.uri).user == expected
            assert sip.parse_sip_uri(target_invite.header("To")).user == expected
            assert snapshots[-1]["called_number"] == snapshots[-1]["called_number_raw"] == did
            assert snapshots[-1]["caller_number"] == caller
            assert snapshots[-1]["inbound_trunk_id"] == "did-provider"
            # ACK is independently built from the UAS response and initial transaction.
            ack_headers = [("Via", f"SIP/2.0/UDP 127.0.0.2:{signal_port};branch={branch if not answered else branch + '-ack'};rport"),
                           ("From", from_header), ("To", final.header("To")),
                           ("Call-ID", call_id), ("CSeq", "1 ACK")]
            ack_uri = sip.name_addr_uri(final.header("Contact")) if answered else request_uri
            await hass.loop.sock_sendto(source, sip.build_request("ACK", ack_uri, ack_headers), listener)
            if answered:
                session = registry.get_session(call_id)
                assert session is not None and session.metadata["called_number"] == did
                remote_port = int(re.search(rb"m=audio (\d+)", final.body)[1])
                source_payload = struct.pack("!320h", *([1600 + target_index] * 320))
                for sequence in range(3):
                    packet = rtp.build_packet(rtp.RtpPacket(96, sequence, sequence * 320, 4100 + index, source_payload))
                    await hass.loop.sock_sendto(media, packet, ("127.0.0.1", remote_port))
                received = bytearray()
                async with asyncio.timeout(3):
                    while len(received) < len(source_payload) * 3:
                        raw, _ = await lab.phones[target_index].audio.get()
                        received.extend(rtp.parse_packet(raw).payload)
                assert bytes(received) == source_payload * 3
                target_port = int(re.search(rb"m=audio (\d+)", target_invite.body)[1])
                return_payload = struct.pack("!320h", *([-1700 - target_index] * 320))
                await hass.loop.sock_sendto(lab.phones[target_index].rtp_socket,
                    rtp.build_packet(rtp.RtpPacket(lab.phones[target_index].payload_type, 0, 0, 6100 + index, return_payload)),
                    ("127.0.0.1", target_port))
                returned = bytearray()
                async with asyncio.timeout(3):
                    while len(returned) < len(return_payload):
                        returned.extend(rtp.parse_packet(await hass.loop.sock_recv(media, 4096)).payload)
                assert bytes(returned) == return_payload
                bye_headers = [("Via", f"SIP/2.0/UDP 127.0.0.2:{signal_port};branch={branch}-bye;rport"),
                    ("From", from_header), ("To", final.header("To")), ("Call-ID", call_id), ("CSeq", "2 BYE")]
                await hass.loop.sock_sendto(source, sip.build_request("BYE", ack_uri, bye_headers), listener)
                assert (await response_for(call_id, {200})).header("CSeq") == "2 BYE"
                await asyncio.wait_for(lab.phones[target_index].bye.wait(), 3)
            async with asyncio.timeout(4):
                while registry.sessions:
                    await asyncio.sleep(.001)
            assert not lab.runtime.rtp_port_pool.get("used")
        await asyncio.gather(*tasks)
        assert len(snapshots) == 104
        assert all(client.ready for client in sip_trunks(hass).values())
    finally:
        detach()
        source.close()
        media.close()
        for call_id in tuple(registry.sessions):
            await registry.terminate_call_wait(call_id, reason="local_hangup")
        await asyncio.gather(*tasks, return_exceptions=True)
