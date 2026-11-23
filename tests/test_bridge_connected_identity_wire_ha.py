"""Connected identity crosses the PBX without replacing either SIP/RTP leg."""

import asyncio
import struct

import pytest

from custom_components.voip_stack.core import rtp, sip
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
from custom_components.voip_stack.roster import RosterEntry
from .test_queue_call_ha import queue_lab as queue_lab, wait_until
from .test_trunk_calls_ha import lab as lab, trunks as trunks

pytestmark = pytest.mark.ha


def _identity_phone(monkeypatch, phone, name, *, rejected_update_status=0):
    """The independent phone supplies its name and accepts standard UPDATE."""
    receive = phone.datagram_received

    def respond(request, address, status, body=b""):
        to = request.header("To")
        if request.method == "INVITE" and status == 200:
            # RFC 3261 keeps the To URI; only the display name is supplied here.
            to = sip.format_name_addr(sip.name_addr_uri(to), name)
        if not sip.extract_tag(to):
            to += ";tag=bedroom"
        headers = [(key, request.header(key)) for key in ("Via", "From", "Call-ID", "CSeq")]
        headers += [("To", to), ("Supported", "from-change"),
                    ("Allow", "INVITE, ACK, CANCEL, BYE, UPDATE, OPTIONS")]
        if body:
            port = phone.transport.get_extra_info("sockname")[1]
            headers += [("Content-Type", "application/sdp"),
                        ("Contact", f"<sip:bedroom@127.0.0.1:{port}>")]
        reasons = {180: "Ringing", 200: "OK", 405: "Method Not Allowed",
                   486: "Busy Here", 487: "Request Terminated", 500: "Server Internal Error"}
        phone.transport.sendto(sip.build_response(status, reasons[status], headers, body), address)

    def dispatch(raw, address):
        message = sip.parse_message(raw)
        if message.method in {"UPDATE", "REFER"}:
            phone.requests.append(message)
            status = 200 if message.method == "UPDATE" else 405
            if (message.method == "UPDATE" and rejected_update_status
                    and sip.parse_sip_uri(message.header("From")).user == "206"):
                status = rejected_update_status
            respond(message, address, status)
        else:
            receive(raw, address)

    monkeypatch.setattr(phone, "respond", respond)
    monkeypatch.setattr(phone, "datagram_received", dispatch)


def _dialog_identity(request):
    return (request.header("Call-ID"), sip.extract_tag(request.header("From")),
            sip.extract_tag(request.header("To")))


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("source_role", ["UAS", "retained-UAC"])
@pytest.mark.parametrize("reject_update", [False, True], ids=["accepted", "rejected"])
async def test_callee_updates_reach_original_sip_source_with_same_dialog_and_duplex_rtp(
    queue_lab, monkeypatch, source_role, reject_update,
):
    runtime = queue_lab
    target, source_phone = runtime.phones[1], runtime.phones[0]
    _identity_phone(monkeypatch, target, "Connected reception")
    _identity_phone(monkeypatch, source_phone, "Original source",
                    rejected_update_status=405 if reject_update else 0)
    source_requests = []
    caller = lifetime = None
    card = None
    session = None
    baseline_ports = set(runtime.runtime.rtp_port_pool.get("used", ()))
    try:
        if source_role == "UAS":
            caller, source_audio = await runtime.caller()
            receive = caller.protocol.datagram_received

            def observe_source(raw, address):
                message = sip.parse_message(raw)
                if message.method in {"UPDATE", "BYE"}:
                    source_requests.append(message)
                if (reject_update and message.method == "UPDATE"
                        and sip.parse_sip_uri(message.header("From")).user == "206"):
                    # An independent peer rejects this request. The production
                    # receiver never accepts or commits its remote identity.
                    caller.transport.sendto(sip.build_uas_response(
                        message, 500, "Server Internal Error"), address)
                    return
                receive(raw, address)

            monkeypatch.setattr(caller.protocol, "datagram_received", observe_source)
            state = await caller.invite(target="bedroom1", remote_host="127.0.0.1",
                remote_sip_port=runtime.runtime.transport_config["sip_port"], timeout=3)
            if state == "ringing":
                state = await caller.wait_for_final(timeout=3)
            assert state == "in_call"
            call_id = caller.dialog_ids.call_id
            source_key = (call_id, caller.dialog_ids.remote_tag, caller.dialog_ids.local_tag)
            source_target = runtime.calls.component("udp_listener").remote_target_for_call(call_id)
            lifetime = asyncio.create_task(caller.wait_for_dialog_termination(), name="identity-source-dialog")
        else:
            card = PhoneEndpoint(endpoint_id="identity-source-card", username="identity-source-card",
                name="Source card", device_id="identity-source-device", kind=EndpointKind.BROWSER,
                availability="available", capabilities=frozenset({"audio", "dtmf"}))
            runtime.runtime.endpoints.register(card)
            runtime.roster.append(RosterEntry(card.username, name=card.name,
                metadata={"local_ha": True, "endpoint_id": card.endpoint_id}))
            runtime.runtime.directory.replace(runtime.roster)
            await runtime.hass.services.async_call("voip_stack", "call", {
                "device_id": card.device_id, "destination": "bedroom0",
                "media_client_id": "identity-source-owner"}, blocking=True)
            call_id = runtime.runtime.endpoints.require(card.endpoint_id).active_call_id
            await wait_until(lambda: runtime.calls.get_session(call_id).state == "in_call")
            source_client = runtime.calls.sip_client_for(call_id)
            source_dialog = source_client.dialog
            retained_port = source_dialog.local_rtp_port
            source_target = source_dialog.remote_target_uri
            source_key = (call_id, source_client.dialog_ids.local_tag, source_client.dialog_ids.remote_tag)
            source_audio = source_phone.rtp_socket
            source_requests = source_phone.requests
            response = await runtime.hass.services.async_call("voip_stack", "transfer", {
                "device_id": card.device_id, "call_id": call_id, "destination": "bedroom1"},
                blocking=True, return_response=True)
            assert response["success"], response
            assert runtime.calls.source_sip_client_for(call_id) is source_client
            assert source_client.dialog.call_id == source_dialog.call_id
            assert len(source_phone.invites) == 1

        session = runtime.calls.get_session(call_id)
        await wait_until(lambda: runtime.calls.resource_for(call_id, "relay") is not None)
        relay = runtime.calls.resource_for(call_id, "relay")
        generation = session.generation
        source_port, target_port = relay.left_port, relay.right_port
        if source_role == "retained-UAC":
            assert source_port == retained_port
        source_fd = relay.left_transport.get_extra_info("socket").fileno()
        target_fd = relay.right_transport.get_extra_info("socket").fileno()
        destination_id = runtime.calls.bridge_link_for(call_id)
        destination = runtime.calls.sip_client_for(destination_id)
        target_dialog = destination.dialog
        target_media = (target_dialog.send_format, target_dialog.recv_format,
                        target_dialog.remote_rtp_host, target_dialog.remote_rtp_port,
                        target_dialog.local_rtp_port, target_dialog.remote_target_uri,
                        target_dialog.route_set)
        wire_invite = target.invites[-1]
        assert sip.name_addr_uri(wire_invite.header("To")) == target_dialog.remote_uri
        await wait_until(lambda: any(message.method == "UPDATE"
            and sip.name_addr_display_name(message.header("From")) == "Connected reception"
            for message in source_requests))

        async def receive_audio(queue, sock, expected, payload_type):
            async with asyncio.timeout(3):
                while True:
                    raw = (await queue.get())[0] if queue is not None else await runtime.hass.loop.sock_recv(sock, 4096)
                    packet = rtp.parse_packet(raw)
                    if packet.payload == expected:
                        assert packet.payload_type == payload_type
                        return packet

        async def duplex(round_index):
            left, right = relay.left.inbound_rtp_format, relay.right.inbound_rtp_format
            assert (left.encoding, left.sample_rate, left.channels, left.frame_ms) == ("L16", 16000, 1, 20)
            assert (right.encoding, right.sample_rate, right.channels, right.frame_ms) == ("L16", 16000, 1, 20)
            result = []
            for index, (sock, port, incoming, outgoing, queue, sink, value) in enumerate((
                (source_audio, source_port, left, relay.right.outbound_rtp_format,
                 target.audio, None, 2310 + round_index * 100),
                (target.rtp_socket, target_port, right, relay.left.outbound_rtp_format,
                 source_phone.audio if caller is None else None, source_audio, -1230 - round_index * 100),
            )):
                payload = struct.pack(f"!{incoming.audio_format.nominal_frame_samples}h",
                    *([value] * incoming.audio_format.nominal_frame_samples))
                for offset in range(2):
                    sequence = 10 + round_index * 2 + offset
                    await runtime.hass.loop.sock_sendto(sock, rtp.build_packet(rtp.RtpPacket(
                        incoming.payload_type, sequence, (sequence - 10) * incoming.rtp_timestamp_step,
                        0x552200 + index, payload)), ("127.0.0.1", port))
                result.append(await receive_audio(queue, sink, payload, outgoing.payload_type))
            return result

        before = await duplex(0)
        for sequence, (name, user) in enumerate((("Remote operator", "205"), ("", "206")), start=2):
            peer_port = target.transport.get_extra_info("sockname")[1]
            headers = [("Via", f"SIP/2.0/UDP 127.0.0.1:{peer_port};branch=z9hG4bK-bridge-identity-{sequence};rport"),
                ("From", sip.format_name_addr(f"sip:{user}@127.0.0.1", name) + ";tag=bedroom"),
                ("To", wire_invite.header("From")), ("Call-ID", destination_id),
                ("CSeq", f"{sequence} UPDATE"), ("Supported", "from-change")]
            target.transport.sendto(sip.build_request("UPDATE",
                sip.name_addr_uri(wire_invite.header("Contact")), headers), target.remote)
            await wait_until(lambda: any(message.method == "UPDATE"
                and sip.name_addr_identity(message.header("From")) == (name or user)
                and sip.parse_sip_uri(message.header("From")).user == user for message in source_requests))
            update = next(message for message in reversed(source_requests) if message.method == "UPDATE"
                and sip.parse_sip_uri(message.header("From")).user == user)
            assert _dialog_identity(update) == source_key
            assert sip.name_addr_display_name(update.header("From")) == name
            assert update.uri == source_target and not update.body
            assert runtime.calls.get_session(call_id) is session and session.generation == generation
            assert runtime.calls.resource_for(call_id, "relay") is relay
            assert destination.dialog.call_id == target_dialog.call_id
            assert (destination.dialog.send_format, destination.dialog.recv_format,
                    destination.dialog.remote_rtp_host, destination.dialog.remote_rtp_port,
                    destination.dialog.local_rtp_port, destination.dialog.remote_target_uri,
                    destination.dialog.route_set) == target_media
            assert (relay.left_port, relay.right_port) == (source_port, target_port)
            assert (relay.left_transport.get_extra_info("socket").fileno(),
                    relay.right_transport.get_extra_info("socket").fileno()) == (source_fd, target_fd)

        await wait_until(lambda: f"voip-bridge-identity-{destination_id}" not in session.named_tasks)
        if reject_update and caller is not None:
            assert sip.parse_sip_uri(caller.dialog.remote_uri).user == "205"
        after = await duplex(1)
        for previous, current in zip(before, after, strict=True):
            assert current.ssrc == previous.ssrc
            assert 0 < (current.sequence - previous.sequence) % 65536 < 32768
            assert 0 < (current.timestamp - previous.timestamp) % 2**32 < 2**31
        assert len(target.invites) == 1
        assert not any(message.method == "REFER" for message in source_requests)
        await runtime.calls.request_termination(call_id, TerminationIntent("test_complete"))
        await wait_until(lambda: not runtime.calls.sessions)
        bye = next(message for message in reversed(source_requests) if message.method == "BYE")
        assert _dialog_identity(bye) == source_key
        assert sip.parse_sip_uri(bye.header("From")).user == "206"
        assert sip.name_addr_display_name(bye.header("From")) == ""
        assert set(runtime.runtime.rtp_port_pool.get("used", ())) == baseline_ports
    finally:
        for active in tuple(runtime.calls.sessions.values()):
            if active.live:
                await runtime.calls.request_termination(active.call_id, TerminationIntent("test_cleanup"))
        if lifetime is not None:
            await asyncio.wait_for(lifetime, 3)
