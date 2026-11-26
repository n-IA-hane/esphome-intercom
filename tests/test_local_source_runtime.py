"""Browser source handoff uses a real, supported RTP contract."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import asyncio
import socket
import struct

import pytest

from custom_components.voip_stack import local_softphone_runtime as local
from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("send,receive,direction", [
    (False, False, "inactive"), (False, True, "recvonly"),
    (True, False, "sendonly"), (True, True, "sendrecv"),
])
def test_pending_browser_source_preserves_pcm_rate_and_video_intent(monkeypatch, send, receive, direction):
    from custom_components.voip_stack.core import sdp

    phones = {
        name: PhoneEndpoint(endpoint_id=name, name=name, kind=EndpointKind.BROWSER)
        for name in ("caller", "callee")
    }
    media = {"local_bridge": True}
    registry = SimpleNamespace(
        get_session=lambda _id: SimpleNamespace(
            token=object(), call_id="local-call",
            metadata={"inbound_from_uri": "sip:previous-party@remote-pbx.example"},
        ),
        resource_for=lambda *_args: media,
    )
    monkeypatch.setattr(local, "call_registry", lambda _hass: registry)
    monkeypatch.setattr(local, "local_softphone_bridge", lambda _hass: object())
    monkeypatch.setattr(local, "_endpoint", lambda _hass, name: phones[name])
    source = local.PendingLocalSource(
        object(),
        SimpleNamespace(call_id="local-call", caller_endpoint_id="caller",
                        callee_endpoint_id="callee", caller_media_owner_id="caller-owner",
                        video_requested=send or receive, video_enabled=send or receive,
                        caller_video_send=send, caller_video_receive=receive,
                        callee_state=local.LocalCallState.RINGING),
        "127.0.0.1", 15060, video_peer_format=sdp.RtpVideoFormat(payload_type=103, encoding="VP8"),
    )
    assert source.invite.call_id == "local-call"
    # This adapter retains the actual browser, not a previous trunk participant.
    assert str(source.invite.caller_uri) == "sip:caller@127.0.0.1:15060"
    assert not source.invite.received_via_trunk
    for format in (source.invite.send_format, source.invite.recv_format):
        assert format.audio_format.sample_rate == 48000
        assert format.audio_format.channels == 1
        assert format.audio_format.nominal_frame_bytes <= 1200
    assert source.caller_video_send is send
    assert source.caller_video_receive is receive
    if send or receive:
        assert source.invite.video_format.direction == direction
    else:
        assert source.invite.video_format is None
    assert media == {"local_bridge": True}


@pytest.mark.parametrize("hangup_before_audio_ready", [False, True])
async def test_native_automation_takes_browser_caller_without_a_second_sip_call(
    hass, monkeypatch, socket_enabled, hangup_before_audio_ready,
):
    from homeassistant.core import Context, ServiceCall
    from pytest_homeassistant.common import MockConfigEntry
    from custom_components.voip_stack import automation_call, peer_snapshot, local_application
    from custom_components.voip_stack.automation_context import CallExecution, current_execution
    from custom_components.voip_stack.config import entry_transport_config
    from custom_components.voip_stack.duration_config import initialize_duration_config
    from custom_components.voip_stack.core import rtp
    from custom_components.voip_stack.endpoint_registry import EndpointRegistry
    from custom_components.voip_stack.endpoint_session import TerminationIntent
    from custom_components.voip_stack.media_ports import RtpPortReservation
    from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
    from custom_components.voip_stack.phone_control import CallControlRequest, PhoneAdapterRegistry, PhoneOperation
    from custom_components.voip_stack.runtime_data import VoipStackRuntime

    endpoints = EndpointRegistry()
    for name in ("caller", "callee"):
        endpoints.register(PhoneEndpoint(
            endpoint_id=name, name=name, kind=EndpointKind.BROWSER,
            device_id=f"device-{name}", availability="available",
            capabilities=frozenset({"audio", "dtmf"}),
        ))
    calls = SipEndpointRuntime(allow_dark_sessions=True)
    calls.activate()
    entry = MockConfigEntry(domain="voip_stack", data=initialize_duration_config({"sip_port": 5060, "advertise_host": "127.0.0.1"}))
    entry.add_to_hass(hass)
    entry.runtime_data = VoipStackRuntime(
        transport_config=entry_transport_config(entry),
        assist_config={}, trunk_config={}, endpoints=endpoints,
        phones=PhoneAdapterRegistry(hass, endpoints), sip=calls,
    )
    bridge = local.async_setup_local_softphone_bridge(hass)
    snapshot = local.start_local_softphone_call(
        hass, "caller", "callee", context=Context(), caller_owner_id="browser-caller-owner",
    )
    session = calls.get_session(snapshot.call_id)
    execution = CallExecution(session.token, {"automation_control": "routable"})
    binding = current_execution.set(execution)
    monkeypatch.setattr(peer_snapshot, "async_advertise_host", AsyncMock(return_value="127.0.0.1"))
    sender = Mock(side_effect=AssertionError("A local browser has no SIP answer to send"))
    monkeypatch.setattr(local_application, "send_final_response", sender)
    # The peer is the caller's existing RTP adapter, not a second browser phone.
    peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer.bind(("127.0.0.1", 0))
    peer.setblocking(False)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        source_port = probe.getsockname()[1]
    reservation = RtpPortReservation(hass, (source_port, peer.getsockname()[1]))
    monkeypatch.setattr(RtpPortReservation, "allocate", lambda _h: reservation)
    announcement = None
    try:
        call = ServiceCall(hass, "voip_stack", "tts_say", {
            "call_id": session.call_id, "expected_generation": session.generation,
        })
        app, _ = await automation_call.resolve_application_action(call)
        assert app.local_source is not None
        assert bridge.get_call(session.call_id) is None
        assert endpoints.require("callee").active_call_id == ""
        assert endpoints.require("caller").active_call_id == session.call_id
        await app.answer()
        assert app.session.state == "in_call"
        assert app.session.metadata["source_endpoint_id"] == "caller"
        sender.assert_not_called()

        synthesized = asyncio.Event()

        async def samples():
            synthesized.set()
            yield b"\x40\x00" * 960

        connection = object()
        app.playback.attach(connection)
        waiting_for_browser = asyncio.Event()
        wait_ready = app.playback.wait_ready

        async def observe_wait_ready():
            waiting_for_browser.set()
            await wait_ready()

        monkeypatch.setattr(app.playback, "wait_ready", observe_wait_ready)
        announcement = asyncio.create_task(app._play_announcement(call, samples))
        async with asyncio.timeout(2):
            await waiting_for_browser.wait()
        assert not synthesized.is_set()
        if not hangup_before_audio_ready:
            app.playback.ready(connection)
            async with asyncio.timeout(2):
                await announcement
                while True:
                    packet = rtp.parse_packet(await hass.loop.sock_recv(peer, 4096))
                    decoded = struct.unpack(f"!{len(packet.payload) // 2}h", packet.payload)
                    if any(decoded):
                        assert max(decoded) >= 60
                        break
        assert len(calls.sessions) == 1
        # A card command is independent of the automation's execution context.
        current_execution.reset(binding)
        binding = None
        hangup = ServiceCall(hass, "voip_stack", "hangup", {
            "device_id": "device-caller", "call_id": session.call_id,
        })
        await entry.runtime_data.phones.control(
            hangup, PhoneOperation.HANGUP, CallControlRequest(call_id=session.call_id),
        )
        assert not session.live
        assert app.media.transport is None
        assert endpoints.require("caller").active_call_id == ""
        assert endpoints.require("callee").active_call_id == ""
        if hangup_before_audio_ready:
            with pytest.raises((asyncio.CancelledError, ConnectionError)):
                await announcement
            assert not synthesized.is_set()
    finally:
        if binding is not None:
            current_execution.reset(binding)
        if announcement is not None:
            announcement.cancel()
            await asyncio.gather(announcement, return_exceptions=True)
        barrier = calls.request_termination(session.call_id, TerminationIntent("test_done"))
        if barrier is not None:
            await barrier
        local.async_shutdown_local_softphone_bridge(hass)
        peer.close()
