import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.voip_stack import call_projection, endpoint_lifecycle, websocket_api
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime


pytestmark = pytest.mark.ha


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
async def test_media_route_targets_each_physical_sip_leg_once(monkeypatch):
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
    call_projection.publish_esp_media_route(object(), "source-call", "direct")
    await asyncio.gather(*tuple(session.tasks))
    assert { (call.args[1]["device_id"], call.args[3]["call_id"], call.args[3]["route"])
             for call in sink.call_args_list } == {
        ("device-p4", "source-call", "ha_transcoding"),
        ("device-ws3", "dest-call", "ha_transcoding"),
    }
    call_projection.publish_esp_media_route(object(), "source-call", "direct")
    await asyncio.gather(*tuple(session.tasks))
    assert sink.await_count == 2
    await session.terminate(TerminationIntent("local_hangup"))


@pytest.mark.asyncio
async def test_media_route_cannot_outlive_its_call_generation(monkeypatch):
    from custom_components.voip_stack import device_resolver, esphome_actions
    runtime = _runtime(monkeypatch)
    lookup_started = asyncio.Event()
    lookup_finish = asyncio.Event()
    async def devices():
        lookup_started.set()
        await lookup_finish.wait()
        return [{"device_id": "dev-1"}]
    monkeypatch.setattr(device_resolver, "get_resolver", lambda _hass: SimpleNamespace(list_devices=devices))
    sink = AsyncMock()
    monkeypatch.setattr(esphome_actions, "async_call_action", sink)
    monkeypatch.setattr(esphome_actions, "has_action", lambda *_args: True)
    session = runtime.upsert("source-call", state="in_call", source_endpoint_id="p4")
    call_projection.publish_esp_media_route(object(), "source-call", "ha_transcoding")
    await lookup_started.wait()
    await session.terminate(TerminationIntent("local_hangup"))
    lookup_finish.set()
    sink.assert_not_awaited()
    assert not session.tasks


def test_phone_projection_derives_authoritative_identity(monkeypatch) -> None:
    runtime = _runtime(monkeypatch)
    sink = Mock()
    monkeypatch.setattr(websocket_api, "_set_ha_softphone_call_state", sink)
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
    monkeypatch.setattr(websocket_api, "_set_sip_bridge_call_state", sink)
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
    monkeypatch.setattr(websocket_api, "_set_sip_bridge_call_state", sink)
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
    monkeypatch.setattr(websocket_api, "_set_sip_bridge_call_state", sink)
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
    monkeypatch.setattr(websocket_api, "_set_ha_softphone_call_state", sink)
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
    monkeypatch.setattr(websocket_api, "_set_sip_bridge_call_state", sink)
    session = runtime.upsert("call-1", state="in_call")
    session.claim_termination(TerminationIntent("remote_hangup"))

    assert not call_projection.publish_bridge_projection(
        object(), session, peer_name="late callback"
    )
    sink.assert_not_called()
