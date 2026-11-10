"""The real bridge preserves a source codec while honoring receive maxptime."""

import asyncio
from dataclasses import replace
import re
import socket
import struct

import pytest

from custom_components.voip_stack.core import rtp, sdp, sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.inbound_routing import bridge
from custom_components.voip_stack.sip_client import SipCallClient
from custom_components.voip_stack.sip_udp_io import SipDatagramQueueProtocol
from tests.test_sip_capabilities_ha import wait_until
from tests.test_trunk_calls_ha import lab as lab, trunks as trunks


pytestmark = pytest.mark.ha


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize(("source_rate", "destination_rate", "offer_tx10"), [
    (16000, 48000, False), (48000, 48000, False), (16000, 16000, False),
    (16000, 48000, True),
], ids=["peer16k20", "browser48k10", "independent16k20-rx-16k10-tx", "peer16k20-or10-prefers10"])
async def test_bridge_repacketizes_source_return_without_changing_destination(
    trunks, monkeypatch, source_rate, destination_rate, offer_tx10,
):
    lab = trunks
    target = lab.phones[0]
    lab.roster[1] = replace(lab.roster[1], metadata={
        "endpoint_id": "sip:bedroom0", "endpoint_kind": "sip_account", "registered": True,
        "sip_capabilities_known": True,
        "sip_audio_tx_formats": ["L16/16000/1/20", "L16/16000/1/10"],
        "sip_audio_rx_formats": ["L16/48000/1/10", "L16/16000/1/20", "L16/16000/1/10"],
        "sdp_features": ["directional_audio_v1", "independent_audio_ptime_v1"],
    })
    lab.runtime.endpoints.update("sip:bedroom0", availability="available")
    observed = {}
    real_select = bridge.bridge_source_audio

    def select(invite, destination):
        selected = real_select(invite, destination)
        observed["source"] = selected
        observed["original_source"] = invite
        observed["destination"] = destination
        return selected

    monkeypatch.setattr(bridge, "bridge_source_audio", select)

    def answer():
        request = target.last_request
        offered = [fmt for fmt in sdp.offered_pcm_formats(request.body) if fmt.encoding == "L16"]
        receive = next(fmt for fmt in offered if fmt.sample_rate == destination_rate)
        send = next(fmt for fmt in offered if fmt.sample_rate == 16000)
        payloads = list(dict.fromkeys([receive.payload_type, send.payload_type]))
        mappings = f"a=rtpmap:{receive.payload_type} L16/{destination_rate}/1\r\n"
        flows = f"a=x-voip-stack-flow:{receive.payload_type} sendrecv\r\n"
        if receive.payload_type != send.payload_type:
            mappings += f"a=rtpmap:{send.payload_type} L16/16000/1\r\n"
            flows = (f"a=x-voip-stack-flow:{receive.payload_type} recv\r\n"
                     f"a=x-voip-stack-flow:{send.payload_type} send\r\n")
        # Preserve either accepted destination geometry, independently of RX time.
        body = (
            "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Fixed microphone peer\r\n"
            "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
            f"m=audio {target.rtp_socket.getsockname()[1]} RTP/AVP {' '.join(map(str, payloads))}\r\n"
            f"{mappings}{flows}"
            "a=x-voip-stack-independent-ptime:1\r\n"
            "a=ptime:10\r\na=maxptime:10\r\na=sendrecv\r\n"
        ).encode()
        observed.update(target_answer=body, target_tx=send)
        target.respond(request, target.remote, 200, body)
        target.answered.set()

    monkeypatch.setattr(target, "answer", answer)
    audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    audio.bind(("127.0.0.2", 0))
    audio.setblocking(False)
    # Fixed20 cases exercise the 20->10 bridge regardless of HA preference.
    # The alternative case keeps20 first in the catalog but permits HA's10ms RX.
    offered_ms = 20 if source_rate == 16000 else 10
    source_ms = 10 if offer_tx10 else offered_ms
    caller = SipCallClient(
        local_ip="127.0.0.2", local_name="Independent source", local_sip_port=0,
        local_rtp_port=audio.getsockname()[1],
        supported_send_formats=[AudioFormat(source_rate, frame_ms=offered_ms)]
            + ([AudioFormat(16000, frame_ms=10)] if offer_tx10 else []),
        supported_recv_formats=[AudioFormat(48000, frame_ms=10),
                                AudioFormat(16000, frame_ms=20), AudioFormat(16000, frame_ms=10)],
        allow_directional_audio_payloads=True, independent_audio_ptime=True,
    )
    class SourceProtocol(SipDatagramQueueProtocol):
        def datagram_received(self, data, addr):
            message = sip.parse_message(data)
            if message.status_code == 200 and message.header("CSeq").endswith(" INVITE"):
                observed["source_answer"] = message.body
            super().datagram_received(data, addr)

    caller.transport, caller.protocol = await lab.hass.loop.create_datagram_endpoint(
        lambda: SourceProtocol(caller.queue), local_addr=("127.0.0.2", 0),
    )
    caller.local_sip_port = caller.transport.get_extra_info("sockname")[1]
    original_build = caller._build_pending_invite

    def bounded_offer():
        body = re.sub(rb"a=maxptime:[^\r\n]+\r\n", b"", caller._pending_invite_body)
        body = body.replace(b"a=ptime:10\r\n", b"a=ptime:10\r\na=maxptime:10\r\n")
        # This offer constrains the source's receiver to10ms. It does not
        # prescribe its transmitter; the caller's local encoder supports20ms.
        assert b"a=ptime:10\r\n" in body and b"a=maxptime:10\r\n" in body
        caller._pending_invite_body = body
        caller._local_sdp_body = body.decode()
        return original_build()

    monkeypatch.setattr(caller, "_build_pending_invite", bounded_offer)
    try:
        result = await caller.invite(
            target="bedroom0", remote_host="127.0.0.1",
            remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3,
        )
        if result == "ringing":
            result = await caller.wait_for_final(timeout=3)
        assert result == "in_call"
        if destination_rate == 16000:
            # ptime is HA's receive preference, while maxptime describes
            # its actual budget. Actual 20 ms packets remain valid even when
            # the HA caller's nominal transmit interval follows the answer.
            answer_format = sdp.parse_sdp(observed["source_answer"])
            assert answer_format["ptime"] == 10
            assert answer_format["maxptime"] >= source_ms == 20
            assert caller.dialog.send_format.frame_ms == answer_format["ptime"]
            assert caller.dialog.recv_format.frame_ms == 10
            assert observed["source"].recv_format.frame_ms == 10
            assert observed["source"].recv_format.max_frame_ms >= 20
            # The same shared bridge preserves the former20ms receive
            # preference too. Only the listener's preference changed.
            previous_source = replace(observed["original_source"], recv_format=replace(
                observed["original_source"].recv_format, frame_ms=20))
            previous = real_select(previous_source, observed["destination"])
            assert previous.send_format.frame_ms == 10
            assert previous.recv_format.frame_ms == 20
            previous_answer = sdp.build_answer_directional(
                "127.0.0.1", "127.0.0.1", 40000, previous.send_format,
                previous.recv_format, remote_sdp=previous.remote_sdp)
            assert sdp.parse_sdp(previous_answer)["ptime"] == 20
            assert sdp.parse_sdp(previous_answer)["maxptime"] >= 20
        session = lab.calls.get_session(caller.dialog_ids.call_id)
        relay = lab.calls.resource_for(session.call_id, "relay")
        samples = source_rate * source_ms // 1000
        forward = struct.pack(f"!{samples}h", *(index - 300 for index in range(samples)))
        await lab.hass.loop.sock_sendto(audio, rtp.build_packet(rtp.RtpPacket(
            caller.dialog.send_format.payload_type, 0, 0, 901, forward,
        )), (caller.dialog.remote_rtp_host, caller.dialog.remote_rtp_port))
        async with asyncio.timeout(2):
            forwarded = [rtp.parse_packet((await target.audio.get())[0]) for _ in range(source_ms // 10)]
        backwards = struct.pack("!320h", *(index - 100 for index in range(320)))
        target_port = int(re.search(rb"m=audio (\d+)", target.last_request.body)[1])
        await lab.hass.loop.sock_sendto(target.rtp_socket, rtp.build_packet(rtp.RtpPacket(
            observed["target_tx"].payload_type, 0, 0, 902, backwards,
        )), ("127.0.0.1", target_port))
        async with asyncio.timeout(2):
            returned = [rtp.parse_packet(await lab.hass.loop.sock_recv(audio, 4096)) for _ in range(2)]
        # Nominal TX follows the answer; the real packets above retain source_ms.
        assert (caller.dialog.send_format.sample_rate, caller.dialog.send_format.frame_ms) == (
            source_rate, sdp.parse_sdp(observed["source_answer"])["ptime"])
        # HA must retain the already accepted destination receiver.
        assert (relay.right.outbound_rtp_format.sample_rate, relay.right.outbound_rtp_format.frame_ms) == (destination_rate, 10)
        assert all(len(packet.payload) == destination_rate * 10 // 1000 * 2 for packet in forwarded)
        for first, second in zip(forwarded, forwarded[1:]):
            assert (second.sequence - first.sequence) & 65535 == 1
            assert (second.timestamp - first.timestamp) & 0xFFFFFFFF == destination_rate // 100
        if source_rate == destination_rate:
            assert b"".join(packet.payload for packet in forwarded) == forward
        if source_rate == destination_rate == 48000:
            assert forwarded[0].payload == forward
        assert (relay.left.outbound_rtp_format.sample_rate, relay.left.outbound_rtp_format.frame_ms) == (16000, 10)
        assert [len(packet.payload) for packet in returned] == [320, 320]
        assert b"".join(packet.payload for packet in returned) == backwards
        assert (returned[1].sequence - returned[0].sequence) & 65535 == 1
        assert (returned[1].timestamp - returned[0].timestamp) & 0xFFFFFFFF == 160
        assert all(len(packet.payload) <= 1200 for packet in [*forwarded, *returned])
        assert relay.left.outbound_rtp_format.frame_ms <= sdp.parse_sdp(observed["source"].remote_sdp)["maxptime"]
        await caller.terminate()
        await wait_until(lambda: not lab.calls.sessions)
        assert not lab.runtime.rtp_port_pool.get("used")
    finally:
        await caller.close()
        for session in tuple(lab.calls.sessions.values()):
            await lab.calls.request_termination(session.call_id, TerminationIntent("test_done"))
        audio.close()
