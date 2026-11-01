"""A bridged re-offer retains the current receiver's frame-rate budget."""

import asyncio
from dataclasses import replace
import socket
from unittest.mock import AsyncMock

import pytest

from custom_components.voip_stack import endpoint_runtime, invite_router, peer_snapshot
from custom_components.voip_stack.const import CONF_SIP_VIDEO
from custom_components.voip_stack.core import sdp
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
from custom_components.voip_stack.sip_client import SipCallClient
from tests.test_automation_originate_ha import lab as lab


pytestmark = pytest.mark.ha


@pytest.mark.parametrize("direction,receiver_limit", [
    pytest.param("sendonly", 2, id="direction-only-unchanged-receiver"),
    pytest.param("sendrecv", 2, id="unchanged-direction"),
    pytest.param("sendonly", 5, id="fresh-receiver-budget"),
])
async def test_bridge_reoffer_answers_current_receiver_budget(lab, monkeypatch, direction, receiver_limit):
    lab.runtime.sip = lab.calls = SipEndpointRuntime(allow_dark_sessions=True)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp, socket.socket() as tcp:
        udp.bind(("127.0.0.1", 0))
        listener_port = udp.getsockname()[1]
        tcp.bind(("127.0.0.1", listener_port))
    lab.runtime.transport_config.update(sip_port=listener_port)
    lab.runtime.transport_config[CONF_SIP_VIDEO] = True
    monkeypatch.setattr(peer_snapshot, "async_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(peer_snapshot, "async_build_peer_snapshot", AsyncMock(return_value=[]))
    monkeypatch.setattr(endpoint_runtime, "_roster_from_peers", lambda *_args: lab.roster)
    monkeypatch.setattr(invite_router, "_roster_from_peers", lambda *_args: lab.roster)
    assert await endpoint_runtime.async_start_sip_endpoint(lab.hass)
    lab.runtime.endpoints.update("sip:bedroom0", availability="available")
    receiver = lab.phones[0]

    def answer():
        request = receiver.last_request
        audio = next(fmt for fmt in sdp.offered_pcm_formats(request.body)
            if fmt.encoding == "L16" and fmt.sample_rate == 16000)
        video = sdp.offered_video_formats(request.body)[0]
        assert video.max_framerate == 10
        limit = 2 if len(receiver.invites) == 1 else receiver_limit
        body = sdp.build_answer_directional("127.0.0.1", "127.0.0.1",
            receiver.rtp_socket.getsockname()[1], audio, audio, remote_sdp=request.body,
            video_port=35002, video_format=replace(video, max_framerate=limit),
            video_direction="recvonly").encode()
        receiver.respond(request, receiver.remote, 200, body)
        receiver.answered.set()

    monkeypatch.setattr(receiver, "answer", answer)
    caller = SipCallClient(local_ip="127.0.0.2", local_name="Independent camera caller",
        local_sip_port=0, local_rtp_port=35000, supported_formats=[AudioFormat(16000, frame_ms=10)],
        local_video_rtp_port=35004,
        video_formats=(sdp.RtpVideoFormat(payload_type=26, encoding="JPEG", max_framerate=10),))
    answers = []
    read_response = caller._read_response

    async def observe_response(*args, **kwargs):
        response = await read_response(*args, **kwargs)
        if response is not None:
            message, _address = response
            if message.status_code == 200 and message.header("CSeq").endswith(" INVITE"):
                answers.append(message)
        return response

    monkeypatch.setattr(caller, "_read_response", observe_response)
    try:
        result = await caller.invite(target="bedroom0", remote_host="127.0.0.1",
            remote_sip_port=listener_port, timeout=3)
        if result == "ringing":
            result = await caller.wait_for_final(timeout=3)
        assert result == "in_call"
        initial = caller.dialog
        assert sdp.offered_video_formats(answers[-1].body)[0].max_framerate == 2
        call_id = caller.dialog_ids.call_id
        relay = lab.calls.resource_for(call_id, "relay")
        destination = lab.calls.sip_client_for(lab.calls.bridge_for(call_id)[1])
        # This is the actual asymmetric contract: the decoder answer is 2,
        # while the distinct local receive contract still comes from offer 10.
        assert destination.dialog.send_video_format.max_framerate == 2
        assert destination.dialog.recv_video_format.max_framerate == 10

        candidate = await caller.async_prepare_video_reinvite(local_video_rtp_port=35004,
            video_formats=caller.video_formats, video_direction=direction)
        assert candidate is not None
        assert len(answers) == 2
        answer_format = sdp.offered_video_formats(answers[-1].body)[0]
        assert answer_format.max_framerate == receiver_limit
        assert answer_format.direction == "recvonly"
        assert answer_format.encoding == "JPEG" and answer_format.payload_type == 26
        assert caller.commit_prepared_reinvite(initial, candidate)
        async with asyncio.timeout(3):
            while relay.video_relay.left.recv_format.max_framerate != receiver_limit:
                await asyncio.sleep(0)
        assert destination.dialog.send_video_format.max_framerate == receiver_limit
        assert destination.dialog.recv_video_format.max_framerate == 10
        assert len(receiver.invites) == (1 if direction == "sendrecv" else 2)
        assert caller.bye()
        session = lab.calls.get_session(call_id)
        await asyncio.wait_for(session.termination_started.wait(), 3)
        barrier = lab.calls.request_termination(call_id, session.termination_intent)
        if barrier is not None:
            await barrier
        assert not lab.calls.sessions and not lab.runtime.rtp_port_pool.get("used")
    finally:
        await caller.close()
        for session in tuple(lab.calls.sessions.values()):
            await lab.calls.request_termination(session.call_id, TerminationIntent("test_done"))
        await endpoint_runtime.async_stop_sip_endpoint(lab.hass)
