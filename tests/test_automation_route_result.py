from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.voip_stack.inbound_routing import automation
from custom_components.voip_stack.inbound_routing.automation import AutomationRoute
from custom_components.voip_stack import routing_automation
from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
from custom_components.voip_stack.endpoint_session import TerminationIntent

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


@pytest.mark.parametrize("trunk_invite", [False, True])
async def test_route_request_commits_identity_before_projection(monkeypatch, trunk_invite) -> None:
    registry = SipEndpointRuntime()
    registry.activate()
    registry.upsert("call-1", state="connecting")
    projected = []

    monkeypatch.setattr(automation, "call_registry", lambda _hass: registry)
    monkeypatch.setattr(routing_automation, "call_registry", lambda _hass: registry)
    def publish(_hass, session, **details):
        projected.append((session, details))
        registry.artifact_for(session.call_id, "pending_route")["future"].set_result({"action": "default"})
    monkeypatch.setattr(routing_automation, "publish_bridge_projection", publish)
    wire = SimpleNamespace(
        audio_format=SimpleNamespace(wire_token=lambda: "PCMA/8000"),
        wire_token=lambda: "pt=8:PCMA/8000/1/20ms",
    )
    invite = SimpleNamespace(
        call_id="call-1",
        caller="Alice",
        target="601",
        source_host="192.0.2.5",
        routing_caller="Alice",
        routing_target="601",
        send_format=wire,
        recv_format=wire,
        selected_format=SimpleNamespace(
            encoding="PCMA", sample_rate=8000, channels=1
        ),
    )
    decision = SimpleNamespace(
        action=SimpleNamespace(value="ha"), target="Casa", sip_uri="", entry=None
    )
    runtime = SimpleNamespace(hass=object(), ha_peer_name=lambda _hass: "Lab")

    result = await automation.request_route_override(
        runtime=runtime,
        invite=invite,
        decision=decision,
        authenticated_account=False,
        caller_is_trusted_endpoint=True,
        automation_routing_enabled=True,
        trunk_invite=trunk_invite,
    )

    assert result.action == "default"
    session = registry.get_session("call-1")
    assert session is not None
    assert (session.caller, session.callee) == ("Alice", "Casa")
    assert projected and projected[0][0] is session

    assert projected[0][1]["target_route"] == "601"
    assert projected[0][1]["dialed_target"] == "601"
    assert projected[0][1]["called_extension"] == "601"


def test_route_result_retains_the_selected_outbound_trunk():
    route = AutomationRoute.from_payload({"action": "forward", "destination": "External", "trunk_id": "work"})
    assert route.trunk_id == "work"


async def test_initial_window_is_shared_and_does_not_reopen(monkeypatch):
    registry = SipEndpointRuntime()
    registry.activate()
    registry.upsert("same-call", state="connecting", owner="router")
    projected = []
    monkeypatch.setattr(automation, "call_registry", lambda _hass: registry)
    monkeypatch.setattr(routing_automation, "call_registry", lambda _hass: registry)

    def publish(_hass, session, **details):
        projected.append(details)
        route = registry.artifact_for(session.call_id, "pending_route")
        assert details["decision_timeout_ms"] == 1500
        assert details["decision_deadline"] - route["created_at"] == pytest.approx(1.5)
        payload = {"action": "forward", "destination": "First" if len(projected) == 1 else "Second"}
        asyncio.get_running_loop().call_soon(route["future"].set_result, payload)

    monkeypatch.setattr(routing_automation, "publish_bridge_projection", publish)
    wire = SimpleNamespace(audio_format=SimpleNamespace(wire_token=lambda: "PCMA/8000"), wire_token=lambda: "PCMA/8000")
    invite = SimpleNamespace(call_id="same-call", caller="Alice", target="Desk", source_host="192.0.2.1",
        routing_caller="Alice", routing_target="Desk", send_format=wire, recv_format=wire,
        selected_format=SimpleNamespace(encoding="PCMA", sample_rate=8000, channels=1))
    values = dict(runtime=SimpleNamespace(hass=object(), ha_peer_name=lambda _: "HA"), invite=invite,
        decision=SimpleNamespace(action=SimpleNamespace(value="direct"), target="Desk", sip_uri="", entry=None),
        authenticated_account=False, caller_is_trusted_endpoint=True, automation_routing_enabled=True, trunk_invite=False)
    first, second = await asyncio.gather(automation.request_route_override(**values), automation.request_route_override(**values))
    third = await automation.request_route_override(**values)
    assert first.destination == second.destination == third.destination == "First"
    assert len(projected) == 1
    assert registry.artifact_for("same-call", "pending_route") is None
    later = await routing_automation.request_route_window(values["runtime"].hass,
        registry.get_session("same-call"), invite=invite, stage="dtmf_fallback")
    assert later["destination"] == "Second"
    assert len(projected) == 2
    generation = registry.get_session("same-call").generation
    await registry.request_termination("same-call", TerminationIntent("test_done"))
    replacement = registry.create_session("same-call")
    assert replacement.generation != generation
    assert (await automation.request_route_override(**values)).destination == "Second"
    assert len(projected) == 3
    await registry.shutdown()


async def test_initial_gate_cannot_replace_an_existing_group_decision(monkeypatch):
    registry = SipEndpointRuntime()
    registry.activate()
    session = registry.upsert("group-owned", state="ringing", owner="router")
    future = asyncio.get_running_loop().create_future()
    group = {"future": future, "ring_group_endpoint_ids": ("desk",)}
    registry.set_pending_route(session.call_id, group)
    monkeypatch.setattr(routing_automation, "call_registry", lambda _: registry)
    monkeypatch.setattr(routing_automation, "publish_bridge_projection",
        lambda *_args, **_details: registry.artifact_for(session.call_id, "pending_route")["future"].set_result({}))
    try:
        with pytest.raises(RuntimeError):
            await routing_automation.request_route_window(object(), session)
        assert registry.artifact_for(session.call_id, "pending_route") is group
        assert not future.done()
    finally:
        future.cancel()
        await registry.shutdown()
