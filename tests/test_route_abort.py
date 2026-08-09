"""Behavioral tests for the shared pre-commit route rollback boundary."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

pytestmark = pytest.mark.ha


class _Registry:
    def __init__(self, *, media=None, session=None) -> None:
        self.media = media
        self.sessions = {"call-1": session} if session is not None else {}
        self.invite_taken = False

    def resolve_session_id(self, call_id):
        return call_id

    def bridge_for(self, _call_id):
        return "", ""

    def sip_client_for(self, _call_id):
        return None

    def transition(self, call_id, **changes):
        session = self.sessions[call_id]
        session.owner = changes["owner"]
        session.state = changes["state"]
        return session

    def take_pending_invite(self, _call_id):
        self.invite_taken = True

    def take_media(self, _call_id, *, provisional):
        assert provisional
        media, self.media = self.media, None
        return media


@pytest.fixture
def abort_env(monkeypatch: pytest.MonkeyPatch):
    from custom_components.voip_stack import route_abort
    from custom_components.voip_stack.endpoint_session import SipTerminationDisposition

    terminate = AsyncMock(return_value=True)
    cleanup = AsyncMock()
    route_taken = Mock()
    released = Mock()
    artifacts = SimpleNamespace(trunk_closed=False)
    monkeypatch.setattr(route_abort, "take_pending_route", route_taken)
    monkeypatch.setattr(route_abort, "release_media_reservation", released)
    monkeypatch.setattr(route_abort, "async_cleanup_outbound_attempts", cleanup)
    monkeypatch.setattr(
        route_abort,
        "EndpointTerminationHandler",
        lambda _hass: SimpleNamespace(terminate=terminate),
    )
    monkeypatch.setattr(
        route_abort,
        "call_runtime_artifacts",
        lambda _hass: SimpleNamespace(artifacts_for=lambda _call_id: artifacts),
    )
    monkeypatch.setitem(
        sys.modules,
        "custom_components.voip_stack.local_softphone_runtime",
        SimpleNamespace(local_softphone_bridge=lambda _: None),
    )
    return (
        route_abort,
        SipTerminationDisposition,
        terminate,
        cleanup,
        route_taken,
        released,
        artifacts,
    )


@pytest.mark.asyncio
async def test_resume_preserves_invite_and_media(abort_env) -> None:
    route_abort, _disposition, terminate, cleanup, route_taken, released, _artifacts = (
        abort_env
    )
    session = SimpleNamespace(owner="router", state="connecting", revision=3)
    registry = _Registry(media={"final_response_sent": True}, session=session)
    publish = Mock()

    resumed = await route_abort.async_abort_route(
        route_abort.RouteAbortContext(
            SimpleNamespace(),
            registry,
            "call-1",
            consume_source=True,
            resume_callee="Casa",
            resume_route_kind="direct",
            transition_resume=True,
            publish_resume=publish,
        ),
        route_abort.RouteAbortIntent("failed", "resume"),
    )

    assert resumed
    assert session.owner == "ha_softphone"
    assert session.state == "ringing"
    assert registry.media is not None
    assert not registry.invite_taken
    publish.assert_called_once_with()
    route_taken.assert_called_once()
    terminate.assert_not_awaited()
    released.assert_not_called()
    cleanup.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "answered", "disposition", "status"),
    [
        ("terminate", False, "final_response", 480),
        ("busy", True, "bye", 486),
    ],
)
async def test_terminal_abort_consumes_source_once(
    abort_env, action, answered, disposition, status
) -> None:
    (
        route_abort,
        _disposition,
        terminate,
        cleanup,
        _route_taken,
        released,
        _artifacts,
    ) = abort_env
    media = {"final_response_sent": answered}
    registry = _Registry(media=media)

    await route_abort.async_abort_route(
        route_abort.RouteAbortContext(
            SimpleNamespace(),
            registry,
            "call-1",
            consume_source=True,
            resume_callee="Casa",
            resume_route_kind="direct",
        ),
        route_abort.RouteAbortIntent("failed", action),
    )

    assert registry.invite_taken
    assert registry.media is None
    released.assert_called_once_with(media)
    intent = terminate.await_args.args[1]
    assert intent.sip_disposition.value == disposition
    assert intent.response_status == status
    cleanup.assert_awaited_once()


@pytest.mark.asyncio
async def test_closed_trunk_cannot_resume(abort_env) -> None:
    (
        route_abort,
        disposition,
        terminate,
        _cleanup,
        _route_taken,
        _released,
        artifacts,
    ) = abort_env
    artifacts.trunk_closed = True
    registry = _Registry(media={"final_response_sent": True})

    await route_abort.async_abort_route(
        route_abort.RouteAbortContext(
            SimpleNamespace(),
            registry,
            "call-1",
            consume_source=True,
            resume_callee="Casa",
            resume_route_kind="direct",
        ),
        route_abort.RouteAbortIntent("closed", "resume"),
    )

    assert terminate.await_args.args[1].sip_disposition is disposition.BYE
