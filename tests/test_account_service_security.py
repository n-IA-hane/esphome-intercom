#!/usr/bin/env python3
"""Behavioral regression tests for private SIP account service responses."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, asdict
import importlib.util
from pathlib import Path
import sys
import types


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "custom_components" / "voip_stack" / "account_services.py"
PACKAGE = "voip_stack_account_security_test"
GENERATED_SECRET = "generated-secret-MUST-NOT-BE-PUBLISHED"


@dataclass
class _SipAccount:
    username: str
    display_name: str
    password: str
    enabled: bool
    extension: str
    conference_group: str
    conference_ring: bool
    ring_group: str


def _load_account_services(monkeypatch, notifications: list[str], events: list[dict]):
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(MODULE.parent)]
    monkeypatch.setitem(sys.modules, PACKAGE, package)

    homeassistant = types.ModuleType("homeassistant")
    homeassistant.__path__ = []
    components = types.ModuleType("homeassistant.components")
    components.__path__ = []
    persistent = types.ModuleType("homeassistant.components.persistent_notification")
    persistent.async_create = lambda _hass, message, **_kwargs: notifications.append(
        str(message)
    )
    components.persistent_notification = persistent
    core = types.ModuleType("homeassistant.core")
    core.ServiceCall = object
    exceptions = types.ModuleType("homeassistant.exceptions")
    exceptions.ServiceValidationError = ValueError
    monkeypatch.setitem(sys.modules, "homeassistant", homeassistant)
    monkeypatch.setitem(sys.modules, "homeassistant.components", components)
    monkeypatch.setitem(
        sys.modules, "homeassistant.components.persistent_notification", persistent
    )
    monkeypatch.setitem(sys.modules, "homeassistant.core", core)
    monkeypatch.setitem(sys.modules, "homeassistant.exceptions", exceptions)

    const = types.ModuleType(f"{PACKAGE}.const")
    const.DOMAIN = "voip_stack"
    registrar = types.ModuleType(f"{PACKAGE}.sip_registrar")
    registrar.SipAccount = _SipAccount
    registrar.dump_account = asdict
    registrar.generate_password = lambda: GENERATED_SECRET
    registrar.normalize_username = lambda value: str(value).strip()
    store = types.ModuleType(f"{PACKAGE}.store")
    store.sip_account_dicts = lambda hass: [dict(item) for item in hass.accounts]
    store.update_sip_accounts = lambda hass, accounts: setattr(
        hass, "accounts", [dict(item) for item in accounts]
    )
    runtime_data = types.ModuleType(f"{PACKAGE}.runtime_data")
    runtime_data.sip_registrar = lambda hass: hass.data.get("voip_stack", {}).get(
        "sip_registrar"
    )
    websocket = types.ModuleType(f"{PACKAGE}.websocket_api")
    websocket._fire_call_event = lambda _hass, payload, _scope: events.append(
        dict(payload)
    )
    for dependency in (const, registrar, runtime_data, store, websocket):
        monkeypatch.setitem(sys.modules, dependency.__name__, dependency)

    name = f"{PACKAGE}.account_services"
    spec = importlib.util.spec_from_file_location(name, MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def test_generated_credentials_are_returned_once_and_never_published(monkeypatch) -> None:
    notifications: list[str] = []
    events: list[dict] = []
    module = _load_account_services(monkeypatch, notifications, events)

    async def refresh(_hass) -> None:
        return None

    handlers = module.build_account_service_handlers(refresh)
    hass = types.SimpleNamespace(accounts=[], data={"voip_stack": {}})
    create = types.SimpleNamespace(
        hass=hass,
        data={"username": "428", "display_name": "Test endpoint"},
    )
    created = asyncio.run(handlers["create_account"](create))

    assert created == {
        "username": "428",
        "display_name": "Test endpoint",
        "password_generated": True,
        "password": GENERATED_SECRET,
    }
    assert hass.accounts[0]["password"] == GENERATED_SECRET
    assert notifications == []
    assert GENERATED_SECRET not in repr(events)

    rotate = types.SimpleNamespace(hass=hass, data={"username": "428"})
    rotated = asyncio.run(handlers["rotate_account_password"](rotate))
    assert rotated == {"username": "428", "password": GENERATED_SECRET}
    assert notifications == []
    assert GENERATED_SECRET not in repr(events)


def test_manual_password_is_never_echoed_and_account_listing_is_redacted(
    monkeypatch,
) -> None:
    notifications: list[str] = []
    events: list[dict] = []
    module = _load_account_services(monkeypatch, notifications, events)

    async def refresh(_hass) -> None:
        return None

    handlers = module.build_account_service_handlers(refresh)
    hass = types.SimpleNamespace(accounts=[], data={"voip_stack": {}})
    create = types.SimpleNamespace(
        hass=hass,
        data={
            "username": "427",
            "display_name": "Private endpoint",
            "password": "manual-secret-MUST-NOT-BE-ECHOED",
        },
    )
    created = asyncio.run(handlers["create_account"](create))
    assert created == {
        "username": "427",
        "display_name": "Private endpoint",
        "password_generated": False,
    }

    listed = asyncio.run(
        handlers["list_accounts"](types.SimpleNamespace(hass=hass, data={}))
    )
    assert listed["accounts"][0]["username"] == "427"
    assert "password" not in listed["accounts"][0]
    assert "manual-secret" not in repr(listed)
    assert notifications == []
    assert events == []
