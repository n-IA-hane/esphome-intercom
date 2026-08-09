from types import SimpleNamespace
from unittest.mock import Mock

from custom_components.voip_stack import call_projection, websocket_api
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime


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
    return runtime


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

    accepted = call_projection.publish_call_projection(
        object(),
        session,
        call_projection.CallProjectionEvent.phone(
            session,
            "phone-1",
            caller="spoofed",
            call_id="wrong",
            peer_name="Alice",
            selected_tx_format="PCMA/8000",
        ),
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

    assert not call_projection.publish_call_projection(
        object(), stale, call_projection.CallProjectionEvent.bridge(stale)
    )
    sink.assert_not_called()


def test_terminal_projection_uses_intent_without_mutating_session(monkeypatch) -> None:
    runtime = _runtime(monkeypatch)
    sink = Mock()
    monkeypatch.setattr(websocket_api, "_set_sip_bridge_call_state", sink)
    session = runtime.upsert("call-1", state="in_call", caller="A", callee="B")
    intent = TerminationIntent("busy", public_state="busy")

    assert call_projection.publish_call_projection(
        object(),
        session,
        call_projection.CallProjectionEvent.bridge(
            session, intent=intent, reason="busy"
        ),
    )
    assert session.state == "in_call"
    assert sink.call_args.args[1] == "busy"
