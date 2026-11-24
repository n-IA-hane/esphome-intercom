import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.voip_stack import call_projection, endpoint_lifecycle, browser_phone_runtime
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime


pytestmark = pytest.mark.ha


async def test_publisher_delivers_one_described_forward_after_phone_projection(hass, monkeypatch):
    from custom_components.voip_stack.endpoint_registry import EndpointRegistry
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint
    from custom_components.voip_stack.runtime_data import VoipStackRuntime

    registry = SipEndpointRuntime()
    registry.activate()
    endpoints = EndpointRegistry()
    endpoints.register(PhoneEndpoint(endpoint_id="card", name="Card", kind="browser"))
    registry.bind_endpoint_registry(endpoints)
    runtime = VoipStackRuntime(transport_config={}, assist_config={}, trunk_config={}, endpoints=endpoints,
                              phones=None, sip=registry)
    monkeypatch.setattr(call_projection, "require_runtime_data", lambda _hass: runtime)
    bridge_sink, phone_sink = Mock(), Mock()
    monkeypatch.setattr(browser_phone_runtime, "_set_sip_bridge_call_state", bridge_sink)
    monkeypatch.setattr(browser_phone_runtime, "_set_ha_softphone_call_state", phone_sink)
    publisher = call_projection.CallStatePublisher(hass, registry)
    registry.adopt_component("state_publisher", publisher, closer=publisher.close)
    session = registry.upsert("described", state="connecting", owner="router", caller="Caller", callee="Target A",
                              route_kind="direct", source_endpoint_id="card")
    registry.claim_endpoint(session.call_id, "card", role="caller")
    session.update_metadata(bridge_presentation={
        "operation": session.event_context.sequence,
        "context": (session.owner, session.state, session.callee, session.route_kind),
        "details": {"peer_name": "Target A", "direction": "outgoing", "route_source": "automation",
                    "event_type": "forwarding", "last_sip_event": "ROUTE_FORWARD"},
    })
    try:
        await asyncio.sleep(0)
        assert phone_sink.call_count == 1
        assert bridge_sink.call_count == 1
        assert bridge_sink.call_args.kwargs["event_type"] == "forwarding"
        assert bridge_sink.call_args.kwargs["peer_name"] == "Target A"
        assert bridge_sink.call_args.kwargs["route_source"] == "automation"
        assert session.owner == "router" and session.state == "connecting"
        # Topology changed within this accepted operation, not another forward.
        registry.observe_leg(session.call_id, "transport", role="callee", state="calling")
        await asyncio.sleep(0)
        assert sum(call.kwargs.get("event_type") == "forwarding" for call in bridge_sink.call_args_list) == 1
        boundary = bridge_sink.call_count
        registry.transition(session.call_id, owner="router", state="remote_ringing", callee="Target B")
        await asyncio.sleep(0)
        assert all(call.kwargs.get("peer_name") != "Target A" for call in bridge_sink.call_args_list[boundary:])
        assert phone_sink.call_args.kwargs["peer_name"] == "Target B"
    finally:
        await publisher.close()
        await registry.request_termination(session.call_id, TerminationIntent("test_done"))


def _runtime(monkeypatch):
    runtime = SipEndpointRuntime()
    runtime.activate()
    endpoints = SimpleNamespace(
        get=lambda _endpoint_id: SimpleNamespace(device_id="dev-1")
    )
    monkeypatch.setattr(
        call_projection,
        "require_runtime_data",
        lambda _hass: SimpleNamespace(sip=runtime, endpoints=endpoints),
    )
    monkeypatch.setattr(call_projection, "runtime_data", call_projection.require_runtime_data)
    return runtime


@pytest.mark.asyncio
async def test_media_route_keeps_pbx_diagnostics_without_device_commands(monkeypatch):
    from custom_components.voip_stack import device_resolver, esphome_actions
    runtime = SipEndpointRuntime()
    runtime.activate()
    data = SimpleNamespace(sip=runtime, endpoints=SimpleNamespace(
        get=lambda endpoint_id: SimpleNamespace(device_id=f"device-{endpoint_id}")))
    monkeypatch.setattr(call_projection, "require_runtime_data", lambda _hass: data)
    monkeypatch.setattr(call_projection, "runtime_data", lambda _hass: data)
    devices = [{"device_id": f"device-{name}"} for name in ("p4", "ws3")]
    monkeypatch.setattr(device_resolver, "get_resolver", lambda _hass: SimpleNamespace(
        list_devices=AsyncMock(return_value=devices)))
    sink = AsyncMock()
    monkeypatch.setattr(esphome_actions, "async_call_action", sink)
    monkeypatch.setattr(esphome_actions, "has_action", lambda *_args: True)
    session = runtime.upsert("source-call", state="in_call",
                             source_endpoint_id="p4", dest_endpoint_id="ws3",
                             bridge_dest_call_id="dest-call")
    session.add_resource("relay:source-call", SimpleNamespace(media_route="ha_transcoding"),
                         lambda _reason: None)
    call_projection.publish_media_route(object(), "source-call", "direct")
    await asyncio.gather(*tuple(session.tasks))
    sink.assert_not_awaited()
    assert session.metadata["committed_media_route"] == "ha_transcoding"
    call_projection.publish_media_route(object(), "source-call", "direct")
    await asyncio.gather(*tuple(session.tasks))
    assert sink.await_count == 0
    await session.terminate(TerminationIntent("local_hangup"))


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["direct", "ha_transcoding"])
async def test_media_route_accepts_local_endpoint_resource_without_relay_route(monkeypatch, route):
    runtime = SipEndpointRuntime()
    runtime.activate()
    data = SimpleNamespace(sip=runtime, endpoints=SimpleNamespace(get=lambda _id: None))
    monkeypatch.setattr(call_projection, "runtime_data", lambda _hass: data)
    session = runtime.upsert("assist-call", state="in_call")
    # Assist endpoints share relay lifecycle ownership, not relay capabilities.
    session.add_resource("relay:assist-call", SimpleNamespace(), lambda _reason: None)
    call_projection.publish_media_route(object(), "assist-call", route)
    assert session.metadata["committed_media_route"] == route
    assert session.live and session.state == "in_call"
    session.add_resource("relay:transcoder", SimpleNamespace(media_route="ha_transcoding"),
                         lambda _reason: None)
    call_projection.publish_media_route(object(), "assist-call", "direct")
    assert session.metadata["committed_media_route"] == "ha_transcoding"
    await session.terminate(TerminationIntent("local_hangup"))


@pytest.mark.asyncio
async def test_media_route_cannot_outlive_its_call_generation(monkeypatch):
    from custom_components.voip_stack import esphome_actions
    runtime = _runtime(monkeypatch)
    sink = AsyncMock()
    monkeypatch.setattr(esphome_actions, "async_call_action", sink)
    session = runtime.upsert("source-call", state="in_call", source_endpoint_id="p4")
    call_projection.publish_media_route(object(), session.call_id, "ha_transcoding")
    assert session.metadata["committed_media_route"] == "ha_transcoding"
    await session.terminate(TerminationIntent("local_hangup"))
    call_projection.publish_media_route(object(), session.call_id, "direct")
    assert session.metadata["committed_media_route"] == "ha_transcoding"
    sink.assert_not_awaited()
    assert not session.tasks


@pytest.mark.asyncio
async def test_media_route_queued_notification_cannot_outlive_relay_owner(monkeypatch):
    from custom_components.voip_stack import esphome_actions
    from custom_components.voip_stack.core.sdp import RtpPcmFormat
    from custom_components.voip_stack.sip_rtp_bridge import RtpPeer, SipRtpRelay

    runtime = _runtime(monkeypatch)
    sink = AsyncMock()
    monkeypatch.setattr(esphome_actions, "async_call_action", sink)
    fmt = RtpPcmFormat(8, "PCMA", 8000, 1, 20)
    relay = SipRtpRelay(
        left=RtpPeer("192.0.2.10", 40000, fmt.payload_type, fmt.audio_format,
                     rtp_format=fmt, send_rtp_format=fmt),
        right=RtpPeer("192.0.2.20", 41000, fmt.payload_type, fmt.audio_format,
                      rtp_format=fmt, send_rtp_format=fmt),
        left_port=42000, right_port=42002)
    previous = runtime.upsert("previous-call", state="in_call", source_endpoint_id="p4")
    current = runtime.upsert("current-call", state="in_call", source_endpoint_id="p4")
    runtime.attach_relay(previous.call_id, relay)
    try:
        call_projection.publish_media_route(object(), previous.call_id, relay.media_route)
        retired = relay.on_media_route_changed
        assert callable(retired)
        previous.release_resource(f"relay:{previous.call_id}", value=relay)
        runtime.attach_relay(current.call_id, relay)
        call_projection.publish_media_route(object(), current.call_id, relay.media_route)
        active = relay.on_media_route_changed
        assert callable(active) and active is not retired
        asyncio.get_running_loop().call_soon(retired, "ha_transcoding")
        await asyncio.sleep(0)
        assert previous.metadata["committed_media_route"] == "direct"
        assert current.metadata["committed_media_route"] == "direct"
        assert relay.on_media_route_changed is active
        assert "esp_media_routes" not in current.metadata
        sink.assert_not_awaited()
        assert not previous.tasks and not current.tasks
    finally:
        await previous.terminate(TerminationIntent("test_done"))
        await current.terminate(TerminationIntent("test_done"))
        await relay.stop()


@pytest.mark.asyncio
async def test_media_route_binding_follows_relay_handoff_and_disposes_old_owner(monkeypatch):
    from custom_components.voip_stack import device_resolver, esphome_actions
    from custom_components.voip_stack.core.sdp import RtpPcmFormat
    from custom_components.voip_stack.sip_rtp_bridge import RtpPeer, SipRtpRelay

    runtime = _runtime(monkeypatch)
    monkeypatch.setattr(device_resolver, "get_resolver", lambda _hass: SimpleNamespace(
        list_devices=AsyncMock(return_value=[{"device_id": "dev-1"}])))
    sink = AsyncMock()
    monkeypatch.setattr(esphome_actions, "async_call_action", sink)
    monkeypatch.setattr(esphome_actions, "has_action", lambda *_args: True)
    fmt = RtpPcmFormat(8, "PCMA", 8000, 1, 20)
    relay = SipRtpRelay(
        left=RtpPeer("192.0.2.10", 40000, fmt.payload_type, fmt.audio_format,
                     rtp_format=fmt, send_rtp_format=fmt),
        right=RtpPeer("192.0.2.20", 41000, fmt.payload_type, fmt.audio_format,
                      rtp_format=fmt, send_rtp_format=fmt),
        left_port=42000, right_port=42002,
    )
    relay.left_to_right_passthrough = relay.right_to_left_passthrough = False
    previous = runtime.upsert("previous-call", state="in_call", source_endpoint_id="p4")
    current = runtime.upsert("current-call", state="in_call", source_endpoint_id="p4")
    runtime.attach_relay(previous.call_id, relay)

    try:
        call_projection.publish_media_route(object(), previous.call_id, relay.media_route)
        await asyncio.gather(*tuple(previous.tasks))
        previous_callback = relay.on_media_route_changed
        previous_processing_callback = relay.on_media_processing_changed
        assert callable(previous_callback)
        assert callable(previous_processing_callback)
        assert previous.metadata["committed_media_route"] == "ha_transcoding"

        previous.release_resource(f"relay:{previous.call_id}", value=relay)
        runtime.attach_relay(current.call_id, relay)
        relay.left_to_right_passthrough = relay.right_to_left_passthrough = True
        previous_callback(relay.media_route)
        previous_processing_callback()
        assert not previous.tasks
        assert previous.metadata["committed_media_route"] == "ha_transcoding"
        assert "committed_media_route" not in current.metadata
        assert sink.await_count == 0

        call_projection.publish_media_route(object(), current.call_id, relay.media_route)
        await asyncio.gather(*tuple(current.tasks))
        current_callback = relay.on_media_route_changed
        current_processing_callback = relay.on_media_processing_changed
        assert callable(current_callback) and current_callback is not previous_callback
        assert callable(current_processing_callback) and current_processing_callback is not previous_processing_callback
        assert current.metadata["committed_media_route"] == "direct"
        await previous.terminate(TerminationIntent("forwarded"))
        assert relay.on_media_route_changed is current_callback
        assert relay.on_media_processing_changed is current_processing_callback

        relay.left_to_right_passthrough = False
        previous_callback(relay.media_route)
        previous_processing_callback()
        assert not previous.tasks and not current.tasks
        assert current.metadata["committed_media_route"] == "direct"
        assert sink.await_count == 0
        current_callback(relay.media_route)
        await asyncio.gather(*tuple(current.tasks))
        assert current.metadata["committed_media_route"] == "ha_transcoding"
        sink.assert_not_awaited()

        detached_callback = relay.on_media_route_changed
        detached_processing_callback = relay.on_media_processing_changed
        assert callable(detached_callback)
        assert callable(detached_processing_callback)
        current.release_resource(f"relay:{current.call_id}", value=relay)
        runtime.attach_relay(current.call_id, relay)
        relay.left_to_right_passthrough = True
        detached_callback(relay.media_route)
        detached_processing_callback()
        assert not current.tasks
        assert current.metadata["committed_media_route"] == "ha_transcoding"
        assert sink.await_count == 0

        call_projection.publish_media_route(object(), current.call_id, relay.media_route)
        await asyncio.gather(*tuple(current.tasks))
        current_callback = relay.on_media_route_changed
        current_processing_callback = relay.on_media_processing_changed
        assert callable(current_callback) and current_callback is not detached_callback
        assert callable(current_processing_callback) and current_processing_callback is not detached_processing_callback
        assert current.metadata["committed_media_route"] == "direct"
        assert sink.await_count == 0
        relay.left_to_right_passthrough = False
        previous_callback(relay.media_route)
        detached_callback(relay.media_route)
        previous_processing_callback()
        detached_processing_callback()
        assert not previous.tasks and not current.tasks
        assert current.metadata["committed_media_route"] == "direct"
        assert relay.on_media_route_changed is current_callback
        assert relay.on_media_processing_changed is current_processing_callback
        assert sink.await_count == 0

        await relay.stop()
        assert relay.on_media_route_changed is None
        assert relay.on_media_processing_changed is None
        current_callback(relay.media_route)
        current_processing_callback()
        assert not current.tasks
        assert current.metadata["committed_media_route"] == "direct"
        assert sink.await_count == 0
    finally:
        await previous.terminate(TerminationIntent("test_done"))
        await current.terminate(TerminationIntent("test_done"))
        await relay.stop()


def test_phone_projection_derives_authoritative_identity(monkeypatch) -> None:
    runtime = _runtime(monkeypatch)
    sink = Mock()
    monkeypatch.setattr(browser_phone_runtime, "_set_ha_softphone_call_state", sink)
    session = runtime.upsert(
        "call-1",
        state="in_call",
        caller="Alice",
        callee="Casa",
        endpoint_id="phone-1",
    )

    accepted = call_projection.publish_phone_projection(
        object(),
        session,
        "phone-1",
        caller="spoofed",
        call_id="wrong",
        peer_name="Alice",
        selected_tx_format="PCMA/8000",
    )

    assert accepted
    assert sink.call_args.args[1] == "in_call"
    assert sink.call_args.kwargs["call_id"] == "call-1"
    assert sink.call_args.kwargs["caller"] == "Alice"
    assert sink.call_args.kwargs["session_device_id"] == "dev-1"


def test_projection_rejects_stale_generation(monkeypatch) -> None:
    runtime = _runtime(monkeypatch)
    sink = Mock()
    monkeypatch.setattr(browser_phone_runtime, "_set_sip_bridge_call_state", sink)
    stale = runtime.upsert("call-1", state="connecting")
    runtime._discard_dark_session("call-1", TerminationIntent("cancelled"))
    runtime.create_session("call-1")

    assert not call_projection.publish_bridge_projection(object(), stale)
    sink.assert_not_called()


@pytest.mark.asyncio
async def test_terminal_projection_waits_for_cleanup_and_runtime_retirement(
    monkeypatch,
) -> None:
    runtime = _runtime(monkeypatch)
    sink = Mock()
    monkeypatch.setattr(browser_phone_runtime, "_set_sip_bridge_call_state", sink)
    gate = asyncio.Event()
    runtime.bind_termination_observer(
        lambda session, intent: call_projection.publish_bridge_projection(
            object(),
            session,
            intent=intent,
            reason=intent.reason,
        )
    )
    session = runtime.upsert("call-1", state="in_call", caller="A", callee="B")
    session.add_resource("blocked", object(), lambda _reason: gate.wait())
    intent = TerminationIntent("busy", public_state="busy")

    cleanup = session.start_termination(intent)
    await asyncio.sleep(0)

    assert not call_projection.publish_bridge_projection(
        object(),
        session,
        intent=intent,
        reason="busy",
    )
    assert session.state == "in_call"
    assert runtime.get_session("call-1") is session
    sink.assert_not_called()

    gate.set()
    await cleanup

    assert runtime.get_session("call-1") is None
    assert runtime.is_terminated("call-1", generation=session.generation)
    assert sink.call_args.args[1] == "busy"


@pytest.mark.asyncio
async def test_terminal_projection_rejects_superseded_generation(monkeypatch) -> None:
    runtime = _runtime(monkeypatch)
    sink = Mock()
    monkeypatch.setattr(browser_phone_runtime, "_set_sip_bridge_call_state", sink)
    session = runtime.upsert("call-1", state="in_call")
    intent = TerminationIntent("remote_hangup")

    await session.terminate(intent)
    runtime.create_session("call-1")

    assert not call_projection.publish_bridge_projection(
        object(),
        session,
        intent=intent,
    )
    sink.assert_not_called()


def test_terminal_projection_retires_every_ring_group_phone(monkeypatch) -> None:
    phone_sink = Mock(return_value=True)
    bridge_sink = Mock(return_value=True)
    monkeypatch.setattr(endpoint_lifecycle, "publish_phone_projection", phone_sink)
    monkeypatch.setattr(endpoint_lifecycle, "publish_bridge_projection", bridge_sink)
    runtime = SipEndpointRuntime()
    runtime.activate()
    session = runtime.upsert(
        "call-1",
        state="ringing",
        caller="Alice",
        callee="RG Casa",
        endpoint_id="casa",
        ring_endpoint_ids=("casa", "test"),
    )
    intent = TerminationIntent("cancelled")

    endpoint_lifecycle.project_session_termination(object(), session, intent)

    assert {call.args[2] for call in phone_sink.call_args_list} == {"casa", "test"}
    assert all(call.kwargs["intent"] is intent for call in phone_sink.call_args_list)
    bridge_sink.assert_not_called()


@pytest.mark.asyncio
async def test_staged_phone_terminal_details_publish_after_cleanup(
    monkeypatch,
) -> None:
    runtime = _runtime(monkeypatch)
    sink = Mock()
    monkeypatch.setattr(browser_phone_runtime, "_set_ha_softphone_call_state", sink)
    runtime.bind_termination_observer(
        lambda session, intent: endpoint_lifecycle.project_session_termination(
            object(), session, intent
        )
    )
    session = runtime.upsert(
        "call-1",
        state="in_call",
        caller="Alice",
        callee="Kitchen",
    )
    runtime.observe_leg(
        "call-1",
        "browser:kitchen",
        role="ha_softphone",
        state="in_call",
        endpoint_id="kitchen",
    )
    call_projection.stage_phone_termination_projection(
        session,
        "kitchen",
        peer_name="Alice",
        direction="incoming",
        reason="remote_hangup",
        origin="remote",
    )

    await session.terminate(TerminationIntent("remote_hangup"))

    assert runtime.get_session("call-1") is None
    assert sink.call_args.args[1] == "idle"
    assert sink.call_args.kwargs["peer_name"] == "Alice"
    assert sink.call_args.kwargs["origin"] == "remote"


def test_terminal_projection_details_cannot_mutate_terminating_session(
    monkeypatch,
) -> None:
    runtime = _runtime(monkeypatch)
    session = runtime.upsert("call-1", state="in_call")
    session.claim_termination(TerminationIntent("remote_hangup"))

    assert not call_projection.stage_phone_termination_projection(
        session,
        "kitchen",
        peer_name="late callback",
    )
    assert "terminal_phone_projections" not in session.metadata


def test_live_projection_is_rejected_after_terminal_claim(monkeypatch) -> None:
    runtime = _runtime(monkeypatch)
    sink = Mock()
    monkeypatch.setattr(browser_phone_runtime, "_set_sip_bridge_call_state", sink)
    session = runtime.upsert("call-1", state="in_call")
    session.claim_termination(TerminationIntent("remote_hangup"))

    assert not call_projection.publish_bridge_projection(
        object(), session, peer_name="late callback"
    )
    sink.assert_not_called()


def test_new_phone_leg_rings_while_the_original_caller_is_answered(monkeypatch):
    runtime = _runtime(monkeypatch)
    sink = Mock()
    monkeypatch.setattr(browser_phone_runtime, "_set_ha_softphone_call_state", sink)
    session = runtime.upsert("greeting", state="in_call", owner="automation", caller="Alice", callee="Welcome")
    assert call_projection.observe_phone_leg_projection(
        object(), runtime, session, "reception", "ringing",
        leg_id="browser:reception", sip_status_code=180,
    )
    assert session.state == "in_call"
    assert sink.call_args.args[1] == "ringing"
    assert sink.call_args.kwargs["call_id"] == session.call_id
