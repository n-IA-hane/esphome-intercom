"""Automation application ownership, real UDP playback and Assist handoff."""

from __future__ import annotations

import asyncio
import socket
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.core import Context, ServiceCall
from homeassistant.exceptions import ServiceValidationError

from custom_components.voip_stack import automation_call as module
from custom_components.voip_stack import media_ports, local_application
from custom_components.voip_stack.browser_playback import BrowserPlayback
from custom_components.voip_stack.core import rtp, sdp, sip
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
from custom_components.voip_stack.roster import RosterEntry
from custom_components.voip_stack.router import RouteAction, resolve_ha_router
from custom_components.voip_stack.sip_listener import SipInvite

pytestmark = pytest.mark.ha


async def test_automation_idle_deadline_uses_the_session_resource_owner(application):
    app, registry, _peer, _released = application
    app.arm_deadline()
    name = "deadline:automation"
    try:
        assert registry.resource_for(app.session.call_id, name) is app.deadline
    finally:
        app.cancel_deadline()
    assert registry.resource_for(app.session.call_id, name) is None


@pytest.fixture
def application(hass, monkeypatch, socket_enabled):
    registry = SipEndpointRuntime(allow_dark_sessions=True)
    registry.activate()
    peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer.bind(("127.0.0.1", 0))
    peer.setblocking(False)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    released = []
    reservation = media_ports.RtpPortReservation(hass, (port, port + 2))
    monkeypatch.setattr(
        media_ports, "release_sip_rtp_port_pair", lambda _h, pair: released.append(pair)
    )
    monkeypatch.setattr(local_application.RtpPortReservation, "allocate", lambda _h: reservation)
    monkeypatch.setattr(module, "call_registry", lambda _h: registry)
    monkeypatch.setattr(local_application, "call_registry", lambda _h: registry)
    monkeypatch.setattr(local_application, "publish_bridge_projection", Mock())
    monkeypatch.setattr(module, "publish_bridge_projection", Mock())
    monkeypatch.setattr(local_application, "send_final_response", Mock(return_value=True))
    monkeypatch.setattr(module, "call_runtime_artifacts", lambda _h: registry)
    fmt = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
    invite = SipInvite(
        source_host="127.0.0.1",
        source_port=5060,
        request_uri=sip.SipUri("666", "127.0.0.1", 5060),
        caller_uri=sip.SipUri("caller", "127.0.0.1", 5060),
        target="666",
        caller="Caller",
        call_id="automation-test",
        cseq="1 INVITE",
        remote_sdp=b"v=0\r\nc=IN IP4 127.0.0.1\r\nm=audio 40000 RTP/AVP 96\r\na=rtpmap:96 L16/16000\r\na=ptime:20\r\n",
        send_format=fmt,
        recv_format=fmt,
        remote_rtp_host="127.0.0.1",
        remote_rtp_port=peer.getsockname()[1],
    )
    contact = RosterEntry(
        "Welcome",
        extension="666",
        ha_bridge=True,
        metadata={"virtual_endpoint": "automation"},
    )
    session = registry.upsert(
        invite.call_id, state="ringing", owner="automation", callee="Welcome"
    )
    registry.set_pending_invite(invite.call_id, invite)
    app = module.AutomationCall(hass, session, invite, contact, "127.0.0.1")
    registry.own_resource(
        invite.call_id, f"automation:{invite.call_id}", app, app.close
    )
    yield app, registry, peer, released
    peer.close()


def service(app, *, context=None, **data):
    return ServiceCall(
        app.hass,
        "voip_stack",
        "tts_say",
        {
            "call_id": app.session.call_id,
            "expected_generation": app.session.generation,
            "tts_entity_id": "tts.test",
            "message": "Hello",
            **data,
        },
        context=context or Context(),
    )


@pytest.mark.parametrize("target", ["Welcome", "666"])
def test_automation_contact_is_local_even_with_external_trunk(target):
    contact = RosterEntry(
        "Welcome", extension="666", metadata={"virtual_endpoint": "automation"}
    )
    assert (
        resolve_ha_router(target, [contact], trunk_ready=True).action
        is RouteAction.AUTOMATION
    )


@pytest.mark.parametrize("phone_takeover", [False, True])
async def test_announcement_answers_once_and_sends_complete_pcm(
    application, monkeypatch, phone_takeover
):
    app, registry, peer, released = application
    binding = None
    if phone_takeover:
        from custom_components.voip_stack import automation_context, peer_snapshot, browser_phone_runtime
        from custom_components.voip_stack.call_scope import call_belongs_to_endpoint
        registry.release_resource(app.session.call_id, f"automation:{app.session.call_id}", value=app)
        registry.transition(app.session.call_id, owner="ha_softphone", state="ringing")
        app.session.metadata.update(endpoint_id="old-phone", source_endpoint_id="sip-caller",
                                    dest_endpoint_id="old-phone", target_endpoint_id="old-phone")
        fields = registry.event_fields(app.session.call_id, "ringing")
        execution = automation_context.CallExecution(app.session.token, {
            **fields, "automation_control": "routable",
        }, ringing_guard=(fields["sequence"], app.session.callee))
        binding = module.current_execution.set(execution)
        monkeypatch.setattr(peer_snapshot, "async_advertise_host", AsyncMock(return_value="127.0.0.1"))
        monkeypatch.setattr(browser_phone_runtime, "_release_ha_softphone_claim", Mock())
        original_token = app.session.token
        app, _ = await module.resolve_application_action(service(app))
        assert app.session.token == original_token
        assert app.session.owner == "automation"
        assert app.controller == execution.controller
        assert execution.ringing_guard is None
        assert not call_belongs_to_endpoint(registry, app.session.call_id, "old-phone")
        assert call_belongs_to_endpoint(registry, app.session.call_id, "sip-caller")
    pcm = b"\x80\x01" * 960

    class Stream:
        def async_set_message(self, message):
            assert message == "Hello"

        async def async_stream_result(self):
            for chunk in (pcm[:13], pcm[13:777], pcm[777:]):
                yield chunk

    from homeassistant.components import tts

    monkeypatch.setattr(tts, "async_create_stream", lambda *_args: Stream())
    try:
        await app.say(service(app))
        assert app.answered
        assert local_application.send_final_response.call_count == 1
        received = bytearray()
        async with asyncio.timeout(2):
            while len(received) < len(pcm):
                raw = await asyncio.get_running_loop().sock_recv(peer, 2048)
                packet = rtp.parse_packet(raw)
                # RTP L16 is network byte order, independent of the input PCM.
                if any(packet.payload):
                    swapped = bytearray(len(packet.payload))
                    swapped[::2], swapped[1::2] = (
                        packet.payload[1::2],
                        packet.payload[::2],
                    )
                    received.extend(swapped)
        assert bytes(received) == pcm
        assert app.media._consumer_task is None
    finally:
        await registry.request_termination(
            app.session.call_id, TerminationIntent("test_done")
        )
        if binding is not None:
            module.current_execution.reset(binding)
    assert len(released) == 1
    assert app.media.transport is None


async def test_call_id_alone_cannot_take_over_a_ringing_phone(application):
    app, registry, _peer, _released = application
    registry.release_resource(app.session.call_id, f"automation:{app.session.call_id}", value=app)
    registry.transition(app.session.call_id, owner="ha_softphone", state="ringing")
    try:
        with pytest.raises(ServiceValidationError, match="no call handled"):
            await module.resolve_application_action(service(app))
        assert app.session.owner == "ha_softphone"
        assert registry.resource_for(app.session.call_id, "automation") is None
    finally:
        await registry.request_termination(app.session.call_id, TerminationIntent("test_done"))


@pytest.mark.parametrize("answered", [False, True])
async def test_application_hangup_uses_its_controller_without_a_browser_phone(application, monkeypatch, answered):
    from custom_components import voip_stack
    from custom_components.voip_stack import endpoint_lifecycle

    app, registry, _peer, _released = application
    monkeypatch.setattr(endpoint_lifecycle, "call_registry", lambda _h: registry)
    controller = Context()
    app.claim(service(app, context=controller))
    if answered:
        await app.answer()
    call = ServiceCall(app.hass, "voip_stack", "hangup", {
        "call_id": app.session.call_id, "expected_generation": app.session.generation,
    }, context=controller)
    await voip_stack._handle_sip_hangup_service(call)
    assert not app.session.live
    assert app.session.termination_intent.reason == "local_hangup"


@pytest.mark.parametrize("changed", ["answered", "ended", "owner", "generation"])
async def test_phone_takeover_rechecks_call_after_address_resolution(application, monkeypatch, changed):
    from custom_components.voip_stack import automation_context, peer_snapshot
    from custom_components.voip_stack.endpoint_session import CallToken

    app, registry, _peer, _released = application
    registry.release_resource(app.session.call_id, f"automation:{app.session.call_id}", value=app)
    registry.transition(app.session.call_id, owner="ha_softphone", state="ringing")
    token = app.session.token
    if changed == "generation":
        token = CallToken(token.call_id, token.generation + 1)
    binding = module.current_execution.set(automation_context.CallExecution(token, {"automation_control": "routable"}))

    async def resolve_host(_hass):
        if changed == "answered":
            registry.transition(app.session.call_id, state="in_call")
        elif changed == "owner":
            registry.transition(app.session.call_id, owner="router")
        elif changed == "ended":
            await registry.request_termination(app.session.call_id, TerminationIntent("remote_hangup"))
        return "127.0.0.1"

    monkeypatch.setattr(peer_snapshot, "async_advertise_host", resolve_host)
    try:
        with pytest.raises(ServiceValidationError, match="no longer an unanswered"):
            await module.resolve_application_action(service(app))
        assert registry.resource_for(app.session.call_id, "automation") is None
        assert app.session.owner != "automation"
    finally:
        module.current_execution.reset(binding)
        barrier = registry.request_termination(app.session.call_id, TerminationIntent("test_done"))
        if barrier is not None:
            await barrier


async def test_native_automation_can_hangup_implicitly_after_speaking(application, monkeypatch):
    from custom_components import voip_stack
    from custom_components.voip_stack import automation_context, endpoint_lifecycle

    app, registry, _peer, _released = application
    monkeypatch.setattr(endpoint_lifecycle, "call_registry", lambda _h: registry)
    monkeypatch.setattr(automation_context, "call_registry", lambda _h: registry)
    execution = automation_context.CallExecution(app.session.token, {"automation_control": "routable"})
    binding = module.current_execution.set(execution)
    try:
        app.claim(service(app))
        await app.answer()
        call = automation_context.bind_call_action(ServiceCall(app.hass, "voip_stack", "hangup", {}))
        await voip_stack._handle_sip_hangup_service(call)
        assert not app.session.live
        assert app.media.transport is None
    finally:
        module.current_execution.reset(binding)
        barrier = registry.request_termination(app.session.call_id, TerminationIntent("test_done"))
        if barrier is not None:
            await barrier


async def test_browser_announcement_waits_for_current_connection(application, monkeypatch):
    app, registry, _peer, _released = application
    app.playback = BrowserPlayback()
    old_connection, current_connection = object(), object()
    app.playback.attach(old_connection)
    app.playback.attach(current_connection)
    synthesized = asyncio.Event()

    class Stream:
        def async_set_message(self, _message):
            synthesized.set()

        async def async_stream_result(self):
            yield b"\x80\x01" * 320

    from homeassistant.components import tts

    monkeypatch.setattr(tts, "async_create_stream", lambda *_args: Stream())
    task = asyncio.create_task(app.say(service(app)))
    try:
        async with asyncio.timeout(2):
            while not app.answered:
                await asyncio.sleep(0)
        app.playback.ready(old_connection)
        await asyncio.sleep(0)
        assert not synthesized.is_set()
        app.playback.ready(current_connection)
        app.playback.detach(old_connection)
        async with asyncio.timeout(2):
            await task
        assert synthesized.is_set()
        assert local_application.send_final_response.call_count == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await registry.request_termination(app.session.call_id, TerminationIntent("test_done"))


@pytest.mark.parametrize("caller_budget", [30.0, 120.0])
async def test_browser_readiness_after_five_seconds_respects_caller_budget(monkeypatch, caller_budget):
    playback = BrowserPlayback()
    previous, current = object(), object()
    playback.attach(previous)
    playback.attach(current)
    loop = asyncio.get_running_loop()
    actual_time = loop.time
    offset = 0.0

    async def wait_for_owner():
        async with asyncio.timeout(caller_budget):
            await playback.wait_ready()

    task = None
    try:
        with monkeypatch.context() as clock:
            clock.setattr(loop, "time", lambda: actual_time() + offset)
            task = asyncio.create_task(wait_for_owner())
            await asyncio.sleep(0)
            offset = 6.0
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not task.done()
            playback.ready(previous)
            await asyncio.sleep(0)
            assert not task.done()
            playback.ready(current)
            await task
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await playback.close("test_done")


async def test_closed_browser_playback_wakes_waiter():
    playback = BrowserPlayback()
    waiter = asyncio.create_task(playback.wait_ready())
    await asyncio.sleep(0)
    await playback.close("remote_hangup")
    with pytest.raises(ConnectionError, match="ended"):
        await waiter
    with pytest.raises(ConnectionError, match="ended"):
        playback.attach(object())


async def test_wait_for_digits_ignores_other_call_and_generation(application):
    from custom_components.voip_stack.websocket_api import SIP_DTMF_EVENT

    app, registry, _peer, _released = application
    task = asyncio.create_task(app.wait_for_dtmf(service(app, max_digits=2, timeout=1)))
    try:
        async with asyncio.timeout(2):
            while not app.answered:
                await asyncio.sleep(0)
        await asyncio.sleep(0)
        payload = {"call_id": app.session.call_id, "generation": app.session.generation, "source_leg": "caller"}
        for event in (
            {**payload, "call_id": "another", "digit": "9"},
            {**payload, "generation": app.session.generation + 1, "digit": "9"},
            {**payload, "digit": "4"},
            {**payload, "digit": "2"},
        ):
            app.hass.bus.async_fire(SIP_DTMF_EVENT, event)
        assert await task == {"status": "received", "digits": "42"}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await registry.request_termination(app.session.call_id, TerminationIntent("test_done"))


@pytest.mark.parametrize("finish", ["gap", "total", "hangup"])
async def test_dtmf_inter_digit_deadline_resets_and_releases(application, monkeypatch, finish):
    from custom_components.voip_stack.const import SIP_DTMF_EVENT

    app, registry, _peer, _released = application
    handles = []
    original_call_later = app.hass.loop.call_later

    def track_timer(delay, callback, *args, **kwargs):
        handle = original_call_later(delay, callback, *args, **kwargs)
        if delay == 0.2:
            handles.append(handle)
        return handle

    monkeypatch.setattr(app.hass.loop, "call_later", track_timer)
    initial_listeners = app.hass.bus.async_listeners().get(SIP_DTMF_EVENT, 0)
    total = 0.3 if finish == "total" else 2
    task = asyncio.create_task(app.wait_for_dtmf(service(
        app, max_digits=8, timeout=total, inter_digit_timeout=0.2)))
    try:
        async with asyncio.timeout(2):
            while not app.answered:
                await asyncio.sleep(0)
        await asyncio.sleep(0)
        payload = {"call_id": app.session.call_id,
                   "generation": app.session.generation, "source_leg": "caller"}
        app.hass.bus.async_fire(SIP_DTMF_EVENT, {**payload, "digit": "4"})
        await asyncio.sleep(0.12)
        app.hass.bus.async_fire(SIP_DTMF_EVENT, {**payload, "digit": "2"})
        await asyncio.sleep(0.12)
        assert not task.done(), "The second digit must renew the inter-digit deadline"
        if finish == "hangup":
            await registry.request_termination(app.session.call_id, TerminationIntent("test_done"))
            await asyncio.gather(task, return_exceptions=True)
        else:
            # A continuing stream cannot extend the original total limit.
            if finish == "total":
                app.hass.bus.async_fire(SIP_DTMF_EVENT, {**payload, "digit": "3"})
            async with asyncio.timeout(0.25):
                result = await task
            assert result == {"status": "timeout", "digits": "423" if finish == "total" else "42"}
        assert len(handles) >= 2
        assert all(handle.cancelled() for handle in handles)
        assert app.hass.bus.async_listeners().get(SIP_DTMF_EVENT, 0) == initial_listeners
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if (remaining := registry.get_session(app.session.call_id)) is not None and remaining.live:
            await registry.request_termination(app.session.call_id, TerminationIntent("test_done"))


@pytest.mark.parametrize("digits", ["1", "111"])
async def test_ivr_selection_finishes_on_digit_gap_without_end_key(application, digits):
    from custom_components.voip_stack.const import SIP_DTMF_EVENT

    app, registry, _peer, _released = application
    task = asyncio.create_task(app.wait_for_dtmf(service(
        app, max_digits=32, timeout=2, inter_digit_timeout=0.1, terminator="")))
    try:
        async with asyncio.timeout(2):
            while not app.answered:
                await asyncio.sleep(0)
        await asyncio.sleep(0)
        for digit in digits:
            app.hass.bus.async_fire(SIP_DTMF_EVENT, {
                "call_id": app.session.call_id,
                "generation": app.session.generation,
                "source_leg": "caller",
                "digit": digit,
            })
            await asyncio.sleep(0)
            assert not task.done(), "A menu prefix must not consume an extension"
        async with asyncio.timeout(1):
            result = await task
        assert result == {"status": "timeout", "digits": digits}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await registry.request_termination(app.session.call_id, TerminationIntent("test_done"))


@pytest.mark.parametrize("failure_stage", ["source", "readiness"])
async def test_browser_forward_preparation_failure_preserves_original_playback(application, monkeypatch, failure_stage):
    from types import SimpleNamespace
    from custom_components.voip_stack import browser_application_handoff as handoff
    from custom_components.voip_stack.endpoint_session import CleanupStage
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint

    app, registry, _peer, _released = application
    await app.answer()
    playback = BrowserPlayback()
    key = f"browser_playback:{app.session.call_id}"
    registry.own_resource(app.session.call_id, key, playback, playback.close, stage=CleanupStage.MEDIA)
    target = PhoneEndpoint(endpoint_id="target-browser", name="Target", device_id="target-device", kind="browser")
    runtime = SimpleNamespace(hass=app.hass, local_ip="127.0.0.1")
    monkeypatch.setattr(handoff, "require_runtime_data", lambda _h: SimpleNamespace(
        media=SimpleNamespace(owners_for=lambda _kind: {})))
    monkeypatch.setattr(handoff, "publish_phone_projection", Mock())
    if failure_stage == "source":
        monkeypatch.setattr(handoff.LocalMediaBridge, "prepare", AsyncMock(side_effect=RuntimeError("source media witness")))
    else:
        # Use the actual reserved UDP socket path before failing admission.
        monkeypatch.setattr(BrowserPlayback, "wait_ready", AsyncMock(side_effect=RuntimeError("readiness media witness")))
    try:
        with pytest.raises(RuntimeError, match=f"{failure_stage} media witness"):
            await handoff.forward_application_to_browser(runtime, registry, app.session, app.invite,
                target, app, accepted_decision={"action": "answer_ha", "endpoint_id": target.endpoint_id})
        assert registry.resource_for(app.session.call_id, "browser_playback") is playback
        assert sum(resource.name == key for resource in app.session.resources) == 1
        assert app.media is not None and not app.media.closed.is_set()
        assert app.session.live
    finally:
        await registry.request_termination(app.session.call_id, TerminationIntent("test_done"))


async def test_local_announcement_decodes_negotiated_rtp_dtmf_once(application):
    app, registry, peer, _released = application
    from custom_components.voip_stack.core.media import negotiator
    offer = app.invite.remote_sdp.replace(b"RTP/AVP 96", b"RTP/AVP 96 100") + b"a=rtpmap:100 telephone-event/8000\r\na=fmtp:100 0-15\r\n"
    events = negotiator.negotiate_dtmf(offer,
        send_clock_rate=app.invite.send_format.rtp_clock_rate,
        receive_clock_rate=app.invite.recv_format.rtp_clock_rate)
    app.invite = replace(app.invite, remote_sdp=offer, dtmf_direction=events)
    try:
        await app.answer()
        received = []
        app.media.on_dtmf = lambda side, digit, transport: received.append((side, digit, transport))
        packet = rtp.build_packet(rtp.RtpPacket(payload_type=100, sequence=1, timestamp=1000, ssrc=123, payload=b"\x01\x80\x00\xa0"))
        app.media.handle_rtp(packet, peer.getsockname())
        app.media.handle_rtp(packet, peer.getsockname())
        assert received == [("left", "1", "rtp_event")]
        assert app.media.counters["drop_payload_type"] == 0
    finally:
        await registry.request_termination(app.session.call_id, TerminationIntent("test_done"))


async def test_competing_automation_and_stale_generation_are_rejected(application):
    app, registry, _peer, _released = application
    first = service(app)
    app.claim(first)
    app.claim(first)
    with pytest.raises(ServiceValidationError, match="Another automation"):
        app.claim(service(app))
    with pytest.raises(ServiceValidationError, match="ended or changed"):
        app.claim(service(app, expected_generation=app.session.generation + 1))
    await registry.request_termination(
        app.session.call_id, TerminationIntent("test_done")
    )


async def test_hangup_cancels_blocked_tts_provider(application, monkeypatch):
    app, registry, _peer, released = application
    entered = asyncio.Event()

    class Stream:
        def async_set_message(self, _message):
            pass

        async def async_stream_result(self):
            entered.set()
            await asyncio.Event().wait()
            yield b""

    from homeassistant.components import tts

    monkeypatch.setattr(tts, "async_create_stream", lambda *_args: Stream())
    task = asyncio.create_task(app.say(service(app)))
    await asyncio.wait_for(entered.wait(), 2)
    await registry.request_termination(
        app.session.call_id, TerminationIntent("remote_hangup")
    )
    result = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
    assert len(released) == 1
    assert app.deadline is None


async def test_provider_error_allows_same_automation_to_forward(
    application, monkeypatch
):
    app, registry, _peer, _released = application
    from homeassistant.components import tts

    monkeypatch.setattr(
        tts,
        "async_create_stream",
        Mock(side_effect=RuntimeError("provider unavailable")),
    )
    call = service(app)
    try:
        with pytest.raises(RuntimeError, match="provider unavailable"):
            await app.say(call)
        registry.forward_call = AsyncMock()
        await app.forward("Assist")
        registry.forward_call.assert_awaited_once_with(
            call_id=app.session.call_id, destination="Assist", on_failure="resume"
        )
    finally:
        await registry.request_termination(
            app.session.call_id, TerminationIntent("test_done")
        )


async def test_confirmed_call_handoff_changes_owner_without_second_answer(application):
    from custom_components.voip_stack.inbound_answer import async_commit_runtime_answer

    app, registry, _peer, released = application
    try:
        await app.answer()
        assert app.session.answer_committed
        sender = Mock(return_value=True)
        result = await async_commit_runtime_answer(
            registry,
            app.session.call_id,
            "",
            send_final_response=sender,
            response_context=app.hass,
            owner="assist",
            callee="Assistant",
            route_kind="assist",
            response_already_sent=True,
        )
        assert result.committed and not result.response_sent
        assert app.session.owner == "assist"
        sender.assert_not_called()
    finally:
        await registry.request_termination(
            app.session.call_id, TerminationIntent("test_done")
        )
    assert len(released) == 1


async def test_forward_to_another_automation_reuses_media_and_releases_old_controller(application, monkeypatch):
    from pytest_homeassistant.common import MockConfigEntry
    from custom_components.voip_stack import call_forwarder
    from custom_components.voip_stack.endpoint_registry import EndpointRegistry
    from custom_components.voip_stack.endpoint_routing import EndpointRouteResolver
    from custom_components.voip_stack.runtime_data import VoipStackRuntime

    app, registry, peer, released = application
    entry = MockConfigEntry(domain="voip_stack", data={})
    entry.add_to_hass(app.hass)
    entry.runtime_data = VoipStackRuntime(
        transport_config={}, assist_config={}, trunk_config={},
        endpoints=EndpointRegistry(), phones=None, sip=registry,
    )
    next_contact = RosterEntry("Next", extension="777", metadata={"virtual_endpoint": "automation"})
    requested = asyncio.Event()
    monkeypatch.setattr(module, "publish_bridge_projection", lambda *_args, **kwargs: (
        requested.set() if kwargs.get("event_type") == "automation_requested" else None
    ))
    monkeypatch.setattr(call_forwarder, "_call_registry", lambda _h: registry)
    monkeypatch.setattr(call_forwarder, "_async_build_peer_snapshot", AsyncMock(return_value=[]))
    monkeypatch.setattr(call_forwarder, "_roster_from_peers", lambda *_args: [next_contact])
    forbidden = Mock(side_effect=AssertionError("A local application must not dial a new SIP leg"))
    runtime = call_forwarder.ForwardRuntime(
        hass=app.hass, config={"sip_port": 5060}, local_ip="127.0.0.1",
        route_resolver=EndpointRouteResolver(app.hass, "127.0.0.1", 5060),
        browser_leg_for_member=forbidden, defer_invite_to_softphone=forbidden,
        prepare_outbound_leg=forbidden, publish_pending_ringing=forbidden,
        start_local_assist_bridge=forbidden,
    )
    try:
        await app.answer()
        original_media, original_transport = app.media, app.media.transport
        app.controller = "previous-execution"
        await call_forwarder.async_forward_existing_call(
            runtime, call_id=app.session.call_id, destination="Next",
        )
        task = registry.task_for(app.session.call_id, "forward")
        if task is not None:
            await task
        # Audio workers intentionally stay alive for the conversation. Wait
        # for this handoff's event rather than every task in Home Assistant.
        await asyncio.wait_for(requested.wait(), 2)
        assert registry.resource_for(app.session.call_id, "automation") is app
        assert app.session.owner == "automation"
        assert app.session.callee == "Next"
        assert app.controller == ""
        assert app.media is original_media
        assert app.media.transport is original_transport
        assert local_application.send_final_response.call_count == 1

        pcm = b"\x11\x00" * 320

        async def samples():
            yield pcm

        await app.media.play_pcm_stream(samples())
        async with asyncio.timeout(2):
            while True:
                packet = rtp.parse_packet(await app.hass.loop.sock_recv(peer, 2048))
                if any(packet.payload):
                    assert packet.payload == b"\x00\x11" * 320
                    break
    finally:
        await registry.request_termination(app.session.call_id, TerminationIntent("test_done"))
    assert len(released) == 1


async def test_inactivity_without_fallback_terminates_call(application):
    app, registry, _peer, _released = application
    await app.expire()
    await app.session.terminate(TerminationIntent("test_done"))
    assert not registry.is_generation_current(
        app.session.call_id, app.session.generation
    )


async def test_invalid_fallback_does_not_rearm_forever(application):
    from dataclasses import replace

    app, registry, _peer, _released = application
    app.contact = replace(
        app.contact,
        metadata={**app.contact.metadata, "fallback_destination": "Missing"},
    )
    registry.forward_call = AsyncMock(
        side_effect=ServiceValidationError("unknown target")
    )
    await app.expire()
    await app.session.terminate(TerminationIntent("test_done"))
    assert app.deadline is None
    assert app.session.termination_intent.reason == "fallback_failed"


async def test_named_contact_without_extension_can_be_selected():
    contact = RosterEntry("Reception", metadata={"virtual_endpoint": "automation"})
    assert (
        resolve_ha_router("Reception", [contact], trunk_ready=True).action
        is RouteAction.AUTOMATION
    )
    assert (
        resolve_ha_router("666", [contact], trunk_ready=True).action
        is RouteAction.TRUNK
    )


@pytest.mark.parametrize(
    "metadata",
    [
        {"virtual_endpoint": "automation", "automation_timeout": 0},
        {"virtual_endpoint": "automation", "automation_timeout": 301},
    ],
)
def test_imported_automation_contacts_reject_unbounded_timeouts(metadata):
    from custom_components.voip_stack.roster import RosterError, parse_roster_json

    with pytest.raises(RosterError):
        parse_roster_json([{"name": "Reception", "metadata": metadata}])


@pytest.mark.parametrize("owner", ["automation", "assist", "bridge"])
async def test_browser_result_does_not_reclaim_local_application(application, owner):
    from custom_components.voip_stack.outbound_lifecycle import (
        observe_outbound_call_result,
    )

    app, registry, _peer, _released = application
    registry.transition(
        app.session.call_id, state="in_call", owner=owner, callee="Application"
    )
    result = observe_outbound_call_result(
        registry,
        app.session.call_id,
        state="in_call",
        caller="Browser",
        callee="Old dialed name",
        route_kind="direct",
        endpoint_id="caller-browser",
    )
    assert result is app.session
    assert result.owner == owner
    assert result.callee == "Application"
    assert result.legs[app.session.call_id].endpoint_id == "caller-browser"
    await registry.request_termination(
        app.session.call_id, TerminationIntent("test_done")
    )


async def test_tcp_reset_releases_listener_connection_without_unhandled_error(
    socket_enabled,
):
    import struct
    from custom_components.voip_stack.sip_listener import SipTcpServer

    server = SipTcpServer(
        host="127.0.0.1",
        port=0,
        local_ip="127.0.0.1",
        local_rtp_port=40000,
        supported_formats=[sdp.RtpPcmFormat(96, "L16", 16000, 1, 20).audio_format],
        on_invite=AsyncMock(),
    )
    assert await server.start()
    try:
        _reader, writer = await asyncio.open_connection(
            "127.0.0.1", server.server.sockets[0].getsockname()[1]
        )
        async with asyncio.timeout(2):
            while not server._client_tasks:
                await asyncio.sleep(0)
        tasks = list(server._client_tasks)
        writer.get_extra_info("socket").setsockopt(
            socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
        )
        writer.transport.abort()
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), 2
        )
        assert results == [None]
        assert not server._client_tasks
        assert not server._writers
    finally:
        await server.stop()


async def test_tts_confirms_existing_trunk_early_media(application):
    app, registry, _peer, _released = application
    reservation = local_application.RtpPortReservation.allocate(app.hass)
    registry.attach_media(
        app.session.call_id,
        {
            "rtp_reservation": reservation,
            "local_rtp_port": reservation.ports[0],
            "final_response_sent": False,
        },
        provisional=True,
    )
    try:
        await app.answer()
        assert registry.resource_for(app.session.call_id, "preanswered")[
            "final_response_sent"
        ]
        assert app.session.answer_committed
        assert local_application.send_final_response.call_count == 1
    finally:
        await registry.request_termination(
            app.session.call_id, TerminationIntent("test_done")
        )


async def test_resuming_application_does_not_publish_another_received_call(application, monkeypatch):
    app, registry, _peer, _released = application
    events = []
    monkeypatch.setattr(module, "publish_bridge_projection", lambda _hass, _session, **fields: events.append(fields))
    try:
        await app.resume()
        assert len(events) == 1
        assert events[0]["event_type"] == "state_changed"
    finally:
        await registry.request_termination(app.session.call_id, TerminationIntent("test_done"))


async def test_announcement_handoff_keeps_the_same_rtp_sender_state(application):
    app, registry, _peer, _released = application
    try:
        await app.answer()
        source = app.media.rtp_source
        provisional = registry.resource_for(app.session.call_id, "preanswered")
        assert provisional["audio_rtp_source"] is source
        await app.release_media_for_forward()
        assert app.media is None
        assert provisional["audio_rtp_source"] is source
    finally:
        await registry.request_termination(app.session.call_id, TerminationIntent("test_done"))


async def test_wait_unanswered_keeps_ringing_without_answer_or_media(application):
    app, registry, peer, released = application
    app.contact.metadata['automation_timeout'] = 0.01
    app.arm_deadline()
    try:
        await app.wait_unanswered(service(app, duration=0.04))
        assert app.session.live and app.session.state == 'ringing'
        assert not app.answered and app.media is None
        local_application.send_final_response.assert_not_called()
        with pytest.raises(BlockingIOError):
            peer.recv(2048)
        assert released == []
        assert app.operation is None and not app.lock.locked()
    finally:
        await registry.request_termination(app.session.call_id, TerminationIntent('test_done'))
    assert app.deadline is None


async def test_caller_cancel_interrupts_unanswered_wait(application):
    app, registry, _peer, released = application
    task = asyncio.create_task(app.wait_unanswered(service(app, duration=60)))
    async with asyncio.timeout(2):
        while app.operation is None:
            await asyncio.sleep(0)
    await registry.request_termination(app.session.call_id, TerminationIntent('remote_cancelled'))
    result, = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(result, asyncio.CancelledError)
    assert app.operation is None and app.deadline is None
    assert not app.answered and app.media is None and released == []
    local_application.send_final_response.assert_not_called()


async def test_unanswered_wait_rejects_an_already_answered_call(application):
    app, registry, _peer, _released = application
    app.answered = True
    try:
        with pytest.raises(ServiceValidationError, match='unanswered'):
            await app.wait_unanswered(service(app, duration=1))
        assert not app.controller
        assert app.operation is None
    finally:
        await registry.request_termination(app.session.call_id, TerminationIntent('test_done'))


async def test_wait_then_forward_does_not_answer_early(application):
    app, registry, _peer, _released = application
    registry.forward_call = AsyncMock()
    try:
        await app.wait_unanswered(service(app, duration=0.01))
        await app.forward('Reception')
        registry.forward_call.assert_awaited_once_with(
            call_id=app.session.call_id, destination='Reception', on_failure='resume',
        )
        assert not app.answered and app.media is None
        local_application.send_final_response.assert_not_called()
    finally:
        await registry.request_termination(app.session.call_id, TerminationIntent('test_done'))


async def test_native_ha_script_waits_without_call_id_then_finishes_unanswered(application, monkeypatch):
    from homeassistant.helpers.script import Script
    from homeassistant.helpers.trigger import TriggerConfig
    from custom_components.voip_stack import automation_context, trigger, services

    app, registry, _peer, _released = application
    monkeypatch.setattr(automation_context, 'call_registry', lambda _: registry)
    monkeypatch.setattr(trigger, 'call_registry', lambda _: registry)
    await services.async_register_services(app.hass, {'wait_unanswered': module.async_wait_unanswered})
    script = Script(app.hass, [{'action':'voip_stack.wait_unanswered','data':{'duration':1}}], 'Unanswered test', 'automation')
    finished = asyncio.Event()
    tasks = []
    def runner(payload, description, context):
        task = app.hass.async_create_task(script.async_run({'trigger':payload}, context))
        tasks.append(task)
        task.add_done_callback(lambda _: finished.set())
        return task
    subject = trigger.VoipCallTrigger(app.hass, TriggerConfig('voip_stack.call_received',options={'destination':'Welcome'}))
    detach = await subject.async_attach_runner(runner)
    terminate = registry.request_termination
    captured = []
    def capture(call_id, intent, **kwargs):
        captured.append(intent)
        return terminate(call_id, intent, **kwargs)
    monkeypatch.setattr(registry, 'request_termination', capture)
    try:
        app.hass.bus.async_fire(trigger.CALL_EVENT, {
            'event_type':'automation_requested','call_id':app.session.call_id,
            'generation':app.session.generation,'callee':'Welcome',
        })
        await asyncio.wait_for(finished.wait(),3)
        await app.hass.async_block_till_done()
        assert len(captured)==1
        assert captured[0] == TerminationIntent.final_response('timeout',480)
        assert app.controller and not app.answered and app.media is None
        local_application.send_final_response.assert_not_called()
    finally:
        detach()
        await terminate(app.session.call_id, TerminationIntent('test_done'))
