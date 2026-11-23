"""The real bridge preserves a source codec while honoring receive maxptime."""

import asyncio
from dataclasses import replace
import re
import socket
import struct

import pytest

from custom_components.voip_stack.core import rtp, sdp
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.sip_client import SipCallClient
from custom_components.voip_stack.sip_udp_io import SipDatagramQueueProtocol
from tests.test_sip_capabilities_ha import wait_until
from tests.test_trunk_calls_ha import lab as lab, trunks as trunks, sip_test_account


pytestmark = pytest.mark.ha


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize(("source_rate", "destination_rate", "offer_tx10"), [
    (16000, 48000, False), (48000, 48000, False), (16000, 16000, False),
    (16000, 48000, True),
], ids=["peer16k20", "browser48k10", "independent16k20-rx-16k10-tx", "peer16k20-or10-allows10"])
async def test_bridge_keeps_each_legs_own_negotiated_audio(
    trunks, monkeypatch, source_rate, destination_rate, offer_tx10,
):
    lab = trunks
    target = lab.phones[0]
    lab.roster[1] = replace(lab.roster[1], metadata={
        "endpoint_id": "sip:bedroom0", "endpoint_kind": "sip_account", "registered": True,
        "sip_capabilities_known": True,
        "sip_audio_tx_formats": ["L16/48000/1", "L16/16000/1"],
        "sip_audio_rx_formats": ["L16/48000/1/10", "L16/16000/1/20", "L16/16000/1/10"],
        "sip_audio_tx_geometry_complete": True, "sip_audio_rx_geometry_complete": True,
        "sip_audio_receiver_ptime": 10, "sip_audio_receiver_maxptime": 10,
    })
    lab.runtime.endpoints.update("sip:bedroom0", availability="available")
    observed = {}

    def answer():
        request = target.last_request
        offered = [fmt for fmt in sdp.offered_pcm_formats(request.body) if fmt.encoding == "L16"]
        receive = next(fmt for fmt in offered if fmt.sample_rate == destination_rate)
        send = next(fmt for fmt in offered if fmt.sample_rate == 16000)
        payloads = list(dict.fromkeys([receive.payload_type, send.payload_type]))
        mappings = f"a=rtpmap:{receive.payload_type} L16/{destination_rate}/1\r\n"
        if receive.payload_type != send.payload_type:
            mappings += f"a=rtpmap:{send.payload_type} L16/16000/1\r\n"
        # Preserve either accepted destination geometry, independently of RX time.
        body = (
            "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Fixed microphone peer\r\n"
            "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
            f"m=audio {target.rtp_socket.getsockname()[1]} RTP/AVP {' '.join(map(str, payloads))}\r\n"
            f"{mappings}"
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
    # The alternative case still sends valid10ms packets, even when HA prefers20.
    offered_ms = 20 if source_rate == 16000 else 10
    source_ms = 10 if offer_tx10 else offered_ms
    account = sip_test_account(lab, "sip:bedroom1")
    caller = SipCallClient(
        local_ip="127.0.0.2", local_name="Independent source", local_sip_port=0,
        username=account.username, password=account.password,
        local_rtp_port=audio.getsockname()[1],
        supported_send_formats=[AudioFormat(source_rate, frame_ms=offered_ms)]
            + ([AudioFormat(16000, frame_ms=10)] if offer_tx10 else [])
            + [AudioFormat(rate, frame_ms=10) for rate in (48000,16000) if rate != source_rate],
        supported_recv_formats=[AudioFormat(48000, frame_ms=10),
                                AudioFormat(16000, frame_ms=20), AudioFormat(16000, frame_ms=10)],
        supported_send_rtp_formats=(sdp.RtpPcmFormat(96,"L16",48000,1,10), sdp.RtpPcmFormat(97,"L16",16000,1,20)),
        supported_recv_rtp_formats=(sdp.RtpPcmFormat(96,"L16",48000,1,10,max_frame_ms=10), sdp.RtpPcmFormat(97,"L16",16000,1,10,max_frame_ms=10)))
    read_response = caller._read_response

    async def observe_response(*args, **kwargs):
        response = await read_response(*args, **kwargs)
        if response is not None:
            message, _address = response
            if message.status_code == 200 and message.header("CSeq").endswith(" INVITE"):
                observed["source_answer"] = message.body
        return response

    monkeypatch.setattr(caller, "_read_response", observe_response)
    caller.transport, caller.protocol = await lab.hass.loop.create_datagram_endpoint(
        lambda: SipDatagramQueueProtocol(caller.queue), local_addr=("127.0.0.2", 0),
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
            assert answer_format["ptime"] == 20
            assert answer_format["maxptime"] >= source_ms == 20
            assert caller.dialog.send_format.frame_ms == answer_format["ptime"]
            assert caller.dialog.recv_format.frame_ms == 10
        session = lab.calls.get_session(caller.dialog_ids.call_id)
        relay = lab.calls.resource_for(session.call_id, "relay")
        # Each leg keeps what it negotiated for itself; nothing re-selects the caller.
        observed["source"] = session.artifacts.caller_invite
        if destination_rate == 16000:
            assert observed["source"].recv_format.frame_ms == 20
            assert observed["source"].recv_format.max_frame_ms >= 20
        samples = source_rate * source_ms // 1000
        # Rate conversion consumes complete negotiated input frames. Supply
        # consecutive short packets until one is complete, retaining the10ms
        # wire case rather than silently replacing it with a20ms packet.
        decoder, converter = relay.left_decoder, relay.left_to_right
        actual = decoder.format_for(caller.dialog.send_format.payload_type) if decoder is not None else relay.left.inbound_rtp_format
        quantum_ms = actual.frame_ms if decoder is not None and decoder.requires_fixed_input(actual) else source_ms
        if converter is not None and converter.requires_fixed_input:
            quantum_ms = max(quantum_ms, decoder.output_pcm.frame_ms)
        packet_count = max(1, (int(quantum_ms) + source_ms - 1) // source_ms)
        total_samples = samples * packet_count
        forward = struct.pack(f"!{total_samples}h", *(index - 300 for index in range(total_samples)))
        for index in range(packet_count):
            payload = forward[index * samples * 2:(index + 1) * samples * 2]
            await lab.hass.loop.sock_sendto(audio, rtp.build_packet(rtp.RtpPacket(
                caller.dialog.send_format.payload_type, index, index * samples, 901, payload,
            )), (caller.dialog.remote_rtp_host, caller.dialog.remote_rtp_port))
        async with asyncio.timeout(2):
            forwarded = [rtp.parse_packet((await target.audio.get())[0])
                         for _ in range(packet_count * source_ms // 10)]
        backwards = struct.pack("!320h", *(index - 100 for index in range(320)))
        target_port = int(re.search(rb"m=audio (\d+)", target.last_request.body)[1])
        await lab.hass.loop.sock_sendto(target.rtp_socket, rtp.build_packet(rtp.RtpPacket(
            observed["target_tx"].payload_type, 0, 0, 902, backwards,
        )), ("127.0.0.1", target_port))
        async with asyncio.timeout(2):
            returned = [rtp.parse_packet(await lab.hass.loop.sock_recv(audio, 4096)) for _ in range(2)]
        # T1 honors the remote RX request only inside the sender's hard budget.
        requested_ms = sdp.parse_sdp(observed["source_answer"])["ptime"]
        assert caller.dialog.send_format.sample_rate == source_rate
        if source_rate * requested_ms * 2 // 1000 <= 1200:
            assert caller.dialog.send_format.frame_ms == requested_ms
        else:
            assert caller.dialog.send_format.frame_ms == 10
            assert source_rate * caller.dialog.send_format.frame_ms * 2 // 1000 <= 1200
        # HA must retain the already accepted destination receiver.
        assert (relay.right.outbound_rtp_format.sample_rate, relay.right.outbound_rtp_format.frame_ms) == (destination_rate, 10)
        assert all(len(packet.payload) == destination_rate * 10 // 1000 * 2 for packet in forwarded)
        for first, second in zip(forwarded, forwarded[1:]):
            assert (second.sequence - first.sequence) & 65535 == 1
            assert (second.timestamp - first.timestamp) & 0xFFFFFFFF == destination_rate // 100
        if source_rate == destination_rate:
            assert b"".join(packet.payload for packet in forwarded) == forward
        if source_rate == destination_rate == 48000:
            assert [packet.payload for packet in forwarded] == [
                forward[offset:offset + 960] for offset in range(0, len(forward), 960)]
        # The caller offered 48 kHz first for its receiver: it gets that, at 10 ms,
        # whatever the destination transmits. HA converts instead of lowering the leg.
        assert (relay.left.outbound_rtp_format.sample_rate, relay.left.outbound_rtp_format.frame_ms) == (48000, 10)
        assert [len(packet.payload) for packet in returned] == [960, 960]
        assert (returned[1].sequence - returned[0].sequence) & 65535 == 1
        assert (returned[1].timestamp - returned[0].timestamp) & 0xFFFFFFFF == 480
        upsampled = struct.unpack("!960h", b"".join(packet.payload for packet in returned))
        # The 16 kHz ramp (-100..219) arrives as a 48 kHz ramp over the same range,
        # after the converter's start-up delay and with its small filter overshoot.
        assert min(upsampled) >= -130 and max(upsampled) <= 240 and max(upsampled) - min(upsampled) > 200
        assert sum(later >= earlier for earlier, later in zip(upsampled, upsampled[1:])) > 850
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
