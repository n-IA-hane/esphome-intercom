"""Outgoing browser calls keep their session while a SIP destination is replaced."""

import asyncio
import re
import socket
import struct
from functools import partial
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.core import Context, ServiceCall

from custom_components.voip_stack import call_forwarder, softphone_originate
from custom_components.voip_stack.endpoint_dialing import EndpointDialer
from custom_components.voip_stack.endpoint_routing import EndpointRouteResolver
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.local_softphone_runtime import (
    async_setup_local_softphone_bridge, async_shutdown_local_softphone_bridge,
)
from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
from custom_components.voip_stack.roster import RosterEntry, dump_roster_json
from custom_components.voip_stack.sip_runtime import enable_reused_signaling_flow, uri_transport
from custom_components.voip_stack.core import rtp

from .test_automation_originate_ha import lab as lab

pytestmark = pytest.mark.ha


@pytest.fixture
async def browser_lab(lab, monkeypatch):
    for name in ("sip:bedroom0", "sip:bedroom1"):
        lab.runtime.endpoints.update(name, availability="available")
    for name in ("browser", "kitchen"):
        lab.runtime.endpoints.register(PhoneEndpoint(
            endpoint_id=name, name=name, device_id=f"device-{name}", kind=EndpointKind.BROWSER,
            availability="available", capabilities=frozenset({"audio", "dtmf"}),
        ))
        lab.roster.append(RosterEntry(name, metadata={"local_ha": True, "endpoint_id": name}))
    lab.hass.states.async_set("sensor.voip_phonebook", "4", {"roster_json": dump_roster_json(lab.roster)})
    monkeypatch.setattr(softphone_originate, "_ha_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(softphone_originate, "_resolve_target_device", AsyncMock(return_value=None))
    monkeypatch.setattr(call_forwarder, "_async_build_peer_snapshot", AsyncMock(return_value=[]))
    monkeypatch.setattr(call_forwarder, "_roster_from_peers", lambda *_args: lab.roster)
    resolver = EndpointRouteResolver(hass=lab.hass, local_ip="127.0.0.1", sip_port=5099)
    dialer = EndpointDialer(lab.hass, "127.0.0.1", lab.runtime.transport_config,
                           resolver, uri_transport, enable_reused_signaling_flow)
    lab.forward = call_forwarder.ForwardRuntime(
        hass=lab.hass, config=lab.runtime.transport_config, local_ip="127.0.0.1",
        route_resolver=resolver, browser_leg_for_member=dialer.browser_leg_for_member,
        defer_invite_to_softphone=Mock(side_effect=AssertionError("No inbound SIP response for a browser source")),
        prepare_outbound_leg=dialer.prepare_outbound_leg,
        publish_pending_ringing=Mock(), start_local_assist_bridge=AsyncMock(),
    )
    lab.calls.forward_call = partial(call_forwarder.async_forward_existing_call, lab.forward)
    lab.bridge = async_setup_local_softphone_bridge(lab.hass)
    yield lab
    async_shutdown_local_softphone_bridge(lab.hass)


async def originate(lab, *, send_video=False):
    endpoint = lab.runtime.endpoints.get("browser")
    await softphone_originate.async_originate_browser_call(
        ServiceCall(lab.hass, "voip_stack", "call", {
            "destination": "bedroom0", "device_id": endpoint.device_id, "media_client_id": "browser-owner",
            "send_video": send_video,
        }, context=Context()), endpoint_id=endpoint.endpoint_id, browser_endpoint=endpoint,
    )
    return lab.calls.get_session(lab.phones[0].invites[0].header("Call-ID"))


@pytest.mark.parametrize("destination", ["Wakeup-Caller", "kitchen", "bedroom1"])
@pytest.mark.parametrize("late_answer", [False, True])
async def test_outgoing_browser_forward_preserves_caller_and_closes_old_sip_leg(browser_lab, destination, late_answer):
    from custom_components.voip_stack.automation_call import automation_call
    from custom_components.voip_stack.phone_control import CallControlRequest, PhoneOperation

    lab = browser_lab
    first = lab.phones[0]
    first.auto_answer = False
    first.answer_on_cancel = late_answer
    session = await originate(lab)
    token = session.token
    assert session.state == "remote_ringing"
    assert session.owner == "ha_softphone"
    assert session.metadata.get("source_endpoint_id") == "browser"
    assert lab.calls.event_context(session.call_id).state == "remote_ringing"
    await lab.calls.forward_call(call_id=session.call_id, destination=destination, on_failure="terminate")
    task = lab.calls.task_for(session.call_id, "forward")
    if task is not None:
        async with asyncio.timeout(4):
            await task
    assert session.live
    assert session.token == token
    assert any(request.method == "CANCEL" for request in first.requests)
    if late_answer:
        assert first.bye.is_set()
    assert lab.runtime.endpoints.require("browser").active_call_id == session.call_id
    assert lab.runtime.endpoints.require("sip:bedroom0").active_call_id == ""
    assert lab.calls.sip_client_for(first.invites[0].header("Call-ID")) is None
    if destination == "Wakeup-Caller":
        app = automation_call(lab.hass, session.call_id)
        assert app is not None and app.local_source.outgoing
        await app.answer()
        assert app.answered
    elif destination == "kitchen":
        assert lab.bridge.get_call(session.call_id).callee_endpoint_id == "kitchen"
    else:
        assert len(lab.phones[1].invites) == 1
        assert session.state == "in_call"
        descriptor = lab.calls.resource_for(session.call_id, "softphone_media")
        browser_rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        browser_rtp.bind(("127.0.0.1", 0))
        browser_rtp.setblocking(False)
        try:
            # Exercise the same UDP boundary as the browser WebSocket adapter.
            send = descriptor["send_format"]
            frames = send.sample_rate * send.frame_ms // 1000
            for index in range(6):
                raw = rtp.build_packet(rtp.RtpPacket(
                    send.payload_type, index, index * frames, 1001,
                    struct.pack(f"!{frames}h", *([1000] * frames)),
                ))
                await lab.hass.loop.sock_sendto(browser_rtp, raw,
                    (descriptor["remote_rtp_host"], descriptor["remote_rtp_port"]))
            async with asyncio.timeout(2):
                raw, _ = await lab.phones[1].audio.get()
                payload = rtp.parse_packet(raw).payload
                samples = struct.unpack(f"!{len(payload) // 2}h", payload)
                assert max(samples) >= 950
                destination_port = int(re.search(rb"m=audio (\d+)", lab.phones[1].invites[0].body)[1])
                # The independent phone answers with the offered packet time.
                # Generate that actual wire quantum rather than assuming 20ms.
                packet_time = int(re.search(rb"a=ptime:(\d+)", lab.phones[1].invites[0].body)[1])
                reverse_samples = 16000 * packet_time // 1000
                for index in range(3):
                    reverse = rtp.build_packet(rtp.RtpPacket(
                        lab.phones[1].payload_type, index, index * reverse_samples, 2002,
                        struct.pack(f"!{reverse_samples}h", *([5000] * reverse_samples)),
                    ))
                    await lab.hass.loop.sock_sendto(lab.phones[1].rtp_socket, reverse,
                                                  ("127.0.0.1", destination_port))
                returned = rtp.parse_packet(await lab.hass.loop.sock_recv(browser_rtp, 4096))
                assert returned.payload_type == descriptor["recv_format"].payload_type
                samples = struct.unpack(f"!{len(returned.payload) // 2}h", returned.payload)
                assert max(samples) >= 4900
        finally:
            browser_rtp.close()
    await lab.runtime.phones.control(
        ServiceCall(lab.hass, "voip_stack", "hangup", {"device_id": "device-browser", "call_id": session.call_id}),
        PhoneOperation.HANGUP, CallControlRequest(call_id=session.call_id),
    )
    barrier = lab.calls.request_termination(session.call_id, TerminationIntent("test_done"))
    if barrier is not None:
        await barrier
    assert not session.live
    assert not lab.calls.sessions
    assert all(not phone.active_call_id for phone in lab.runtime.endpoints.endpoints)


async def test_two_no_answer_destinations_then_automation_keep_the_browser_caller(browser_lab):
    from homeassistant.core import callback
    from custom_components.voip_stack.automation_call import automation_call
    from custom_components.voip_stack.phone_policy import PhonePolicy
    from custom_components.voip_stack.phone_policy_runtime import setup_phone_policy_events
    from custom_components.voip_stack.store import config_entry
    from custom_components.voip_stack.websocket_api import CALL_EVENT

    lab = browser_lab
    setup_phone_policy_events(lab.hass, config_entry(lab.hass))
    for index, target in ((0, "sip:bedroom1"), (1, "Wakeup-Caller")):
        lab.phones[index].auto_answer = False
        lab.runtime.endpoints.update(f"sip:bedroom{index}", policy=PhonePolicy.from_data({
            "ringing_timeout": 1, "no_answer": {"kind": "contact", "target": target},
        }))
    ready = asyncio.Event()

    @callback
    def observe(event):
        if event.data.get("event_type") == "automation_requested":
            ready.set()

    unsubscribe = lab.hass.bus.async_listen(CALL_EVENT, observe)
    try:
        session = await originate(lab)
        token = session.token
        async with asyncio.timeout(5):
            await ready.wait()
        app = automation_call(lab.hass, session.call_id)
        assert app is not None
        assert app.session.token == token
        assert app.session.live
        for phone in lab.phones:
            assert len(phone.invites) == 1
            assert any(request.method == "CANCEL" for request in phone.requests)
        await app.answer()
        assert app.media is not None
        assert lab.runtime.endpoints.require("browser").active_call_id == session.call_id
        await lab.calls.request_termination(session.call_id, TerminationIntent("test_done"))
        assert not lab.calls.sessions
    finally:
        unsubscribe()


async def test_answer_during_forward_resolution_keeps_original_browser_call(browser_lab, monkeypatch):
    from homeassistant.core import callback
    from homeassistant.exceptions import ServiceValidationError
    from custom_components.voip_stack.websocket_api import CALL_EVENT

    lab = browser_lab
    first = lab.phones[0]
    first.auto_answer = False
    session = await originate(lab)
    original = lab.calls.sip_client_for(session.call_id)
    answered = asyncio.Event()

    @callback
    def observe(event):
        if event.data.get("call_id") == session.call_id and event.data.get("state") == "in_call":
            answered.set()

    unsubscribe = lab.hass.bus.async_listen(CALL_EVENT, observe)

    async def peers(_hass):
        first.answer()
        async with asyncio.timeout(2):
            await answered.wait()
        return []

    monkeypatch.setattr(call_forwarder, "_async_build_peer_snapshot", peers)
    try:
        with pytest.raises(ServiceValidationError, match="already answered"):
            await lab.calls.forward_call(call_id=session.call_id, destination="bedroom1", on_failure="terminate")
        assert session.live and session.owner == "ha_softphone"
        assert lab.calls.sip_client_for(session.call_id) is original
        assert lab.calls.artifact_for(session.call_id, "pending_invite") is None
        assert lab.calls.resource_for(session.call_id, "browser_source") is None
        assert not any(request.method in {"CANCEL", "BYE"} for request in first.requests)
    finally:
        unsubscribe()


async def test_cancel_forward_after_old_sip_leg_closes_releases_caller(browser_lab, monkeypatch):
    from custom_components.voip_stack.local_softphone_runtime import PendingLocalSource

    lab = browser_lab
    lab.phones[0].auto_answer = False
    session = await originate(lab)
    retired = asyncio.Event()
    finish = asyncio.Event()
    retire = PendingLocalSource.retire_destination

    async def paused_retire(source):
        await retire(source)
        retired.set()
        await finish.wait()

    monkeypatch.setattr(PendingLocalSource, "retire_destination", paused_retire)
    task = asyncio.create_task(lab.calls.forward_call(
        call_id=session.call_id, destination="bedroom1", on_failure="terminate",
    ))
    try:
        async with asyncio.timeout(2):
            await retired.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not session.live and session.termination_intent.reason == "route_failed"
            barrier = lab.calls.request_termination(session.call_id, session.termination_intent)
            if barrier is not None:
                await barrier
        assert not lab.calls.sessions
        assert not lab.phones[1].invites
        assert all(not endpoint.active_call_id for endpoint in lab.runtime.endpoints.endpoints)
    finally:
        finish.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("destination", ["kitchen", "bedroom1"])
async def test_forward_preserves_explicit_browser_camera_intent(browser_lab, destination):
    from custom_components.voip_stack.const import CONF_SIP_VIDEO, CONF_VIDEO_CAMERA_SEND

    lab = browser_lab
    lab.runtime.transport_config.update({CONF_SIP_VIDEO: True, CONF_VIDEO_CAMERA_SEND: True})
    for name in ("browser", "kitchen", "sip:bedroom0", "sip:bedroom1"):
        lab.runtime.endpoints.update(name, capabilities=frozenset({"audio", "video", "dtmf"}))
    for entry in lab.roster:
        if entry.id.startswith("bedroom"):
            entry.metadata["sip_video_codec"] = "VP8"
    lab.hass.states.async_set("sensor.voip_phonebook", "4", {"roster_json": dump_roster_json(lab.roster)})
    lab.phones[0].auto_answer = False
    session = await originate(lab, send_video=True)
    assert b"m=video " in lab.phones[0].invites[0].body
    await lab.calls.forward_call(call_id=session.call_id, destination=destination, on_failure="terminate")
    task = lab.calls.task_for(session.call_id, "forward")
    if task is not None:
        async with asyncio.timeout(3):
            await task
    assert session.live
    if destination == "kitchen":
        local_call = lab.bridge.get_call(session.call_id)
        assert local_call.video_requested
        assert local_call.caller_video_send
    else:
        assert b"m=video " in lab.phones[1].invites[0].body
        # The simulated peer declines video but accepts audio.
        assert session.state == "in_call"


async def test_native_unanswered_action_can_speak_to_outgoing_browser_caller(browser_lab, monkeypatch):
    from custom_components import voip_stack
    from custom_components.voip_stack import automation_call, automation_context, peer_snapshot, local_application

    lab = browser_lab
    lab.phones[0].auto_answer = False
    session = await originate(lab)
    context = lab.calls.event_context(session.call_id)
    execution = automation_context.CallExecution(session.token, {"automation_control": "ha_anchored"},
        ringing_guard=(context.sequence, session.callee))
    binding = automation_context.current_execution.set(execution)
    monkeypatch.setattr(peer_snapshot, "async_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(local_application, "send_final_response",
                        Mock(side_effect=AssertionError("The browser caller has no SIP INVITE to answer")))
    try:
        call = automation_context.bind_call_action(ServiceCall(lab.hass, "voip_stack", "tts_say", {}))
        app, _ = await automation_call.resolve_application_action(call)
        app.claim(call)
        await app.answer()
        assert app.answered and session.live
        assert execution.ringing_guard is None
        assert any(request.method == "CANCEL" for request in lab.phones[0].requests)
        assert lab.runtime.endpoints.require("browser").active_call_id == session.call_id
        assert not lab.runtime.endpoints.require("sip:bedroom0").active_call_id
        await voip_stack._handle_sip_hangup_service(automation_context.bind_call_action(
            ServiceCall(lab.hass, "voip_stack", "hangup", {}),
        ))
        assert not lab.calls.sessions
    finally:
        automation_context.current_execution.reset(binding)


async def test_failed_browser_forward_can_resume_original_destination_with_same_call(browser_lab, monkeypatch):
    lab = browser_lab
    first, second = lab.phones
    first.auto_answer = False
    second.final_status = 486
    retried = asyncio.Event()
    receive = first.datagram_received

    def observe_retry(raw, address):
        receive(raw, address)
        if len(first.invites) == 2:
            retried.set()

    monkeypatch.setattr(first, "datagram_received", observe_retry)
    session = await originate(lab)
    token = session.token
    await lab.calls.forward_call(call_id=session.call_id, destination="bedroom1", on_failure="resume")
    async with asyncio.timeout(3):
        await retried.wait()
    assert session.live and session.token == token
    assert len(second.invites) == 1
    assert first.invites[0].header("Call-ID") != first.invites[1].header("Call-ID")
    assert lab.runtime.endpoints.require("browser").active_call_id == session.call_id
    await lab.calls.request_termination(session.call_id, TerminationIntent("test_done"))
    assert not lab.calls.sessions
