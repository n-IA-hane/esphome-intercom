#!/usr/bin/env python3
"""Behavioral authorization boundaries for VoIP control and media access."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import types

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "custom_components" / "voip_stack" / "authorization.py"


class _Unauthorized(Exception):
    def __init__(self, **kwargs) -> None:
        self.details = kwargs


class _UnknownUser(Exception):
    def __init__(self, **kwargs) -> None:
        self.details = kwargs


@pytest.fixture
def authorization(monkeypatch):
    homeassistant = types.ModuleType("homeassistant")
    homeassistant.__path__ = []
    exceptions = types.ModuleType("homeassistant.exceptions")
    exceptions.Unauthorized = _Unauthorized
    exceptions.UnknownUser = _UnknownUser
    monkeypatch.setitem(sys.modules, "homeassistant", homeassistant)
    monkeypatch.setitem(sys.modules, "homeassistant.exceptions", exceptions)

    package_name = "voip_stack_authorization_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(MODULE.parent)]
    monkeypatch.setitem(sys.modules, package_name, package)
    identity = types.ModuleType(f"{package_name}.entity_identity")
    identity.shared_entity_ids = lambda hass: {"call_event": "event.voip_stack_call"}
    monkeypatch.setitem(sys.modules, identity.__name__, identity)
    spec = importlib.util.spec_from_file_location(f"{package_name}.authorization", MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Permissions:
    def __init__(self, allowed: set[str]) -> None:
        self.allowed = allowed
        self.checks: list[tuple[str, str]] = []

    def check_entity(self, entity_id: str, policy: str) -> bool:
        self.checks.append((entity_id, policy))
        return policy in self.allowed


class _User:
    def __init__(self, user_id: str, *, allowed: set[str], admin: bool = False) -> None:
        self.id = user_id
        self.is_admin = admin
        self.permissions = _Permissions(allowed)


class _EntityPermissions:
    def __init__(self, allowed: set[tuple[str, str]]) -> None:
        self.allowed = allowed

    def check_entity(self, entity_id: str, policy: str) -> bool:
        return (entity_id, policy) in self.allowed


class _Registry:
    """Spy for the session lookup and sticky controller storage contract."""

    def __init__(self, controller_user_id=""):
        self.session = types.SimpleNamespace(metadata={"controller_user_id": controller_user_id})
        self.sessions = {"call-1": self.session}
        self.bound = []

    def resolve_session_id(self, call_id):
        return call_id

    def bind_controller(self, call_id, *, user_id, endpoint_id=""):
        metadata = self.sessions[call_id].metadata
        if endpoint_id:
            controllers = metadata.setdefault("controller_user_ids", {})
            if controllers.get(endpoint_id) not in {None, "", user_id}:
                raise ValueError("Controller is already bound")
            controllers[endpoint_id] = user_id
        else:
            if metadata.get("controller_user_id") not in {None, "", user_id}:
                raise ValueError("Controller is already bound")
            metadata["controller_user_id"] = user_id
        self.bound.append(f"{endpoint_id}:{user_id}" if endpoint_id else user_id)


class _ScopedRegistry(_Registry):
    def __init__(self, controllers):
        super().__init__()
        self.session.metadata.update(local_bridge=True, controller_user_ids=dict(controllers))


def _service_call(user_id: str | None):
    return types.SimpleNamespace(context=types.SimpleNamespace(user_id=user_id))


def test_service_control_uses_entity_policy_and_preserves_internal_automation(
    authorization,
) -> None:
    allowed = _User("allowed", allowed={"control"})
    denied = _User("denied", allowed={"read"})
    users = {user.id: user for user in (allowed, denied)}
    hass = types.SimpleNamespace(
        auth=types.SimpleNamespace(
            async_get_user=lambda user_id: asyncio.sleep(0, result=users.get(user_id))
        )
    )

    asyncio.run(
        authorization.async_require_service_control(hass, _service_call(None))
    )
    asyncio.run(
        authorization.async_require_service_control(hass, _service_call("allowed"))
    )
    assert allowed.permissions.checks == [
        ("event.voip_stack_call", "control")
    ]
    with pytest.raises(_Unauthorized):
        asyncio.run(
            authorization.async_require_service_control(hass, _service_call("denied"))
        )
    with pytest.raises(_UnknownUser):
        asyncio.run(
            authorization.async_require_service_control(hass, _service_call("missing"))
        )


def test_admin_service_boundary_allows_only_admin_or_internal_context(
    authorization,
) -> None:
    admin = _User("admin", allowed=set(), admin=True)
    ordinary = _User("ordinary", allowed={"control"})
    users = {user.id: user for user in (admin, ordinary)}
    hass = types.SimpleNamespace(
        auth=types.SimpleNamespace(
            async_get_user=lambda user_id: asyncio.sleep(0, result=users.get(user_id))
        )
    )

    asyncio.run(authorization.async_require_service_admin(hass, _service_call(None)))
    asyncio.run(
        authorization.async_require_service_admin(hass, _service_call("admin"))
    )
    with pytest.raises(_Unauthorized):
        asyncio.run(
            authorization.async_require_service_admin(hass, _service_call("ordinary"))
        )


def test_websocket_and_http_media_require_explicit_entity_permission(
    authorization,
) -> None:
    reader = _User("reader", allowed={"read"})
    controller = _User("controller", allowed={"read", "control"})

    assert authorization.require_websocket_read(
        None, types.SimpleNamespace(user=reader)
    ) == "reader"
    with pytest.raises(_Unauthorized):
        authorization.require_websocket_control(None, types.SimpleNamespace(user=reader))
    assert authorization.require_websocket_control(
        None, types.SimpleNamespace(user=controller)
    ) == "controller"

    with pytest.raises(_Unauthorized):
        authorization.require_http_control(_Request(reader))
    assert authorization.require_http_control(_Request(controller)) == "controller"
    with pytest.raises(_Unauthorized):
        authorization.require_http_control(_Request(None))


def test_action_authorization_requires_the_exact_control_entity(
    authorization,
) -> None:
    user = types.SimpleNamespace(
        id="kiosk-user",
        is_admin=False,
        permissions=_EntityPermissions(
            {
                ("event.voip_stack_call", "control"),
                ("sensor.kitchen_phone_call_state", "control"),
            }
        ),
    )
    hass = types.SimpleNamespace(
        auth=types.SimpleNamespace(
            async_get_user=lambda _user_id: asyncio.sleep(0, result=user)
        )
    )

    with pytest.raises(_Unauthorized):
        asyncio.run(
            authorization.async_require_service_entity_control(
                hass,
                _service_call("kiosk-user"),
                ("button.kitchen_phone_answer",),
            )
        )

    user.permissions.allowed.add(("button.kitchen_phone_answer", "control"))
    asyncio.run(
        authorization.async_require_service_entity_control(
            hass,
            _service_call("kiosk-user"),
            ("button.kitchen_phone_answer",),
        )
    )

    # Internal HA automations have no user and keep their established trusted
    # service path even when the device exposes no action entity.
    asyncio.run(
        authorization.async_require_service_entity_control(
            hass,
            _service_call(None),
            (),
        )
    )


def test_media_controller_accepts_only_the_user_who_controls_the_call(
    authorization,
) -> None:
    registry = _Registry("user-a")
    user_a = _User("user-a", allowed={"control"})
    admin_b = _User("admin-b", allowed={"control"}, admin=True)
    hass = types.SimpleNamespace(data={})

    assert (
        asyncio.run(
            authorization.async_require_media_controller(
                hass, registry, "call-1", user_a
            )
        )
        == "user-a"
    )
    with pytest.raises(_Unauthorized):
        asyncio.run(
            authorization.async_require_media_controller(
                hass, registry, "call-1", admin_b
            )
        )


def test_media_controller_status_is_scoped_without_exposing_user_ids(
    authorization,
) -> None:
    registry = _ScopedRegistry({"kitchen": "user-a", "office": "user-b"})

    assert (
        authorization.media_controller_status(
            registry, "call-1", "kitchen", "user-a"
        )
        == "self"
    )
    assert (
        authorization.media_controller_status(
            registry, "call-1", "kitchen", "user-b"
        )
        == "other"
    )
    assert (
        authorization.media_controller_status(
            registry, "call-1", "hall", "user-c"
        )
        == "available"
    )
    assert (
        authorization.media_controller_status(
            registry, "missing", "kitchen", "user-a"
        )
        == "available"
    )


def test_only_admin_can_atomically_bind_media_for_an_internal_call(
    authorization,
) -> None:
    registry = _Registry()
    ordinary = _User("ordinary", allowed={"control"})
    admin = _User("admin", allowed={"control"}, admin=True)
    hass = types.SimpleNamespace(data={})

    with pytest.raises(_Unauthorized):
        asyncio.run(
            authorization.async_require_media_controller(
                hass, registry, "call-1", ordinary
            )
        )
    assert registry.bound == []
    assert (
        asyncio.run(
            authorization.async_require_media_controller(
                hass, registry, "call-1", admin
            )
        )
        == "admin"
    )
    assert registry.bound == ["admin"]


def test_local_call_media_authorization_is_scoped_to_each_phone_leg(
    authorization,
) -> None:
    registry = _ScopedRegistry({"kitchen": "user-a", "office": "user-b"})
    user_a = _User("user-a", allowed={"control"})
    user_b = _User("user-b", allowed={"control"})
    hass = types.SimpleNamespace(data={})

    assert (
        asyncio.run(
            authorization.async_require_media_controller(
                hass,
                registry,
                "call-1",
                user_a,
                endpoint_id="kitchen",
            )
        )
        == "user-a"
    )
    assert (
        asyncio.run(
            authorization.async_require_media_controller(
                hass,
                registry,
                "call-1",
                user_b,
                endpoint_id="office",
            )
        )
        == "user-b"
    )
    with pytest.raises(_Unauthorized):
        asyncio.run(
            authorization.async_require_media_controller(
                hass,
                registry,
                "call-1",
                user_a,
                endpoint_id="office",
            )
        )


def test_admin_binds_only_the_unowned_local_phone_leg(authorization) -> None:
    registry = _ScopedRegistry({"kitchen": "user-a"})
    admin = _User("admin", allowed={"control"}, admin=True)
    hass = types.SimpleNamespace(data={})

    assert (
        asyncio.run(
            authorization.async_require_media_controller(
                hass,
                registry,
                "call-1",
                admin,
                endpoint_id="office",
            )
        )
        == "admin"
    )
    assert registry.session.metadata["controller_user_ids"] == {
        "kitchen": "user-a",
        "office": "admin",
    }
    assert registry.bound == ["office:admin"]


def test_media_controller_rejects_unknown_call(authorization) -> None:
    registry = _Registry()
    registry.sessions.clear()
    hass = types.SimpleNamespace(data={})
    admin = _User("admin", allowed={"control"}, admin=True)

    with pytest.raises(_Unauthorized):
        asyncio.run(
            authorization.async_require_media_controller(
                hass, registry, "missing", admin
            )
        )


def test_all_external_surfaces_apply_authorization_and_ws_context() -> None:
    websocket = (ROOT / "custom_components" / "voip_stack" / "websocket_api.py").read_text()
    services = (ROOT / "custom_components" / "voip_stack" / "services.py").read_text()
    audio = (ROOT / "custom_components" / "voip_stack" / "audio_ws_view.py").read_text()
    video = (ROOT / "custom_components" / "voip_stack" / "video_ws_view.py").read_text()
    media_session = (
        ROOT / "custom_components" / "voip_stack" / "media_ws_session.py"
    ).read_text()

    assert 'require_websocket_read(hass, connection)' in websocket
    assert 'require_websocket_endpoint_read(hass, connection, selected_endpoint)' in websocket
    assert "require_websocket_control(hass, connection)" not in websocket
    assert "hass.services.async_call(" not in websocket
    assert "async_require_service_control" in services
    assert "async_require_service_admin" in services
    for view in (audio, video):
        assert "async_prepare_media_websocket_request(" in view
        assert "async_claimed_media_websocket(" in view
    assert "resolve_media_websocket_request(request)" in media_session
    assert "async_authorize_media_websocket_request(context, request)" in media_session
    assert "require_http_control(request)" in media_session
    assert "require_media_client_id(request)" in media_session
    assert "async_require_media_controller(" in media_session
    assert "user_id=context.user_id" in media_session
    assert "client_id=context.client_id" in media_session

    for service in ("route", "set_deadline", "cancel_deadline"):
        assert f'"{service}",' in services[
            services.index("admin_services = {") : services.index(
                "def handler_for(name: str):"
            )
        ]


class _Request(dict):
    def __init__(self, user):
        super().__init__(hass_user=user)
        self.app = {"hass": None}
