"""Configured DID selection routes directly without a HA routing automation."""

import asyncio
from dataclasses import replace
import re
import socket
import struct

import pytest

from custom_components.voip_stack.config import normalized_trunk_config
from custom_components.voip_stack.core import rtp, sip
from custom_components.voip_stack.sip_trunk import SipTrunkClient
from custom_components.voip_stack.trunk_runtime import trunk_client_config

from .test_automation_originate_ha import lab as lab
from .test_trunk_calls_ha import trunks as trunks

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_direct_did_policy_and_fallback_reach_real_registered_peers(trunks):
    lab = trunks
    registry = lab.calls
    listener = ("127.0.0.1", lab.runtime.transport_config["sip_port"])
    lab.runtime.endpoints.update("bedroom1", extension="401")
    lab.roster[:] = [replace(row, extension="401") if row.id == "bedroom1" else row for row in lab.roster]
    config = normalized_trunk_config({"trunk_options_interval": 0,
        "trunk_enabled": True, "trunk_register": False,
        "trunk_allowed_ips": ["127.0.0.2/32"], "trunk_server": "127.0.0.2",
        "trunk_did_source": "to", "trunk_inbound_default_target": "bedroom0",
        "trunk_did_prefix_rules": [{"prefix": "055051", "replacement": ""}],
        "automation_routing_enabled": False,
    })
    provider = SipTrunkClient(config=trunk_client_config(config), local_ip="127.0.0.1",
                              local_sip_port=listener[1], trunk_id="direct-did")
    lab.runtime.trunk_configs["direct-did"] = config
    registry.adopt_component("trunk:direct-did", provider, closer=provider.stop)
    signal = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    media = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for sock in (signal, media):
        sock.bind(("127.0.0.2", 0))
        sock.setblocking(False)
    source_uri = f"sip:outside@127.0.0.2:{signal.getsockname()[1]}"
    offer = ("v=0\r\no=- 1 1 IN IP4 127.0.0.2\r\ns=Direct DID\r\n"
             "c=IN IP4 127.0.0.2\r\nt=0 0\r\n"
             f"m=audio {media.getsockname()[1]} RTP/AVP 96\r\n"
             "a=rtpmap:96 L16/16000/1\r\na=ptime:20\r\na=sendrecv\r\n").encode()
    # An empty/invalid selected source must reach the configured fallback,
    # even when the unchanged Request-URI itself names a real phone.
    scenarios = [
        ("to", [("To", "<sip:055051401@provider.test>")], [], "registration", "bedroom1", "401", False),
        ("p_called_party_id", [("To", "<sip:registration@provider.test>"),
                              ("P-Called-Party-ID", "<sip:055051401@provider.test>")], [], "registration", "bedroom1", "401", False),
        ("p_called_party_id", [("To", "<sip:055051401@provider.test>")], ["to"], "registration", "bedroom1", "401", False),
        ("p_called_party_id", [("To", "<sip:055051401@provider.test>")], [], "bedroom1", "bedroom0", "", False),
        ("p_called_party_id", [("To", "<sip:055051401@provider.test>"),
                              ("P-Called-Party-ID", "not-a-uri")], [], "bedroom1", "bedroom0", "", False),
        ("p_called_party_id", [("To", "<sip:055051401@provider.test>"),
                              ("P-Called-Party-ID", "<sip:055051401@provider.test>"),
                              ("P-Called-Party-ID", "<sip:055051401@other.test>")], [], "bedroom1", "bedroom0", "", False),
        ("to", [("To", "<sip:055051999@provider.test>")], [], "registration", "bedroom0", "999", False),
        ("request_uri", [("To", "<sip:ignored@provider.test>")], [], "401", "bedroom1", "401", False),
        ("to", [("To", "<sip:055051401@provider.test>")], [], "registration", "bedroom1", "401", True),
    ]

    async def final_for(call_id, statuses):
        async with asyncio.timeout(3):
            while True:
                message = sip.parse_message(await lab.hass.loop.sock_recv(signal, 16384))
                if message.header("Call-ID") == call_id and message.status_code in statuses:
                    return message

    try:
        for index, (source, called_headers, fallbacks, request_user, expected, normalized, answered) in enumerate(scenarios):
            config.update(trunk_did_source=source, trunk_did_fallbacks=fallbacks)
            call_id = f"direct-did-{index}"
            for phone in lab.phones:
                phone.final_status = 200 if answered else 486
                phone.bye.clear()
            before = [len(phone.invites) for phone in lab.phones]
            from_header = f'"External caller" <sip:+3900123@provider.test>;tag=caller-{index}'
            branch = f"z9hG4bK-{call_id}"
            request_uri = f"sip:{request_user}@{listener[0]}:{listener[1]}"
            headers = [("Via", f"SIP/2.0/UDP 127.0.0.2:{signal.getsockname()[1]};branch={branch};rport"),
                       ("From", from_header), *called_headers, ("Call-ID", call_id),
                       ("CSeq", "1 INVITE"), ("Contact", f"<{source_uri}>"),
                       ("Content-Type", "application/sdp")]
            await lab.hass.loop.sock_sendto(signal, sip.build_request("INVITE", request_uri, headers, offer), listener)
            final = await final_for(call_id, {200, 403, 404, 480, 486, 488, 500, 503})
            assert final.status_code == (200 if answered else 486)
            target = int(expected[-1])
            assert [len(phone.invites) - count for phone, count in zip(lab.phones, before)] == [int(target == 0), int(target == 1)]
            assert sip.parse_sip_uri(lab.phones[target].invites[-1].uri).user == expected
            ack_uri = sip.name_addr_uri(final.header("Contact")) if answered else request_uri
            dialog_headers = [("From", from_header), ("To", final.header("To")), ("Call-ID", call_id)]
            await lab.hass.loop.sock_sendto(signal, sip.build_request("ACK", ack_uri, [
                ("Via", f"SIP/2.0/UDP 127.0.0.2:{signal.getsockname()[1]};branch={branch}-ack;rport"),
                *dialog_headers, ("CSeq", "1 ACK")]), listener)
            if answered:
                assert registry.get_session(call_id).metadata["called_number"] == normalized
                target_port = int(re.search(rb"m=audio (\d+)", final.body)[1])
                payload = struct.pack("!320h", *([2500] * 320))
                for sequence in range(3):
                    await lab.hass.loop.sock_sendto(media, rtp.build_packet(
                        rtp.RtpPacket(96, sequence, sequence * 320, 2001, payload)), (listener[0], target_port))
                received = bytearray()
                async with asyncio.timeout(2):
                    while len(received) < len(payload) * 3:
                        raw, _ = await lab.phones[target].audio.get()
                        received.extend(rtp.parse_packet(raw).payload)
                assert bytes(received) == payload * 3
                await lab.hass.loop.sock_sendto(signal, sip.build_request("BYE", ack_uri, [
                    ("Via", f"SIP/2.0/UDP 127.0.0.2:{signal.getsockname()[1]};branch={branch}-bye;rport"),
                    *dialog_headers, ("CSeq", "2 BYE")]), listener)
                assert (await final_for(call_id, {200})).header("CSeq") == "2 BYE"
                await asyncio.wait_for(lab.phones[target].bye.wait(), 2)
            async with asyncio.timeout(3):
                while registry.sessions:
                    await asyncio.sleep(.001)
            assert not lab.runtime.rtp_port_pool.get("used")
    finally:
        signal.close()
        media.close()
        for call_id in tuple(registry.sessions):
            await registry.terminate_call_wait(call_id, reason="local_hangup")
