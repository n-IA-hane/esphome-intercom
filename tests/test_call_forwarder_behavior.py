"""Executable guards for the canonical call-forwarding boundary."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

from homeassistant.exceptions import ServiceValidationError
import pytest

from custom_components.voip_stack import call_forwarder


pytestmark = pytest.mark.ha


def _runtime() -> call_forwarder.ForwardRuntime:
    return call_forwarder.ForwardRuntime(
        hass=SimpleNamespace(),
        config={},
        local_ip="127.0.0.1",
        route_resolver=Mock(),
        attach_client_media_update=Mock(),
        browser_leg_for_member=Mock(),
        defer_invite_to_softphone=Mock(),
        prepare_outbound_leg=Mock(),
        publish_pending_ringing=Mock(),
        sip_uri_for_member=Mock(),
        start_local_assist_bridge=Mock(),
    )


def _registry(
    *,
    invite: object | None = None,
    state: str = "ringing",
    sequence: int = 4,
    route_history: tuple[str, ...] = (),
) -> SimpleNamespace:
    context = SimpleNamespace(
        state=state,
        sequence=sequence,
        route_history=route_history,
    )
    return SimpleNamespace(
        pending_invites={"call-1": invite} if invite is not None else {},
        event_context=Mock(return_value=context),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call_id", "destination", "on_failure", "message"),
    [
        ("", "Test", "resume", "call_id and destination are required"),
        ("call-1", "", "resume", "call_id and destination are required"),
        ("call-1", "Test", "retry", "on_failure must be"),
    ],
)
async def test_forward_rejects_invalid_public_request(
    call_id: str,
    destination: str,
    on_failure: str,
    message: str,
) -> None:
    with pytest.raises(ServiceValidationError, match=message):
        await call_forwarder.async_forward_existing_call(
            _runtime(),
            call_id=call_id,
            destination=destination,
            on_failure=on_failure,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expected_state", "expected_sequence", "message"),
    [
        ("in_call", 0, "is ringing, expected in_call"),
        ("", 9, "sequence is 4, expected 9"),
    ],
)
async def test_forward_rejects_stale_snapshot_before_route_mutation(
    monkeypatch: pytest.MonkeyPatch,
    expected_state: str,
    expected_sequence: int,
    message: str,
) -> None:
    registry = _registry(invite=object())
    monkeypatch.setattr(call_forwarder, "_call_registry", lambda _hass: registry)

    with pytest.raises(ServiceValidationError, match=message):
        await call_forwarder.async_forward_existing_call(
            _runtime(),
            call_id="call-1",
            destination="Test",
            expected_state=expected_state,
            expected_sequence=expected_sequence,
        )


@pytest.mark.asyncio
async def test_forward_rejects_route_loop_and_missing_invite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry(invite=object(), route_history=("hop",) * 8)
    monkeypatch.setattr(call_forwarder, "_call_registry", lambda _hass: registry)
    with pytest.raises(ServiceValidationError, match="exceeded 8 routing hops"):
        await call_forwarder.async_forward_existing_call(
            _runtime(), call_id="call-1", destination="Test"
        )

    registry = _registry()
    with pytest.raises(ServiceValidationError, match="not a forwardable"):
        await call_forwarder.async_forward_existing_call(
            _runtime(), call_id="call-1", destination="Test"
        )


@pytest.mark.asyncio
async def test_forward_rejects_concurrent_owner_without_replacing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry(invite=object(), state="ringing")
    owner = asyncio.create_task(asyncio.sleep(10))
    stored = {"forward": owner}
    artifacts = SimpleNamespace(
        task_for=lambda call_id, name: stored.get(name),
        artifacts_for=lambda call_id: SimpleNamespace(forward_claim=False),
    )
    monkeypatch.setattr(call_forwarder, "_call_registry", lambda _hass: registry)
    monkeypatch.setattr(
        call_forwarder,
        "call_runtime_artifacts",
        lambda _hass: artifacts,
    )

    try:
        with pytest.raises(ServiceValidationError, match="already being forwarded"):
            await call_forwarder.async_forward_existing_call(
                _runtime(), call_id="call-1", destination="Test"
            )
        assert not owner.cancelled()
        assert stored["forward"] is owner
    finally:
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)
