"""Native clients must stay scoped to their registration and live call generation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio
import time
from urllib.parse import parse_qs, urlsplit

from aiohttp import web
import pytest

from custom_components.voip_stack import companion_view as views
from custom_components.voip_stack import companion_phones as phones
from custom_components.voip_stack.companion_protocol import CompanionCallToken
from custom_components.voip_stack.endpoint_registry import EndpointRegistry
from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint

pytestmark = pytest.mark.ha


@pytest.fixture
def fixture(monkeypatch):
    token = CompanionCallToken("registration-1", "call-1", 7)
    binding = phones.CompanionBinding("registration-1", "mobile-1", "companion:registration-1", "user-1", "webhook-1")
    invitation = phones.CompanionInvitation(token, time.monotonic() + 30, "Door")
    directory = EndpointRegistry()
    directory.register(PhoneEndpoint(binding.endpoint_id, "Mobile", EndpointKind.COMPANION, active_call_id="call-1", device_id="phone-1"))
    manager = SimpleNamespace(bindings={binding.endpoint_id: binding}, invitations={binding.endpoint_id: invitation})
    runtime = SimpleNamespace(companion_phones=manager, endpoints=directory)
    registry = SimpleNamespace(resolve_session_id=lambda x: x, is_generation_current=lambda c, g: c == "call-1" and g == 7)
    listeners = []
    def listen(event_type, listener):
        listeners.append(listener)
        return lambda: listeners.remove(listener)
    hass = SimpleNamespace(services=SimpleNamespace(async_call=AsyncMock()),
        bus=SimpleNamespace(async_listen=listen), listeners=listeners)
    user = SimpleNamespace(id="user-1", permissions=SimpleNamespace(check_entity=lambda *args: True))
    class Request(dict):
        app = {"hass": hass}
        query = {k: v[0] for k, v in parse_qs(urlsplit(token.path()).query).items()}
        async def json(self):
            return self.body
    request = Request(hass_user=user)
    snapshot = {"state": "ringing", "call_id": "call-1"}
    monkeypatch.setattr(views, "require_runtime_data", lambda h: runtime)
    monkeypatch.setattr(views, "call_registry", lambda h: registry)
    monkeypatch.setattr(views, "_ha_softphone_state", lambda *args: snapshot)
    return SimpleNamespace(token=token, binding=binding, invitation=invitation, manager=manager,
        runtime=runtime, registry=registry, hass=hass, user=user, request=request, snapshot=snapshot)


@pytest.mark.asyncio
async def test_matching_registration_can_read_its_ringing_call(fixture):
    response = await views.CompanionCallView().get(fixture.request)
    assert response.status == 200
    assert '"id": "call-1"' in response.text


@pytest.mark.parametrize("change", ["user", "registration", "call", "generation", "claim", "terminated", "expired"])
@pytest.mark.asyncio
async def test_unrelated_or_stale_invitation_cannot_control_call(fixture, change):
    f = fixture
    if change == "user": f.user.id = "user-2"
    elif change == "registration": f.request.query["registration_id"] = "other"
    elif change == "call": f.request.query["call_id"] = "other"
    elif change == "generation": f.request.query["generation"] = "6"
    elif change == "claim":
        f.runtime.endpoints.release_call(f.binding.endpoint_id, "call-1")
        f.runtime.endpoints.claim_call(f.binding.endpoint_id, "other")
    elif change == "terminated": f.registry.is_generation_current = lambda *args: False
    elif change == "expired": f.manager.invitations[f.binding.endpoint_id] = phones.CompanionInvitation(f.token, 0, "Door")
    f.request.body = {"action": "answer", "client_id": "native-client-123456"}
    with pytest.raises((web.HTTPForbidden, web.HTTPGone)):
        await views.CompanionCallView().post(f.request)
    f.hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_answer_uses_existing_phone_service_and_scoped_media(fixture):
    f = fixture
    async def answer(*args, **kwargs):
        f.snapshot["state"] = "in_call"
    f.hass.services.async_call.side_effect = answer
    f.request.body = {"action": "answer", "client_id": "native-client-123456"}
    response = await views.CompanionCallView().post(f.request)
    assert response.status == 200
    args = f.hass.services.async_call.await_args
    assert args.args[:2] == ("voip_stack", "answer")
    assert args.args[2]["device_id"] == "phone-1"
    assert args.args[2]["call_id"] == "call-1"
    assert args.kwargs["context"].user_id == "user-1"
    assert "/api/voip_stack/ws?" in response.text
    assert "native-client-123456" in response.text


@pytest.mark.asyncio
async def test_only_selected_native_phone_gets_one_ring_and_one_cancel(monkeypatch, fixture):
    f = fixture
    manager = phones.CompanionPhones(f.hass, SimpleNamespace())
    manager.bindings[f.binding.endpoint_id] = f.binding
    manager._notify = AsyncMock()
    tasks = []
    monkeypatch.setattr(phones, "call_registry", lambda h: SimpleNamespace(current_generation=lambda c: 7))
    monkeypatch.setattr(phones, "create_runtime_task", lambda h, c: tasks.append(asyncio.create_task(c)))
    def event(endpoint, state):
        return SimpleNamespace(data={"endpoint_id": endpoint, "call_id": "call-1", "state": state, "caller": "Door"})
    manager._call_changed(event("browser:another-room", "ringing"))
    manager._call_changed(event(f.binding.endpoint_id, "ringing"))
    manager._call_changed(event(f.binding.endpoint_id, "ringing"))
    manager._call_changed(event(f.binding.endpoint_id, "in_call"))
    manager._call_changed(event(f.binding.endpoint_id, "idle"))
    await asyncio.gather(*tasks)
    assert [c.args[2] for c in manager._notify.await_args_list] == ["ring", "cancel"]
    assert all(c.args[0] == f.binding for c in manager._notify.await_args_list)
    assert manager.invitations == {}


@pytest.mark.asyncio
async def test_fork_answer_waits_for_commit_and_releases_subscription(fixture):
    f = fixture
    def committed():
        f.snapshot["state"] = "in_call"
        for listener in tuple(f.hass.listeners):
            listener(SimpleNamespace(data={"endpoint_id": f.binding.endpoint_id}))
    async def answer(*args, **kwargs):
        f.snapshot["state"] = "connecting"
        asyncio.get_running_loop().call_soon(committed)
    f.hass.services.async_call.side_effect = answer
    f.request.body = {"action": "answer", "client_id": "native-client-123456"}
    response = await views.CompanionCallView().post(f.request)
    assert '"state": "in_call"' in response.text
    assert f.hass.listeners == []


@pytest.mark.asyncio
async def test_cancel_during_answer_releases_subscription(fixture):
    f = fixture
    def cancelled():
        f.manager.invitations.clear()
        for listener in tuple(f.hass.listeners):
            listener(SimpleNamespace(data={"endpoint_id": f.binding.endpoint_id}))
    async def answer(*args, **kwargs):
        f.snapshot["state"] = "connecting"
        asyncio.get_running_loop().call_soon(cancelled)
    f.hass.services.async_call.side_effect = answer
    f.request.body = {"action": "answer", "client_id": "native-client-123456"}
    with pytest.raises(web.HTTPGone):
        await views.CompanionCallView().post(f.request)
    assert f.hass.listeners == []


@pytest.mark.asyncio
async def test_call_replaced_while_reading_body_cannot_receive_old_command(fixture):
    f = fixture
    async def changed_body():
        f.runtime.endpoints.release_call(f.binding.endpoint_id, "call-1")
        f.runtime.endpoints.claim_call(f.binding.endpoint_id, "replacement")
        return {"action": "answer", "client_id": "native-client-123456"}
    f.request.json = changed_body
    with pytest.raises(web.HTTPGone):
        await views.CompanionCallView().post(f.request)
    f.hass.services.async_call.assert_not_awaited()
    assert f.hass.listeners == []


@pytest.mark.asyncio
async def test_failed_answer_removes_its_event_listener(fixture):
    f = fixture
    f.request.body = {"action": "answer", "client_id": "native-client-123456"}
    f.hass.services.async_call.side_effect = RuntimeError("Answer could not commit")
    with pytest.raises(RuntimeError, match="Answer could not commit"):
        await views.CompanionCallView().post(f.request)
    assert f.hass.listeners == []
