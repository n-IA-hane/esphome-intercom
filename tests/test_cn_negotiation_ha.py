"""Actual authenticated SIP and RTP negotiate optional CN through the HA bridge."""

import asyncio
import socket

import pytest

from custom_components.voip_stack.core import rtp, sdp, sip
from custom_components.voip_stack.core.rtp_payload import RtpPayloadDecoder
from custom_components.voip_stack.core.codec_policy import CodecPolicy
from custom_components.voip_stack.core.media import shadow
from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
from custom_components.voip_stack.roster import RosterEntry
from custom_components.voip_stack.sip_client import SipCallClient
from custom_components.voip_stack.local_call_media import LocalAudioContract
from custom_components.voip_stack.rtp_audio_receiver import RtpAudioReceiver
from custom_components.voip_stack.sip_udp_io import SipDatagramQueueProtocol

from .test_automation_originate_ha import assert_call_resources_released
from .test_trunk_calls_ha import lab as lab, trunks as trunks, sip_test_account

pytestmark = pytest.mark.ha


@pytest.fixture(autouse=True)
def active_engine():
    previous = shadow.engine_mode()
    shadow.set_engine_mode("active")
    try:
        yield
    finally:
        shadow.set_engine_mode(previous)


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("cn_enabled", [False, True])
async def test_authenticated_invite_cn_sid_speech_cleanup_and_second_call(trunks, monkeypatch, cn_enabled):
    lab = trunks
    entry = lab.hass.config_entries.async_entries("voip_stack")[0]
    lab.hass.config_entries.async_update_entry(entry, data={
        **entry.data, "audio_codecs": ["PCMU", *(["CN"] if cn_enabled else [])], "video_codecs": [],
    })
    lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id="source", name="Source", username="source",
                                               kind=EndpointKind.SIP_ACCOUNT, availability="available"))
    lab.roster.append(RosterEntry("source", address="127.0.0.2", metadata={"endpoint_id": "source"}))
    lab.runtime.directory.replace(lab.roster)
    account = sip_test_account(lab, "source")
    destination = lab.phones[0]

    def answer_pcmu():
        request = destination.last_request
        primary = next(fmt for fmt in sdp.offered_pcm_formats(request.body) if fmt.encoding == "PCMU")
        body = sdp.build_answer_directional("127.0.0.1", "127.0.0.1", destination.rtp_socket.getsockname()[1],
                                            primary, primary, remote_sdp=request.body)
        destination.respond(request, destination.remote, 200, body.encode())
        destination.answered.set()

    monkeypatch.setattr(destination, "answer", answer_pcmu)
    source = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    source.bind(("127.0.0.2", 0))
    source.setblocking(False)
    try:
        for call_number in range(2):
            client = SipCallClient(local_ip="127.0.0.2", local_name="Source", local_uri_user="source",
                local_sip_port=0, local_rtp_port=source.getsockname()[1], username=account.username,
                password=account.password, include_common_codecs=True, codec_policy=CodecPolicy(audio=("PCMU", "CN")))
            client.transport, client.protocol = await lab.hass.loop.create_datagram_endpoint(
                lambda: SipDatagramQueueProtocol(client.queue), local_addr=("127.0.0.2", 0))
            client.local_sip_port = client.transport.get_extra_info("sockname")[1]
            answers = []
            def observe_answer(raw, address, deliver=client.protocol.datagram_received, answers=answers):
                message = sip.parse_message(raw)
                if message.status_code == 200 and message.header("CSeq").endswith(" INVITE"):
                    answers.append(message.body)
                deliver(raw, address)
            monkeypatch.setattr(client.protocol, "datagram_received", observe_answer)
            try:
                result = await client.invite(target="External", remote_host="127.0.0.1",
                    remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3)
                if result == "ringing":
                    result = await client.wait_for_final(timeout=3)
                assert result == "in_call"
                dialog = client.dialog
                assert dialog is not None
                session = lab.calls.get_session(dialog.call_id)
                relay = lab.calls.resource_for(dialog.call_id, "relay")
                assert relay is not None
                actual_cn = [pt for pt, mapping in sdp.parse_sdp(client._local_sdp_body)["rtpmap"].items()
                             if mapping[0] == "CN"]
                assert len(actual_cn) == 1
                assert len(answers) == 1
                assert (13 in sdp.parse_sdp(answers[0])["payload_order"]) is cn_enabled
                assert any(fmt.encoding == "CN" for fmt in dialog.recv_formats) is cn_enabled
                contract = LocalAudioContract.from_dialog(dialog)
                assert contract.recv_formats == dialog.recv_formats
                local_receiver = RtpAudioReceiver(contract.recv_format, contract.recv_formats)
                if cn_enabled:
                    assert local_receiver.decode(actual_cn[0], bytes([40]))
                    assert all(any(local_receiver.take_comfort_noise()[0]) for _ in range(3))
                    local_receiver.decode(0, b"\xff" * 160)
                    assert not local_receiver.comfort_noise_active
                assert any(fmt.encoding == "CN" for fmt in relay.left.inbound_rtp_formats) is cn_enabled
                assert all(fmt.encoding != "CN" for fmt in relay.right.inbound_rtp_formats)
                assert not relay._cn_timers
                # The source sends to the payload number actually accepted in its answer.
                sid_pt = next((fmt.payload_type for fmt in relay.left.inbound_rtp_formats if fmt.encoding == "CN"), 13)
                target = (dialog.remote_rtp_host, dialog.remote_rtp_port)
                def wire(pt, payload, sequence):
                    return rtp.build_packet(rtp.RtpPacket(pt, sequence, sequence * 160, 901 + call_number, payload))
                await lab.hass.loop.sock_sendto(source, wire(sid_pt, bytes([40]), 1), target)
                if cn_enabled:
                    packets = [rtp.parse_packet((await asyncio.wait_for(destination.audio.get(), .5))[0]) for _ in range(4)]
                    assert all(packet.payload_type == 0 and len(packet.payload) == 160 for packet in packets)
                    decoder = RtpPayloadDecoder(dialog.recv_format)
                    assert all(any(decoder.decode(packet.payload)) for packet in packets)
                    assert len({packet.payload for packet in packets}) > 1
                else:
                    with pytest.raises(TimeoutError):
                        await asyncio.wait_for(destination.audio.get(), .09)
                await lab.hass.loop.sock_sendto(source, wire(0, b"\xff" * 160, 2), target)
                async with asyncio.timeout(.5):
                    while rtp.parse_packet((await destination.audio.get())[0]).payload != b"\xff" * 160:
                        pass
                assert not relay._cn_timers
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(destination.audio.get(), .07)
                assert client.bye()
                await asyncio.wait_for(session.termination_started.wait(), 3)
                barrier = lab.calls.request_termination(session.call_id, session.termination_intent)
                if barrier is not None:
                    await barrier
                assert not relay._cn_timers
                assert_call_resources_released(lab)
                assert not lab.runtime.rtp_port_pool["used"]
            finally:
                await client.close()
    finally:
        source.close()
