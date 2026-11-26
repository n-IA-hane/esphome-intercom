"""Production trunk routing delegates to the call generation's shared window."""

import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from custom_components.voip_stack import routing_automation, trunk_routing
from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime

pytestmark = pytest.mark.ha


@pytest.fixture
async def window(hass, monkeypatch):
    registry = SipEndpointRuntime()
    registry.activate()
    session = registry.upsert("call-1", state="connecting", owner="router")
    sink = Mock()
    monkeypatch.setattr(routing_automation, "call_registry", lambda _: registry)
    monkeypatch.setattr(routing_automation, "publish_bridge_projection", sink)
    invite = SimpleNamespace(call_id=session.call_id, caller="Alice", source_host="192.0.2.2")
    yield SimpleNamespace(hass=hass, registry=registry, session=session, invite=invite, sink=sink)
    await registry.shutdown()


@pytest.mark.parametrize("log_level", [logging.INFO, logging.DEBUG])
async def test_explicit_automation_decision_wins_and_future_is_removed(window, caplog, log_level):
    caplog.set_level(log_level, logger=trunk_routing._LOGGER.name)
    def answer(*_args, **_details):
        window.registry.artifact_for("call-1", "pending_route")["future"].set_result(
            {"action": "forward", "destination": "RG Casa"})
    window.sink.side_effect = answer
    result = await trunk_routing.async_request_inbound_destination(window.hass, window.invite,
        registry=window.registry, session=window.session,
        trunk_config={"trunk_inbound_default_target": "Casa", "name": "Office"}, timeout=1.0)
    assert result == {"action": "forward", "destination": "RG Casa"}
    assert window.registry.artifact_for("call-1", "pending_route") is None
    assert window.sink.call_args.args[1] is window.session
    assert window.sink.call_args.kwargs["fallback_destination"] == "Casa"
    assert window.sink.call_args.kwargs["ingress"] == "trunk"
    records = [item for item in caplog.records if item.name == trunk_routing._LOGGER.name]
    assert len(records) == int(log_level == logging.DEBUG)
    if records:
        assert "Trunk Office waits for a routing action" in records[0].getMessage()
        assert "fallback is Casa" in records[0].getMessage()


async def test_default_or_timeout_preserves_configured_fallback(window, caplog):
    caplog.set_level(logging.DEBUG, logger=trunk_routing._LOGGER.name)
    def answer(*_args, **_details):
        window.registry.artifact_for("call-1", "pending_route")["future"].set_result(
            {"action": "default", "destination": "ignored"})
    window.sink.side_effect = answer
    result = await trunk_routing.async_request_inbound_destination(window.hass, window.invite,
        registry=window.registry, session=window.session,
        trunk_config={"trunk_inbound_default_target": "  Test  "}, timeout=1.0)
    assert result == {}
    assert trunk_routing.trunk_default_target({}) == ""
    assert "Routing action keeps trunk fallback Test" in caplog.text
    assert "No routing action before the deadline" not in caplog.text


async def test_timeout_always_removes_pending_route(window, caplog):
    caplog.set_level(logging.DEBUG, logger=trunk_routing._LOGGER.name)
    result = await trunk_routing.async_request_inbound_destination(window.hass, window.invite,
        registry=window.registry, session=window.session, trunk_config={}, timeout=0.001)
    assert result == {}
    assert window.registry.artifact_for("call-1", "pending_route") is None
    assert "No routing action before the deadline" in caplog.text
    assert window.sink.call_args.kwargs["fallback_destination"] == ""


async def test_default_destination_can_keep_an_explicit_outbound_line(window):
    def answer(*_args, **_details):
        window.registry.artifact_for("call-1", "pending_route")["future"].set_result(
            {"action": "default", "destination": "ignored", "trunk_id": "work"})
    window.sink.side_effect = answer
    result = await trunk_routing.async_request_inbound_destination(window.hass, window.invite,
        registry=window.registry, session=window.session,
        trunk_config={"trunk_inbound_default_target": "12345"}, timeout=1.0)
    assert result == {"action": "default", "trunk_id": "work"}
