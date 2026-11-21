"""CC-7 crosses real SIP sockets and native HA call service validation."""

import asyncio
import json
import socket

import pytest
from homeassistant.core import Context

from custom_components.voip_stack.core import sdp, sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.sip_listener import SipInviteResult, SipUdpEndpoint

from .test_automation_originate_ha import assert_call_resources_released, dial, lab as lab

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("scenario,attributes,payloads,expected,send_ms", [
    ("optional-and-invalid-alternative", ["a=rtpmap:8 PCMA/8000", "a=rtpmap:96 L16/bad",
        "a=ptime:20", "a=ptime:nan", "a=minptime:bad", "a=maxptime:40", "a=maxptime:inf"], "96 8", 200, 20),
    ("strict-feasible-maximum", ["a=rtpmap:8 PCMA/8000", "a=maxptime:5", "a=maxptime:40"], "8", 200, 5),
    ("strict-impossible-maximum", ["a=rtpmap:8 PCMA/8000", "a=maxptime:0.01", "a=maxptime:40"], "8", 488, None),
    ("no-valid-codec", ["a=rtpmap:8 PCMA/bad"], "8", 488, None),
])
async def test_native_listener_returns_200_or_488_per_remaining_contract(
    hass, socket_enabled, tmp_path, scenario, attributes, payloads, expected, send_ms,
):
    accepted = []

    async def answer(invite):
        accepted.append(invite)
        body = sdp.build_answer_directional("127.0.0.1", "127.0.0.1", 44000,
            invite.send_format, invite.recv_format, remote_sdp=invite.remote_sdp)
        return SipInviteResult(200, "OK", answer_sdp=body)

    owner = SipUdpEndpoint(local_ip="127.0.0.1", local_rtp_port=44000,
        supported_formats=[AudioFormat(8000, frame_ms=20)], on_invite=answer)
    server, _ = await hass.loop.create_datagram_endpoint(lambda: owner, local_addr=("127.0.0.1", 0))
    source = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    source.bind(("127.0.0.1", 0))
    source.setblocking(False)
    address = server.get_extra_info("sockname")
    peer_port = source.getsockname()[1]
    uri = f"sip:ha@127.0.0.1:{address[1]}"
    call_id = f"cc7-{scenario}"
    offer = "\r\n".join(["v=0", "o=- 1 1 IN IP4 127.0.0.1", "s=CC7 wire",
        "c=IN IP4 127.0.0.1", "t=0 0", f"m=audio 35000 RTP/AVP {payloads}",
        *attributes, "a=sendrecv", ""])
    headers = [("Via", f"SIP/2.0/UDP 127.0.0.1:{peer_port};branch=z9hG4bK-{call_id};rport"),
        ("From", "<sip:caller@127.0.0.1>;tag=caller"), ("To", "<sip:ha@127.0.0.1>"),
        ("Call-ID", call_id), ("CSeq", "1 INVITE"),
        ("Contact", f"<sip:caller@127.0.0.1:{peer_port}>"), ("Content-Type", "application/sdp")]

    async def final_response():
        async with asyncio.timeout(3):
            while True:
                raw, _ = await hass.loop.sock_recvfrom(source, 8192)
                message = sip.parse_message(raw)
                if message.status_code >= 200:
                    return message

    try:
        await hass.loop.sock_sendto(source, sip.build_request("INVITE", uri, headers, offer.encode()), address)
        response = await final_response()
        receipt = {"scenario": scenario, "call_id": call_id, "offer": offer,
            "status": response.status_code, "answer": response.body.decode(),
            "accepted_callbacks": len(accepted),
            "send_ms": accepted[0].send_format.frame_ms if accepted else None}
        (tmp_path / "sip-result.json").write_text(json.dumps(receipt, indent=2) + "\n")
        assert response.status_code == expected, receipt
        assert len(accepted) == (1 if expected == 200 else 0)
        dialog_headers = [(key, response.header(key)) for key in ("Via", "From", "To", "Call-ID")]
        await hass.loop.sock_sendto(source, sip.build_request("ACK", uri,
            [*dialog_headers, ("CSeq", "1 ACK")]), address)
        if expected == 200:
            assert accepted[0].send_format.encoding == "PCMA"
            assert accepted[0].send_format.frame_ms == send_ms
            assert accepted[0].send_format.max_frame_ms == (5 if send_ms == 5 else 40)
            await hass.loop.sock_sendto(source, sip.build_request("BYE", uri,
                [*dialog_headers, ("CSeq", "2 BYE")]), address)
            assert (await final_response()).header("CSeq") == "2 BYE"
        async with asyncio.timeout(3):
            while owner.pending_invites or owner._invite_tasks:
                await asyncio.sleep(.001)
        assert not owner.active_dialogs
        # The bounded 481 cache after BYE rejects a late original INVITE; it
        # owns neither a dialog nor a retransmission task.
        assert all(item.status == 481 and item.final_task is None
            for item in owner.completed_invites.values())
        receipt["cleanup"] = {"pending": len(owner.pending_invites),
            "dialogs": len(owner.active_dialogs), "invite_tasks": len(owner._invite_tasks),
            "retired_transaction_cache": [item.status for item in owner.completed_invites.values()]}
        (tmp_path / "sip-result.json").write_text(json.dumps(receipt, indent=2) + "\n")
    finally:
        source.close()
        server.close()
        await owner.wait_closed()


async def test_native_call_service_accepts_usable_answer_despite_invalid_optional_fields(lab, monkeypatch, tmp_path):
    phone = lab.phones[0]
    original_respond = phone.respond
    answered = []

    def respond(request, address, status, body=b""):
        if body:
            # Alter the independently generated peer answer, retaining its
            # negotiated L16 mapping and actual socket transport coordinates.
            body = body.replace(b" RTP/AVP ", b" RTP/AVP 111 ", 1)
            body += b"a=rtpmap:111 L16/bad\r\na=ptime:nan\r\na=maxptime:40\r\na=maxptime:inf\r\n"
            answered.append(body.decode())
        original_respond(request, address, status, body)

    monkeypatch.setattr(phone, "respond", respond)
    context = Context()
    result = await dial(lab, context)
    (tmp_path / "service-result.json").write_text(json.dumps({"service": "voip_stack.call",
        "service_data": {"source_automation": "Wakeup-Caller", "destination": "bedroom0", "answer_timeout": 1},
        "result": result, "peer_answer": answered, "wire_invites": len(phone.invites)}, indent=2) + "\n")
    assert result["call_id"] == phone.invites[0].header("Call-ID")
    await lab.hass.services.async_call("voip_stack", "hangup", {}, blocking=True, context=context)
    await asyncio.wait_for(phone.bye.wait(), 3)
    assert_call_resources_released(lab)
