#!/usr/bin/env python3
"""Persistence and migration contracts for logical phone config subentries."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types

import pytest


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.voip_stack"
PKG_DIR = ROOT / "custom_components" / "voip_stack"


def _install_ha_fakes() -> None:
    homeassistant = sys.modules.setdefault(
        "homeassistant", types.ModuleType("homeassistant")
    )
    homeassistant.__path__ = []
    config_entries = sys.modules.setdefault(
        "homeassistant.config_entries",
        types.ModuleType("homeassistant.config_entries"),
    )
    config_entries.ConfigEntry = getattr(
        config_entries, "ConfigEntry", type("ConfigEntry", (), {})
    )
    config_entries.ConfigSubentry = getattr(
        config_entries, "ConfigSubentry", type("ConfigSubentry", (), {})
    )
    core = sys.modules.setdefault(
        "homeassistant.core", types.ModuleType("homeassistant.core")
    )
    core.HomeAssistant = getattr(
        core, "HomeAssistant", type("HomeAssistant", (), {})
    )


def _load(name: str):
    _install_ha_fakes()
    if "custom_components" not in sys.modules:
        package = types.ModuleType("custom_components")
        package.__path__ = [str(ROOT / "custom_components")]
        sys.modules["custom_components"] = package
    if PKG_NAME not in sys.modules:
        package = types.ModuleType(PKG_NAME)
        package.__path__ = [str(PKG_DIR)]
        sys.modules[PKG_NAME] = package
    full_name = f"{PKG_NAME}.{name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = importlib.util.spec_from_file_location(
        full_name, PKG_DIR / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


phone_config = _load("phone_config")
phone_endpoint = _load("phone_endpoint")
endpoint_registry = _load("endpoint_registry")


def _bind_runtime(entry, registry) -> None:
    previous = getattr(entry, "runtime_data", None)
    entry.runtime_data = types.SimpleNamespace(
        endpoints=registry,
        softphone_presence=getattr(previous, "softphone_presence", {}),
        softphones=getattr(previous, "softphones", {}),
    )


def test_removed_sip_offline_policy_falls_back_without_breaking_setup(
    caplog,
) -> None:
    entry = types.SimpleNamespace(data={})
    endpoint = phone_config.endpoint_from_data(
        entry,
        {
            phone_config.CONF_PHONE_ENDPOINT_ID: "sip:dev-upgrade",
            phone_config.CONF_PHONE_KIND: phone_endpoint.EndpointKind.SIP_ACCOUNT.value,
            phone_config.CONF_PHONE_NAME: "Dev phone",
            phone_config.CONF_PHONE_USERNAME: "dev-phone",
            phone_config.CONF_PHONE_OFFLINE_POLICY: "wait",
        },
    )

    assert endpoint.offline_policy is phone_endpoint.OfflinePolicy.UNAVAILABLE
    assert "Unsupported persisted SIP account offline policy 'wait'" in caplog.text


def test_browser_phone_persists_auto_answer_and_send_video() -> None:
    entry = types.SimpleNamespace(data={})
    endpoint = phone_config.endpoint_from_data(
        entry,
        {
            phone_config.CONF_PHONE_ENDPOINT_ID: "browser:kitchen",
            phone_config.CONF_PHONE_KIND: phone_endpoint.EndpointKind.BROWSER.value,
            phone_config.CONF_PHONE_NAME: "Kitchen",
            phone_config.CONF_PHONE_AUTO_ANSWER: True,
            phone_config.CONF_PHONE_SEND_VIDEO: True,
        },
    )

    assert endpoint.auto_answer is True
    assert endpoint.send_video is True


def test_legacy_store_fills_only_settings_missing_from_entry_options() -> None:
    raw = {
        "dnd": True,
        "extension": "401",
        "groups": {
            "ring_group": "Ground floor",
            "conference_group": "Staff",
            "conference_ring": True,
        },
    }
    overrides = phone_config.legacy_default_phone_overrides(
        raw,
        options={"ha_softphone_extension": "999"},
    )

    assert phone_config.CONF_PHONE_EXTENSION not in overrides
    assert overrides == {
        phone_config.CONF_PHONE_DND: True,
        phone_config.CONF_PHONE_RING_GROUP: "Ground floor",
        phone_config.CONF_PHONE_CONFERENCE_GROUP: "Staff",
        phone_config.CONF_PHONE_CONFERENCE_RING: True,
    }


def test_new_sip_account_identity_is_opaque_and_username_independent() -> None:
    raw = {
        "username": "desk",
        "display_name": "Desk",
        "password": "secret",
    }
    first = phone_config.sip_account_phone_data(raw)
    second = phone_config.sip_account_phone_data(raw)

    assert first[phone_config.CONF_PHONE_ENDPOINT_ID].startswith("sip:")
    assert first[phone_config.CONF_PHONE_ENDPOINT_ID] != "sip:desk"
    assert (
        first[phone_config.CONF_PHONE_ENDPOINT_ID]
        != second[phone_config.CONF_PHONE_ENDPOINT_ID]
    )
    renamed = dict(first)
    renamed[phone_config.CONF_PHONE_USERNAME] = "renamed-desk"
    assert (
        renamed[phone_config.CONF_PHONE_ENDPOINT_ID]
        == first[phone_config.CONF_PHONE_ENDPOINT_ID]
    )


def test_renamed_registered_sip_account_becomes_offline_until_new_register() -> None:
    registry = endpoint_registry.EndpointRegistry()
    registry.register(
        phone_endpoint.PhoneEndpoint(
            endpoint_id="sip:stable",
            name="Desk",
            kind=phone_endpoint.EndpointKind.SIP_ACCOUNT,
            username="desk",
            availability=phone_endpoint.EndpointAvailability.AVAILABLE,
        )
    )
    subentry = types.SimpleNamespace(
        subentry_type=phone_config.PHONE_SUBENTRY_TYPE,
        subentry_id="phone-desk",
        unique_id="phone:sip:stable",
        title="Desk",
        data={
            phone_config.CONF_PHONE_ENDPOINT_ID: "sip:stable",
            phone_config.CONF_PHONE_KIND: phone_endpoint.EndpointKind.SIP_ACCOUNT.value,
            phone_config.CONF_PHONE_NAME: "Desk",
            phone_config.CONF_PHONE_USERNAME: "renamed-desk",
            phone_config.CONF_PHONE_PASSWORD: "secret",
            phone_config.CONF_PHONE_ENABLED: True,
        },
    )
    entry = types.SimpleNamespace(
        data={},
        options={},
        subentries={subentry.subentry_id: subentry},
    )
    hass = types.SimpleNamespace(
        data={"voip_stack": {}}
    )
    _bind_runtime(entry, registry)

    phone_config.sync_registry_from_entry(hass, entry)

    renamed = registry.require("sip:stable")
    assert renamed.username == "renamed-desk"
    assert renamed.availability is phone_endpoint.EndpointAvailability.OFFLINE


def test_active_removed_endpoint_is_unavailable_until_deferred_teardown() -> None:
    registry = endpoint_registry.EndpointRegistry()
    registry.register(
        phone_endpoint.PhoneEndpoint(
            endpoint_id="kitchen",
            name="Kitchen",
            kind=phone_endpoint.EndpointKind.BROWSER,
            availability=phone_endpoint.EndpointAvailability.AVAILABLE,
            active_call_id="call-1",
        )
    )
    hass = types.SimpleNamespace(
        data={"voip_stack": {}}
    )
    entry = types.SimpleNamespace(subentries={})
    _bind_runtime(entry, registry)

    phone_config.sync_registry_from_entry(hass, entry)

    draining = registry.require("kitchen")
    assert draining.active_call_id == "call-1"
    assert (
        draining.availability
        is phone_endpoint.EndpointAvailability.UNAVAILABLE
    )
    assert registry.pending_removals == {"kitchen"}


def test_deferred_browser_removal_clears_presence_after_terminal_call() -> None:
    callbacks: list[tuple[object, tuple[object, ...]]] = []

    class Loop:
        @staticmethod
        def call_soon(callback, *args) -> None:
            callbacks.append((callback, args))

    subentry = types.SimpleNamespace(
        subentry_type=phone_config.PHONE_SUBENTRY_TYPE,
        subentry_id="phone-kitchen",
        unique_id="phone:kitchen",
        title="Kitchen",
        data={
            phone_config.CONF_PHONE_ENDPOINT_ID: "kitchen",
            phone_config.CONF_PHONE_KIND: phone_endpoint.EndpointKind.BROWSER.value,
            phone_config.CONF_PHONE_NAME: "Kitchen",
            phone_config.CONF_PHONE_ENABLED: True,
        },
    )
    presence = {"kitchen": 1}
    stores = {"kitchen": {"state": "in_call", "caller": "Door"}}
    entry = types.SimpleNamespace(
        data={},
        options={},
        subentries={subentry.subentry_id: subentry},
        runtime_data=types.SimpleNamespace(
            softphone_presence=presence,
            softphones=stores,
        ),
    )
    hass = types.SimpleNamespace(
        data={"voip_stack": {}},
        loop=Loop(),
    )
    registry = phone_config.async_setup_endpoint_registry(hass, entry)
    _bind_runtime(entry, registry)
    registry.claim_call("kitchen", "call-1")
    entry.subentries.clear()

    phone_config.sync_registry_from_entry(hass, entry)
    registry.release_call("kitchen", "call-1")

    assert len(callbacks) == 1
    callback, args = callbacks.pop()
    callback(*args)
    assert registry.get("kitchen") is None
    assert "kitchen" not in presence
    assert "kitchen" not in stores


def test_idle_browser_removal_immediately_forgets_runtime_store() -> None:
    subentry = types.SimpleNamespace(
        subentry_type=phone_config.PHONE_SUBENTRY_TYPE,
        subentry_id="phone-hall",
        unique_id="phone:hall",
        title="Hall",
        data={
            phone_config.CONF_PHONE_ENDPOINT_ID: "hall",
            phone_config.CONF_PHONE_KIND: phone_endpoint.EndpointKind.BROWSER.value,
            phone_config.CONF_PHONE_NAME: "Hall",
            phone_config.CONF_PHONE_ENABLED: True,
        },
    )
    stores = {"hall": {"state": "idle"}}
    entry = types.SimpleNamespace(
        data={},
        options={},
        subentries={subentry.subentry_id: subentry},
        runtime_data=types.SimpleNamespace(
            softphone_presence={},
            softphones=stores,
        ),
    )
    hass = types.SimpleNamespace(data={"voip_stack": {}})
    registry = phone_config.async_setup_endpoint_registry(hass, entry)
    _bind_runtime(entry, registry)
    entry.subentries.clear()

    phone_config.sync_registry_from_entry(hass, entry)

    assert registry.get("hall") is None
    assert "hall" not in stores


def test_sip_account_services_share_contact_assist_and_group_namespace() -> None:
    browser = types.SimpleNamespace(
        subentry_type=phone_config.PHONE_SUBENTRY_TYPE,
        subentry_id="phone-reception",
        unique_id="phone:reception",
        title="Reception",
        data={
            phone_config.CONF_PHONE_ENDPOINT_ID: "reception",
            phone_config.CONF_PHONE_KIND: phone_endpoint.EndpointKind.BROWSER.value,
            phone_config.CONF_PHONE_NAME: "Reception",
            phone_config.CONF_PHONE_RING_GROUP: "Staff",
            phone_config.CONF_PHONE_ENABLED: True,
        },
    )
    entry = types.SimpleNamespace(
        data={
            "phonebook_contacts": [
                {"id": "front", "name": "Front Desk", "extension": "410"}
            ],
            "assist_endpoint_enabled": True,
            "assist_extension": "999",
        },
        subentries={browser.subentry_id: browser},
    )

    base = {
        phone_config.CONF_PHONE_ENDPOINT_ID: "sip:stable",
        phone_config.CONF_PHONE_KIND: phone_endpoint.EndpointKind.SIP_ACCOUNT.value,
        phone_config.CONF_PHONE_NAME: "Desk phone",
        phone_config.CONF_PHONE_USERNAME: "desk",
        phone_config.CONF_PHONE_PASSWORD: "secret",
        phone_config.CONF_PHONE_ENABLED: True,
    }
    # Reusing a group route for another member is intentional.
    phone_config.validate_sip_account_namespace(
        entry,
        [dict(base, ring_group="Staff")],
    )

    for changes in (
        {phone_config.CONF_PHONE_USERNAME: "front-desk"},
        {phone_config.CONF_PHONE_EXTENSION: "999"},
        {phone_config.CONF_PHONE_NAME: "Staff"},
        {phone_config.CONF_PHONE_RING_GROUP: "Reception"},
    ):
        with pytest.raises(ValueError, match="conflicts"):
            phone_config.validate_sip_account_namespace(
                entry,
                [dict(base, **changes)],
            )
