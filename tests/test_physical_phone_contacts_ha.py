"""A programmed directory reference reaches the normal SIP/trunk permission path."""

import asyncio
import re
import socket
import struct

import pytest

from .test_trunk_calls_ha import lab as lab, trunks as trunks

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("allowed", [True, False])
async def test_programmed_contact_alias_uses_current_route_permissions_and_duplex_media(trunks, allowed):
    from custom_components.voip_stack.core import rtp
    from custom_components.voip_stack.core.audio_format import AudioFormat
    from custom_components.voip_stack.phone_directory import PhoneDirectory
    from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
    from custom_components.voip_stack.phone_policy import PhonePolicy
    from custom_components.voip_stack.phone_profile import resolve_phone_keys
    from custom_components.voip_stack.roster import RosterEntry
    from custom_components.voip_stack.sip_client import SipCallClient
    from custom_components.voip_stack.sip_udp_io import SipDatagramQueueProtocol

    state = trunks
    state.runtime.endpoints.register(PhoneEndpoint(endpoint_id="source", name="Source",
        username="source", kind=EndpointKind.SIP_ACCOUNT, availability="available",
        policy=PhonePolicy(outbound_trunk_id="work")))
    state.roster.append(RosterEntry("source", address="127.0.0.2", metadata={"endpoint_id": "source"}))
    entry = RosterEntry(id="customer", name="Customer", number="003905512345",
        metadata={"endpoint_id": "contact:customer", "endpoint_kind": "contact"})
    state.roster.append(entry)
    state.runtime.directory = PhoneDirectory(state.roster)
    profile = {"capabilities": {"key_slots": [{"slot": "1", "functions": ["speed_dial"],
        "max_value_bytes": 31, "max_label_bytes": 31}]}}
    phone = {"lines": [{"slot": "1", "endpoint_id": "source"}], "keys": [{"slot": "1",
        "line": "1", "function": "speed_dial", "target": "contact:customer", "label": ""}]}
    key, = resolve_phone_keys(state.runtime, phone, profile)
    if not allowed:
        state.runtime.transport_config["external_call_blocked_endpoints"] = ["source"]
    audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    audio.bind(("127.0.0.2", 0))
    audio.setblocking(False)
    client = SipCallClient(local_ip="127.0.0.2", local_name="Source", local_uri_user="source",
        local_sip_port=5060, local_rtp_port=audio.getsockname()[1],
        supported_formats=[AudioFormat(sample_rate=16000, frame_ms=20)])
    client.transport, client.protocol = await state.hass.loop.create_datagram_endpoint(
        lambda: SipDatagramQueueProtocol(client.queue), local_addr=("127.0.0.2", 0))
    client.local_sip_port = client.transport.get_extra_info("sockname")[1]
    session = None
    try:
        result = await client.invite(target=key["dial_target"], remote_host="127.0.0.1",
            remote_sip_port=state.runtime.transport_config["sip_port"], timeout=3)
        if result == "ringing":
            result = await client.wait_for_final(timeout=3)
        if not allowed:
            assert result != "in_call" and client.last_sip_status_code == 403
            assert not any(peer.invites for peer in state.phones)
            return
        assert result == "in_call"
        assert not state.phones[0].invites and len(state.phones[1].invites) == 1
        assert "003905512345" in state.phones[1].invites[0].uri
        session = state.calls.get_session(client.dialog_ids.call_id)
        assert session.metadata["source_endpoint_id"] == "source"
        assert session.metadata["outbound_trunk_id"] == "work"
        dialog = client.dialog
        async with asyncio.timeout(3):
            for index in range(3):
                packet = rtp.build_packet(rtp.RtpPacket(dialog.send_format.payload_type,
                    index, index * 320, 901, struct.pack("!320h", *([2100] * 320))))
                await state.hass.loop.sock_sendto(audio, packet, (dialog.remote_rtp_host, dialog.remote_rtp_port))
            received, _ = await state.phones[1].audio.get()
            assert set(struct.unpack("!320h", rtp.parse_packet(received).payload)) == {2100}
            port = int(re.search(rb"m=audio (\d+)", state.phones[1].invites[0].body)[1])
            packet = rtp.build_packet(rtp.RtpPacket(state.phones[1].payload_type, 0, 0, 902,
                struct.pack("!320h", *([3100] * 320))))
            await state.hass.loop.sock_sendto(state.phones[1].rtp_socket, packet, ("127.0.0.1", port))
            received = rtp.parse_packet(await state.hass.loop.sock_recv(audio, 4096))
            assert set(struct.unpack("!320h", received.payload)) == {3100}
    finally:
        if session is not None and session.live:
            await client.terminate()
            await asyncio.wait_for(session.terminated.wait(), 3)
        await client.close()
        audio.close()
        assert not state.calls.sessions and not state.calls.endpoint_claims_snapshot()
        assert not state.calls.relays_snapshot() and not state.runtime.rtp_port_pool.get("used")
