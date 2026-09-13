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
from custom_components.voip_stack import media_ports
from custom_components.voip_stack.core import rtp, sdp, sip
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
from custom_components.voip_stack.roster import RosterEntry
from custom_components.voip_stack.router import RouteAction, resolve_ha_router
from custom_components.voip_stack.sip_listener import SipInvite

pytestmark = pytest.mark.ha


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
    monkeypatch.setattr(module.RtpPortReservation, "allocate", lambda _h: reservation)
    monkeypatch.setattr(module, "call_registry", lambda _h: registry)
    monkeypatch.setattr(module, "publish_bridge_projection", Mock())
    monkeypatch.setattr(module, "send_final_response", Mock(return_value=True))
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


async def test_announcement_answers_once_and_sends_complete_pcm(
    application, monkeypatch
):
    app, registry, peer, released = application
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
        assert module.send_final_response.call_count == 1
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
    assert len(released) == 1
    assert app.media.transport is None


async def test_browser_announcement_waits_for_current_connection(application, monkeypatch):
    app, registry, _peer, _released = application
    app.playback = module.BrowserPlayback()
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
        assert module.send_final_response.call_count == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await registry.request_termination(app.session.call_id, TerminationIntent("test_done"))


async def test_closed_browser_playback_wakes_waiter():
    playback = module.BrowserPlayback()
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


async def test_local_announcement_decodes_negotiated_rtp_dtmf_once(application):
    app, registry, peer, _released = application
    app.invite = replace(app.invite, remote_sdp=app.invite.remote_sdp.replace(b"RTP/AVP 96", b"RTP/AVP 96 100") + b"a=rtpmap:100 telephone-event/8000\r\na=fmtp:100 0-15\r\n")
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
    reservation = module.RtpPortReservation.allocate(app.hass)
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
        assert module.send_final_response.call_count == 1
    finally:
        await registry.request_termination(
            app.session.call_id, TerminationIntent("test_done")
        )
