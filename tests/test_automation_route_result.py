from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.voip_stack.inbound_routing import automation
from custom_components.voip_stack.inbound_routing.automation import AutomationRoute
from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime

pytestmark = pytest.mark.ha


def test_answer_ha_route_preserves_selected_endpoint() -> None:
    route = AutomationRoute.from_payload(
        {
            "action": "answer_ha",
            "endpoint_id": "browser:casa",
        }
    )

    assert route.action == "answer_ha"
    assert route.endpoint_id == "browser:casa"


async def test_route_request_commits_identity_before_projection(monkeypatch) -> None:
    registry = SipEndpointRuntime()
    registry.activate()
    registry.upsert("call-1", state="connecting")
    projected = []

    def set_route(_hass, _call_id, route) -> None:
        route["future"].set_result({"action": "default"})

    monkeypatch.setattr(automation, "call_registry", lambda _hass: registry)
    monkeypatch.setattr(automation, "set_pending_route", set_route)
    monkeypatch.setattr(automation, "take_pending_route", lambda *_args: None)
    monkeypatch.setattr(
        automation,
        "publish_bridge_projection",
        lambda _hass, session, **details: projected.append((session, details)),
    )
    wire = SimpleNamespace(
        audio_format=SimpleNamespace(wire_token=lambda: "PCMA/8000"),
        wire_token=lambda: "pt=8:PCMA/8000/1/20ms",
    )
    invite = SimpleNamespace(
        call_id="call-1",
        caller="Alice",
        target="Casa",
        source_host="192.0.2.5",
        routing_caller="Alice",
        routing_target="Casa",
        send_format=wire,
        recv_format=wire,
        selected_format=SimpleNamespace(
            encoding="PCMA", sample_rate=8000, channels=1
        ),
    )
    decision = SimpleNamespace(
        action=SimpleNamespace(value="ha"), target="Casa", sip_uri=""
    )
    runtime = SimpleNamespace(hass=object(), ha_peer_name=lambda _hass: "Lab")

    result = await automation.request_route_override(
        runtime=runtime,
        invite=invite,
        decision=decision,
        registered_source=False,
        caller_is_trusted_endpoint=True,
        automation_routing_enabled=True,
        trunk_invite=False,
    )

    assert result.action == "default"
    session = registry.get_session("call-1")
    assert session is not None
    assert (session.caller, session.callee) == ("Alice", "Casa")
    assert projected and projected[0][0] is session
