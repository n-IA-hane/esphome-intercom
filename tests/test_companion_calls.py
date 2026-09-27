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


def test_phone_name_follows_tracker_without_changing_identity(monkeypatch):
    mobile = SimpleNamespace(entry_id="registration", title="CPH2709",
        data={"device_id": "installation-id", "device_name": "CPH2709"})
    tracker = SimpleNamespace(domain="device_tracker", platform="mobile_app",
        unique_id="installation-id", name=None, original_name="CPH2709",
        entity_id="device_tracker.cph2709", disabled_by="user")
    battery = SimpleNamespace(domain="sensor", platform="mobile_app", unique_id="battery",
        name="Unrelated battery name")
    monkeypatch.setattr(phones.er, "async_get", lambda _: object())
    monkeypatch.setattr(phones.er, "async_entries_for_config_entry", lambda *_: [battery, tracker])
    identity = phones.companion_endpoint_id(mobile.entry_id)
    assert phones.companion_display_name(None, mobile) == "CPH2709"
    tracker.name = "App Daniele"
    tracker.entity_id = "device_tracker.daniele"
    assert phones.companion_display_name(None, mobile) == "App Daniele"
    assert phones.companion_endpoint_id(mobile.entry_id) == identity


def test_tracker_absence_does_not_require_location_permission(monkeypatch):
    mobile = SimpleNamespace(entry_id="registration", title="Fallback", data={"device_name": "Tablet"})
    monkeypatch.setattr(phones.er, "async_get", lambda _: object())
    monkeypatch.setattr(phones.er, "async_entries_for_config_entry", lambda *_: [])
    assert phones.companion_display_name(None, mobile) == "Tablet"


@pytest.mark.asyncio
async def test_disabled_companion_support_preserves_phone_and_makes_it_unavailable(monkeypatch, fixture):
    f = fixture
    phone = SimpleNamespace(data={"endpoint_id": f.binding.endpoint_id, "kind": "companion", "enabled": True})
    entry = SimpleNamespace(data={phones.CONF_COMPANION_ENABLED: False})
    f.hass.config_entries = SimpleNamespace(async_entries=lambda _: [])
    manager = phones.CompanionPhones(f.hass, entry)
    manager.bindings[f.binding.endpoint_id] = f.binding
    monkeypatch.setattr(phones.dr, "async_get", lambda _: object())
    monkeypatch.setattr(phones, "phone_subentries", lambda _: [phone])
    monkeypatch.setattr(phones, "sync_registry_from_entry", lambda *_: None)
    monkeypatch.setattr(phones, "require_runtime_data", lambda _: f.runtime)
    await manager.sync()
    assert manager.bindings == {}
    endpoint = f.runtime.endpoints.get(f.binding.endpoint_id)
    assert endpoint is not None
    assert endpoint.availability.value == "unavailable"
    assert endpoint.active_call_id == "call-1"


@pytest.mark.asyncio
async def test_outgoing_native_call_notifies_connection_without_incoming_ring(fixture, monkeypatch):
    f = fixture
    manager = phones.CompanionPhones(f.hass, SimpleNamespace())
    manager.bindings[f.binding.endpoint_id] = f.binding
    manager._notify = AsyncMock()
    tasks = []
    monkeypatch.setattr(phones, "call_registry", lambda hass: SimpleNamespace(current_generation=lambda c: 7))
    monkeypatch.setattr(phones, "create_runtime_task", lambda hass, coro: tasks.append(asyncio.create_task(coro)))
    for phase in ("calling", "remote_ringing", "in_call", "in_call", "idle"):
        manager._call_changed(SimpleNamespace(data={"endpoint_id": f.binding.endpoint_id,
            "call_id": "call-1", "state": phase, "direction": "outgoing", "callee": "Office"}))
    await asyncio.gather(*tasks)
    assert [call.args[2] for call in manager._notify.await_args_list] == ["refresh", "cancel"]
    assert manager.invitations == {}


@pytest.mark.asyncio
async def test_old_terminal_event_cannot_cancel_a_new_native_invitation(fixture, monkeypatch):
    f = fixture
    manager = phones.CompanionPhones(f.hass, SimpleNamespace())
    manager.bindings[f.binding.endpoint_id] = f.binding
    manager.invitations[f.binding.endpoint_id] = f.invitation
    manager._notify = AsyncMock()
    manager._call_changed(SimpleNamespace(data={"endpoint_id": f.binding.endpoint_id,
        "call_id": "old-call", "state": "idle"}))
    assert manager.invitations[f.binding.endpoint_id] == f.invitation
    manager._notify.assert_not_called()


@pytest.mark.asyncio
async def test_connected_outgoing_descriptor_uses_existing_media_transport(fixture):
    f = fixture
    f.snapshot.update(state="in_call", direction="outgoing", callee="Office")
    response = await views.CompanionCallView().get(f.request)
    import json
    description = json.loads(response.text)
    assert description["direction"] == "outgoing"
    assert description["caller"] == "Office"
    path = urlsplit(description["media_path"])
    assert path.path == "/api/voip_stack/ws"
    assert parse_qs(path.query)["endpoint_id"] == [f.binding.endpoint_id]
    assert parse_qs(path.query)["call_id"] == [f.token.call_id]


@pytest.mark.asyncio
async def test_native_invitation_without_domain_deadline_does_not_invent_ring_timeout(fixture):
    f = fixture
    from dataclasses import replace
    f.manager.invitations[f.binding.endpoint_id] = replace(f.invitation, expires=None)
    response = await views.CompanionCallView().get(f.request)
    import json
    assert json.loads(response.text)["remaining_ms"] is None
