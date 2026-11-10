"""The public answer action commits receive-video choice with the answer owner."""

import pytest
from custom_components.voip_stack.const import CONF_SIP_VIDEO, CONF_VIDEO_CAMERA_SEND
from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.local_softphone_runtime import local_softphone_bridge
from custom_components.voip_stack.websocket_api import _ha_softphone_store
from tests.test_automation_originate_ha import lab as lab

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("accept,send", [(False, True), (True, False), (True, True)])
async def test_public_browser_answer_commits_receive_video_independently_of_camera(lab, accept, send):
    lab.runtime.transport_config.update({CONF_SIP_VIDEO: True, CONF_VIDEO_CAMERA_SEND: True})
    caller = PhoneEndpoint(endpoint_id="browser:video-caller", device_id="browser-video-caller",
        name="Video caller", kind=EndpointKind.BROWSER, capabilities=frozenset({"audio", "video", "dtmf"}))
    callee = PhoneEndpoint(endpoint_id="browser:video-callee", device_id="browser-video-callee",
        name="Video callee", kind=EndpointKind.BROWSER, capabilities=frozenset({"audio", "video", "dtmf"}))
    for phone in (caller, callee):
        lab.runtime.endpoints.register(phone)
    from custom_components.voip_stack.directory_runtime import async_refresh_directory
    from custom_components.voip_stack.roster import dump_roster_json
    directory = await async_refresh_directory(lab.hass)
    lab.hass.states.async_set("sensor.voip_phonebook", "ready", {"roster_json": dump_roster_json(directory)})
    from custom_components.voip_stack.local_softphone_runtime import async_setup_local_softphone_bridge
    async_setup_local_softphone_bridge(lab.hass)
    call_id = ""
    try:
        await lab.hass.services.async_call("voip_stack", "call", {"device_id": caller.device_id,
            "destination": callee.name, "send_video": True}, blocking=True)
        call_id = _ha_softphone_store(lab.hass, caller.endpoint_id)["call_id"]
        bridge = local_softphone_bridge(lab.hass)
        ringing = bridge.get_call(call_id)
        assert ringing.video_enabled and ringing.caller_video_send
        await lab.hass.services.async_call("voip_stack", "answer", {"device_id": callee.device_id,
            "call_id": call_id, "media_client_id": "browser-answer-owner", "accept_video": accept,
            "send_video": send}, blocking=True)
        answered = bridge.get_call(call_id)
        assert answered.video_enabled is accept
        assert answered.caller_video_send is accept
        assert answered.callee_video_send is (accept and send)
        assert answered.answer_owner_id == "browser-answer-owner"
        assert _ha_softphone_store(lab.hass, callee.endpoint_id)["video_direction"] == (
            "inactive" if not accept else "sendrecv" if send else "recvonly")
    finally:
        if call_id:
            session = lab.calls.get_session(call_id)
            if session is not None:
                await lab.calls.request_termination(call_id, TerminationIntent("test_done"))


@pytest.mark.parametrize("accept,send", [(False, True), (True, False), (True, True)])
async def test_public_sip_browser_answer_declares_atomic_video_choice_on_udp(lab, monkeypatch, accept, send):
    import asyncio
    import socket
    import re
    from unittest.mock import AsyncMock
    from custom_components.voip_stack import endpoint_runtime, invite_router, peer_snapshot
    from custom_components.voip_stack.core import rtp, sdp
    from custom_components.voip_stack.core.audio_format import AudioFormat
    from custom_components.voip_stack.directory_runtime import async_refresh_directory
    from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
    from custom_components.voip_stack.roster import dump_roster_json
    from custom_components.voip_stack.sip_client import SipCallClient

    lab.runtime.sip = lab.calls = SipEndpointRuntime(allow_dark_sessions=True)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp, socket.socket() as tcp:
        udp.bind(("127.0.0.1", 0))
        listener_port = udp.getsockname()[1]
        tcp.bind(("127.0.0.1", listener_port))
    lab.runtime.transport_config.update(sip_port=listener_port,
        **{CONF_SIP_VIDEO: True, CONF_VIDEO_CAMERA_SEND: True})
    monkeypatch.setattr(peer_snapshot, "async_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(peer_snapshot, "async_build_peer_snapshot", AsyncMock(return_value=[]))
    monkeypatch.setattr(endpoint_runtime, "_roster_from_peers", lambda *_args: lab.roster)
    monkeypatch.setattr(invite_router, "_roster_from_peers", lambda *_args: lab.roster)
    browser = PhoneEndpoint(endpoint_id="browser:udp-answer", device_id="browser-udp-answer",
        name="UDP browser", kind=EndpointKind.BROWSER,
        capabilities=frozenset({"audio", "video", "dtmf"}))
    lab.runtime.endpoints.register(browser)
    lab.roster[:] = await async_refresh_directory(lab.hass)
    lab.hass.states.async_set("sensor.voip_phonebook", "ready", {"roster_json": dump_roster_json(lab.roster)})
    assert await endpoint_runtime.async_start_sip_endpoint(lab.hass)
    source = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    source.bind(("127.0.0.2", 0))
    source.setblocking(False)
    caller = SipCallClient(local_ip="127.0.0.2", local_name="Independent SIP camera",
        local_sip_port=0, local_rtp_port=35000, supported_formats=[AudioFormat(16000, frame_ms=20)],
        local_video_rtp_port=source.getsockname()[1],
        video_formats=(sdp.RtpVideoFormat(payload_type=26, encoding="JPEG", max_framerate=15),))
    answers = []
    read_response = caller._read_response

    async def observe_response(*args, **kwargs):
        response = await read_response(*args, **kwargs)
        if response is not None:
            message, _address = response
            if message.status_code == 200 and message.header("CSeq").endswith(" INVITE"):
                answers.append(message.body)
        return response

    monkeypatch.setattr(caller, "_read_response", observe_response)
    try:
        result = await caller.invite(target=browser.sip_uri_user, remote_host="127.0.0.1",
            remote_sip_port=listener_port, timeout=3)
        assert result == "ringing"
        call_id = caller.dialog_ids.call_id
        await lab.hass.services.async_call("voip_stack", "answer", {
            "device_id": browser.device_id, "call_id": call_id,
            "media_client_id": "udp-answer-owner", "accept_video": accept, "send_video": send,
        }, blocking=True)
        assert await caller.wait_for_final(timeout=3) == "in_call"
        answer = answers[-1]
        assert answer.count(b"m=") == 2
        assert caller.dialog.send_format.sample_rate == caller.dialog.recv_format.sample_rate == 16000
        media = lab.calls.resource_for(call_id, "softphone_media")
        assert media["camera_send_authorized"] is (accept and send)
        video_port = int(re.search(rb"m=video (\d+) ", answer)[1])
        assert video_port > 0 if accept else video_port == 0
        if accept:
            assert sdp.parse_video_sdp(answer)["direction"] == ("sendrecv" if send else "recvonly")
        assert media["video_direction"] == ("inactive" if not accept else "sendrecv" if send else "recvonly")
        assert media["media_client_id"] == "udp-answer-owner"
        if accept:
            # The first media packet reaches the socket reserved by the answer
            # transaction before a browser WebSocket claims that same owner.
            packet = rtp.RtpPacket(payload_type=26, sequence=41, timestamp=90000,
                ssrc=0x12345678, marker=True, payload=b"first-video-datagram")
            raw = rtp.build_packet(packet)
            await lab.hass.loop.sock_sendto(source, raw, ("127.0.0.1", video_port))
            assert await asyncio.wait_for(lab.hass.loop.sock_recv(media["video_rtp_socket"], 2048), 1) == raw
    finally:
        await caller.close()
        if caller.dialog_ids.call_id in lab.calls.sessions:
            await lab.calls.request_termination(caller.dialog_ids.call_id, TerminationIntent("test_done"))
        await endpoint_runtime.async_stop_sip_endpoint(lab.hass)
        source.close()
    assert not lab.calls.sessions and not lab.runtime.rtp_port_pool.get("used")


@pytest.mark.parametrize(
    "caller_send,callee_send,caller_video,callee_video,allow_camera,directions,negotiated",
    [
        (False, True, True, True, True, ("recvonly", "sendonly"), True),
        (True, False, True, True, True, ("sendonly", "recvonly"), True),
        (False, False, True, True, True, ("inactive", "inactive"), True),
        (False, True, False, True, True, ("inactive", "inactive"), False),
        (True, True, True, False, True, ("inactive", "inactive"), False),
        (True, True, True, True, False, ("inactive", "inactive"), True),
    ],
)
async def test_dashboard_camera_off_receives_casa_camera_on_after_public_answer(
    lab, caller_send, callee_send, caller_video, callee_video, allow_camera, directions, negotiated,
):
    """Real HA service path must separate receive negotiation from caller camera intent."""
    from custom_components.voip_stack.directory_runtime import async_refresh_directory
    from custom_components.voip_stack.roster import dump_roster_json
    from custom_components.voip_stack.local_softphone_runtime import async_setup_local_softphone_bridge
    from custom_components.voip_stack.local_softphone_bridge import LocalMediaNotNegotiatedError

    lab.runtime.transport_config.update({CONF_SIP_VIDEO: True, CONF_VIDEO_CAMERA_SEND: allow_camera})
    caller = PhoneEndpoint(endpoint_id="browser:dashboard-rx", device_id="dashboard-rx",
        name="Dashboard receive video", kind=EndpointKind.BROWSER,
        capabilities=frozenset({"audio", "dtmf"} | ({"video"} if caller_video else set())))
    callee = PhoneEndpoint(endpoint_id="browser:casa-tx", device_id="casa-tx",
        name="Casa camera sender", kind=EndpointKind.BROWSER,
        capabilities=frozenset({"audio", "dtmf"} | ({"video"} if callee_video else set())))
    for phone in (caller, callee):
        lab.runtime.endpoints.register(phone)
    directory = await async_refresh_directory(lab.hass)
    lab.hass.states.async_set("sensor.voip_phonebook", "ready", {"roster_json": dump_roster_json(directory)})
    async_setup_local_softphone_bridge(lab.hass)
    call_id = ""
    try:
        await lab.hass.services.async_call("voip_stack", "call", {
            "device_id": caller.device_id, "destination": callee.name,
            "media_client_id": "dashboard-media-owner", "send_video": caller_send}, blocking=True)
        call_id = _ha_softphone_store(lab.hass, caller.endpoint_id)["call_id"]
        bridge = local_softphone_bridge(lab.hass)
        await lab.hass.services.async_call("voip_stack", "answer", {
            "device_id": callee.device_id, "call_id": call_id,
            "media_client_id": "casa-media-owner", "send_video": callee_send, "accept_video": True}, blocking=True)
        snapshot = bridge.get_call(call_id)
        caller_lease = bridge.acquire_media(call_id, caller.endpoint_id, "dashboard-media-owner")
        callee_lease = bridge.acquire_media(call_id, callee.endpoint_id, "casa-media-owner")
        bridge.send_audio(call_id, caller.endpoint_id, caller_lease.token, b"caller-audio")
        assert bridge.receive_audio_nowait(call_id, callee.endpoint_id, callee_lease.token) == b"caller-audio"
        assert snapshot.video_enabled is negotiated
        assert snapshot.video_direction_for(caller.endpoint_id) == directions[0]
        assert snapshot.video_direction_for(callee.endpoint_id) == directions[1]
        assert _ha_softphone_store(lab.hass, caller.endpoint_id)["video_direction"] == directions[0]
        for sender, receiver, direction, payload in (
            (caller_lease, callee_lease, directions[0], b"dashboard-camera-frame"),
            (callee_lease, caller_lease, directions[1], b"casa-camera-frame"),
        ):
            if direction in ("sendonly", "sendrecv"):
                bridge.send_video(call_id, sender.endpoint_id, sender.token, payload)
                assert bridge.receive_video_nowait(call_id, receiver.endpoint_id, receiver.token) == payload
            else:
                with pytest.raises(LocalMediaNotNegotiatedError):
                    bridge.send_video(call_id, sender.endpoint_id, sender.token, payload)
        if negotiated and allow_camera and not caller_send and not callee_send:
            from custom_components.voip_stack.softphone_video_intent import async_apply_send_video_intent
            assert await async_apply_send_video_intent(lab.hass, callee.endpoint_id, True)
            updated = bridge.get_call(call_id)
            assert updated.call_id == call_id
            assert updated.video_direction_for(caller.endpoint_id) == "recvonly"
            assert updated.video_direction_for(callee.endpoint_id) == "sendonly"
            assert await async_apply_send_video_intent(lab.hass, callee.endpoint_id, False)
            assert bridge.get_call(call_id).video_direction_for(caller.endpoint_id) == "inactive"
    finally:
        if call_id and lab.calls.get_session(call_id) is not None:
            await lab.calls.request_termination(call_id, TerminationIntent("test_done"))
