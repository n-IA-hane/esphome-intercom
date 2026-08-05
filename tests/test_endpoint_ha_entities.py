#!/usr/bin/env python3
"""HA Device Registry contracts for logical phone endpoints."""

from __future__ import annotations

import asyncio
from enum import StrEnum
import importlib.util
from pathlib import Path
import sys
import types


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.voip_stack"
PKG_DIR = ROOT / "custom_components" / "voip_stack"


def _install_ha_fakes() -> None:
    ha = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
    ha.__path__ = []
    components = sys.modules.setdefault(
        "homeassistant.components", types.ModuleType("homeassistant.components")
    )
    components.__path__ = []
    switch_component = sys.modules.setdefault(
        "homeassistant.components.switch",
        types.ModuleType("homeassistant.components.switch"),
    )
    switch_component.SwitchEntity = type("SwitchEntity", (), {})
    text_component = sys.modules.setdefault(
        "homeassistant.components.text",
        types.ModuleType("homeassistant.components.text"),
    )
    text_component.TextEntity = type("TextEntity", (), {})
    text_component.TextMode = types.SimpleNamespace(TEXT="text")
    helpers = sys.modules.setdefault(
        "homeassistant.helpers", types.ModuleType("homeassistant.helpers")
    )
    helpers.__path__ = []

    config_entries = sys.modules.setdefault(
        "homeassistant.config_entries", types.ModuleType("homeassistant.config_entries")
    )
    config_entries.ConfigEntry = type("ConfigEntry", (), {})
    config_entries.ConfigSubentry = type("ConfigSubentry", (), {})
    core = sys.modules.setdefault(
        "homeassistant.core", types.ModuleType("homeassistant.core")
    )
    core.HomeAssistant = type("HomeAssistant", (), {})
    core.callback = lambda fn: fn

    device_registry = sys.modules.setdefault(
        "homeassistant.helpers.device_registry",
        types.ModuleType("homeassistant.helpers.device_registry"),
    )

    class DeviceEntryType(StrEnum):
        SERVICE = "service"

    device_registry.DeviceEntry = type("DeviceEntry", (), {})
    device_registry.DeviceEntryType = DeviceEntryType
    device_registry.DeviceInfo = lambda **kwargs: kwargs
    device_registry.async_get = lambda _hass: None

    entity = sys.modules.setdefault(
        "homeassistant.helpers.entity", types.ModuleType("homeassistant.helpers.entity")
    )
    entity.Entity = type("Entity", (), {})
    entity_registry = sys.modules.setdefault(
        "homeassistant.helpers.entity_registry",
        types.ModuleType("homeassistant.helpers.entity_registry"),
    )
    entity_registry.async_get = lambda _hass: None
    entity_platform = sys.modules.setdefault(
        "homeassistant.helpers.entity_platform",
        types.ModuleType("homeassistant.helpers.entity_platform"),
    )
    entity_platform.AddConfigEntryEntitiesCallback = object


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
    spec = importlib.util.spec_from_file_location(full_name, PKG_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


phone_endpoint = _load("phone_endpoint")
endpoint_registry = _load("endpoint_registry")
endpoint_device = _load("endpoint_device")
entity_manager = _load("endpoint_entity_manager")
endpoint_switch = _load("switch")
endpoint_text = _load("text")


def _endpoint(**changes):
    values = {
        "endpoint_id": "kitchen",
        "name": "Kitchen",
        "kind": phone_endpoint.EndpointKind.BROWSER,
        "availability": phone_endpoint.EndpointAvailability.AVAILABLE,
        "capabilities": {"audio", "video"},
    }
    values.update(changes)
    return phone_endpoint.PhoneEndpoint(**values)


def test_virtual_phone_device_is_service_owned_by_voip_stack() -> None:
    info = endpoint_device.endpoint_device_info(_endpoint())
    assert info is not None
    assert info["entry_type"].value == "service"
    assert info["identifiers"] == {
        ("voip_stack", "phone_endpoint:kitchen")
    }
    assert info["model"] == endpoint_device.BROWSER_PHONE_DEVICE_MODEL


def test_sip_account_uses_same_generic_device_model() -> None:
    info = endpoint_device.endpoint_device_info(
        _endpoint(kind=phone_endpoint.EndpointKind.SIP_ACCOUNT)
    )
    assert info is not None
    assert info["model"] == endpoint_device.SIP_ACCOUNT_DEVICE_MODEL
    assert "mobotix" not in repr(info).lower()
    assert "zoiper" not in repr(info).lower()


def test_esphome_endpoint_reuses_device_without_adopting_it() -> None:
    existing = types.SimpleNamespace(id="esp-device")

    class DeviceRegistry:
        create_calls = 0

        def async_get(self, device_id):
            return existing if device_id == "esp-device" else None

        def async_get_or_create(self, **kwargs):
            self.create_calls += 1
            raise AssertionError("ESPHome device must not be adopted")

    devices = DeviceRegistry()
    endpoint_device.dr.async_get = lambda _hass: devices
    endpoint = _endpoint(
        kind=phone_endpoint.EndpointKind.ESPHOME,
        device_id="esp-device",
    )
    result = endpoint_device.async_ensure_endpoint_device(
        object(), types.SimpleNamespace(entry_id="voip-entry"), endpoint
    )
    assert result is existing
    assert devices.create_calls == 0
    assert endpoint_device.endpoint_device_info(endpoint) is None


def test_managed_device_id_is_written_back_to_registry() -> None:
    device = types.SimpleNamespace(id="ha-device")

    class DeviceRegistry:
        def async_get_or_create(self, **kwargs):
            assert kwargs["config_entry_id"] == "voip-entry"
            assert kwargs["config_subentry_id"] == "sub-kitchen"
            return device

    endpoint_device.dr.async_get = lambda _hass: DeviceRegistry()
    registry = endpoint_registry.EndpointRegistry()
    registry.register(_endpoint())
    endpoint_device.async_ensure_endpoint_device(
        types.SimpleNamespace(
            data={"voip_stack": {"endpoint_subentry_ids": {"kitchen": "sub-kitchen"}}}
        ),
        types.SimpleNamespace(entry_id="voip-entry"),
        registry.require("kitchen"),
        registry,
    )
    assert registry.require("kitchen").device_id == "ha-device"


def test_call_event_routes_only_to_involved_endpoint() -> None:
    registry = endpoint_registry.EndpointRegistry()
    registry.register(_endpoint(device_id="ha-device", extension="401"))
    endpoint = registry.require("kitchen")
    assert entity_manager.event_matches_endpoint(
        {"source_endpoint_id": "kitchen"}, endpoint, registry
    )
    assert entity_manager.event_matches_endpoint(
        {"target_device_id": "ha-device"}, endpoint, registry
    )
    assert entity_manager.event_matches_endpoint(
        {"participant_endpoint_ids": ["hall", "kitchen"]}, endpoint, registry
    )
    assert not entity_manager.event_matches_endpoint(
        {"caller": "Kitchen", "callee": "401"}, endpoint, registry
    )
    assert not entity_manager.event_matches_endpoint(
        {"target_endpoint_id": "office", "callee": "402"}, endpoint, registry
    )


def test_default_phone_requires_real_endpoint_or_device_identity() -> None:
    endpoint = _endpoint(endpoint_id="default", device_id="device-casa")

    assert not entity_manager.event_matches_endpoint(
        {"scope": "session", "device_id": "__voip_stack_ha_softphone__"},
        endpoint,
        owner_scoped=True,
    )
    assert not entity_manager.event_matches_endpoint(
        {"scope": "session"},
        endpoint,
    )
    assert entity_manager.event_matches_endpoint(
        {"scope": "session", "device_id": "device-casa"},
        endpoint,
        owner_scoped=True,
    )


def test_owner_scoped_session_event_does_not_cross_local_phone_legs() -> None:
    registry = endpoint_registry.EndpointRegistry()
    registry.register(_endpoint(device_id="kitchen-device"))
    office = _endpoint(
        endpoint_id="office",
        name="Office",
        device_id="office-device",
    )
    registry.register(office)
    payload = {
        "scope": "session",
        "endpoint_id": "kitchen",
        "device_id": "kitchen-device",
        "source_endpoint_id": "office",
        "dest_endpoint_id": "kitchen",
        "participant_endpoint_ids": ["office", "kitchen"],
        "state": "ringing",
    }

    assert entity_manager.event_matches_endpoint(
        payload,
        registry.require("kitchen"),
        registry,
        owner_scoped=True,
    )
    assert not entity_manager.event_matches_endpoint(
        payload,
        registry.require("office"),
        registry,
        owner_scoped=True,
    )
    # Call-level bridge/DTMF events may still intentionally select every
    # involved endpoint when they are not a per-phone state projection.
    assert entity_manager.event_matches_endpoint(
        payload,
        registry.require("office"),
        registry,
    )


def test_only_session_projection_can_change_a_phone_entity_state() -> None:
    registry = endpoint_registry.EndpointRegistry()
    registry.register(_endpoint(device_id="kitchen-device"))
    endpoint = registry.require("kitchen")
    bridge_event = {
        "scope": "sip_bridge",
        "endpoint_id": "kitchen",
        "device_id": "kitchen-device",
        "call_id": "call-1",
        "state": "connecting",
        "automation_control": "routable",
    }
    session_event = {
        **bridge_event,
        "scope": "session",
        "state": "ringing",
    }

    assert not entity_manager.event_projects_endpoint_state(
        bridge_event,
        endpoint,
        registry,
    )
    assert entity_manager.event_projects_endpoint_state(
        session_event,
        endpoint,
        registry,
    )


def test_public_attributes_exclude_live_media_diagnostics() -> None:
    attributes = endpoint_device.endpoint_public_attributes(
        _endpoint(extension="401")
    )
    assert attributes == {
        "endpoint_id": "kitchen",
        "endpoint_kind": "browser",
        "extension": "401",
        "capabilities": ["audio", "video"],
    }
    assert not ({"sdp", "rtp", "sip_uri", "contact"} & attributes.keys())


def test_call_state_attributes_expose_stable_routing_origin() -> None:
    attributes = endpoint_device.endpoint_call_state_attributes(
        _endpoint(extension="401"),
        {
            "call_id": "call-1",
            "direction": "incoming",
            "ingress": "trunk",
            "origin": "trunk",
            "caller": "",
            "peer_name": "Daniele",
            "terminal_reason": "",
            "sdp": "must-not-leak",
        },
        extra_keys=("origin", "caller"),
        include_empty=True,
    )

    assert attributes["endpoint_id"] == "kitchen"
    assert attributes["call_id"] == "call-1"
    assert attributes["direction"] == "incoming"
    assert attributes["ingress"] == "trunk"
    assert attributes["origin"] == "trunk"
    assert attributes["caller"] == ""
    assert attributes["peer_name"] == "Daniele"
    assert attributes["terminal_reason"] == ""
    assert "sdp" not in attributes


def test_dynamic_entity_creation_is_not_reentered_by_device_id_update(
    monkeypatch,
) -> None:
    """Writing the HA device id back to the registry must add one entity."""
    registry = endpoint_registry.EndpointRegistry()
    hass = types.SimpleNamespace(
        data={"voip_stack": {"endpoint_registry": registry}}
    )
    unload_callbacks = []
    entry = types.SimpleNamespace(
        async_on_unload=unload_callbacks.append,
        runtime_data=types.SimpleNamespace(endpoints=registry),
    )
    added: list[tuple[list[object], str | None]] = []
    created: list[str] = []

    def async_add_entities(entities, *args, config_subentry_id=None):
        added.append((list(entities), config_subentry_id))

    def ensure_device(_hass, _entry, endpoint, endpoint_registry):
        endpoint_registry.update(endpoint.endpoint_id, device_id="ha-device")

    def factory(_hass, endpoint, _registry):
        created.append(endpoint.endpoint_id)
        return types.SimpleNamespace(hass=None)

    monkeypatch.setattr(
        entity_manager, "async_ensure_endpoint_device", ensure_device
    )
    manager = entity_manager.EndpointEntityManager(
        hass,
        entry,
        async_add_entities,
        factory,
    )
    manager.async_setup()
    registry.register(_endpoint())

    assert created == ["kitchen"]
    assert len(added) == 1
    assert len(added[0][0]) == 1
    assert manager.entities["kitchen"] is added[0][0][0]


def test_entity_manager_predicate_excludes_unsupported_phone_kind(monkeypatch) -> None:
    registry = endpoint_registry.EndpointRegistry()
    hass = types.SimpleNamespace(
        data={"voip_stack": {"endpoint_registry": registry}}
    )
    entry = types.SimpleNamespace(
        async_on_unload=lambda _callback: None,
        runtime_data=types.SimpleNamespace(endpoints=registry),
    )
    added = []
    monkeypatch.setattr(
        entity_manager,
        "async_ensure_endpoint_device",
        lambda _hass, _entry, endpoint, _registry: endpoint,
    )
    manager = entity_manager.EndpointEntityManager(
        hass,
        entry,
        lambda entities, *args, **kwargs: added.extend(entities),
        lambda _hass, endpoint, _registry: types.SimpleNamespace(
            endpoint=endpoint, hass=None
        ),
        predicate=lambda endpoint: endpoint.kind is phone_endpoint.EndpointKind.BROWSER,
    )
    manager.async_setup()

    registry.register(
        _endpoint(endpoint_id="browser", kind=phone_endpoint.EndpointKind.BROWSER)
    )
    registry.register(
        _endpoint(
            endpoint_id="sip",
            name="Desk SIP",
            kind=phone_endpoint.EndpointKind.SIP_ACCOUNT,
        )
    )

    assert [entity.endpoint.endpoint_id for entity in added] == ["browser"]


def test_browser_phone_setting_entities_share_the_service_settings_writer(
    monkeypatch,
) -> None:
    registry = endpoint_registry.EndpointRegistry()
    endpoint = _endpoint(
        extension="401",
        ring_group="Home",
        conference_group="Family",
        conference_ring=False,
    )
    registry.register(endpoint)
    calls: list[dict[str, object]] = []
    fake_websocket = types.ModuleType(f"{PKG_NAME}.websocket_api")

    async def async_set_ha_softphone_settings(_hass, **settings):
        calls.append(settings)

    fake_websocket.async_set_ha_softphone_settings = async_set_ha_softphone_settings
    monkeypatch.setitem(sys.modules, f"{PKG_NAME}.websocket_api", fake_websocket)

    setting = endpoint_text._PhoneTextSetting(
        "ring_group", "phone_endpoint_ring_group", "mdi:phone-ring", 255
    )
    text_entity = endpoint_text.PhoneEndpointSettingText(
        None, endpoint, registry, setting=setting
    )
    assert text_entity._attr_icon == "mdi:phone-ring"
    text_entity.hass = object()
    asyncio.run(text_entity.async_set_value("Home, Upstairs"))

    conference_switch = endpoint_switch.PhoneEndpointConferenceRingSwitch(
        None, endpoint, registry
    )
    assert conference_switch._attr_icon == "mdi:phone-in-talk"
    conference_switch.hass = text_entity.hass
    asyncio.run(conference_switch.async_turn_on())

    auto_answer_switch = endpoint_switch.PhoneEndpointAutoAnswerSwitch(
        None, endpoint, registry
    )
    auto_answer_switch.hass = text_entity.hass
    asyncio.run(auto_answer_switch.async_turn_on())

    send_video_switch = endpoint_switch.PhoneEndpointSendVideoSwitch(
        None, endpoint, registry
    )
    send_video_switch.hass = text_entity.hass
    asyncio.run(send_video_switch.async_turn_on())

    assert calls == [
        {"endpoint_id": "kitchen", "ring_group": "Home, Upstairs"},
        {"endpoint_id": "kitchen", "conference_ring": True},
        {"endpoint_id": "kitchen", "auto_answer": True},
        {"endpoint_id": "kitchen", "send_video": True},
    ]


def test_manager_bucket_unload_callback_returns_none() -> None:
    """HA unload callbacks must never leak the removed manager as a job."""
    callbacks = []
    managers = {}
    entry = types.SimpleNamespace(
        async_on_unload=callbacks.append,
        runtime_data=types.SimpleNamespace(entity_managers=managers),
    )
    manager = object()

    entity_manager.register_endpoint_entity_manager(
        entry, "endpoint_manager", manager
    )

    assert managers["endpoint_manager"] is manager
    assert callbacks[0]() is None
    assert "endpoint_manager" not in managers


def test_dynamic_entities_leave_hass_ownership_to_entity_platform() -> None:
    """Disabled or not-yet-added entities must retain HA's ``hass=None`` state."""
    modules_and_classes = {
        "binary_sensor.py": "PhoneEndpointConnectivityBinarySensor",
        "event.py": "PhoneEndpointCallEvent",
        "sensor.py": "PhoneEndpointCallStateSensor",
        "switch.py": "PhoneEndpointDndSwitch",
    }
    for filename, class_name in modules_and_classes.items():
        source = (PKG_DIR / filename).read_text(encoding="utf-8")
        class_body = source.split(f"class {class_name}", 1)[1]
        next_class = class_body.find("\nclass ")
        if next_class >= 0:
            class_body = class_body[:next_class]
        assert "self.hass = hass" not in class_body


def test_sip_account_dnd_is_persisted_without_transport_hook(monkeypatch) -> None:
    registry = endpoint_registry.EndpointRegistry()
    registry.register(
        _endpoint(kind=phone_endpoint.EndpointKind.SIP_ACCOUNT, dnd=False)
    )
    hass = types.SimpleNamespace(
        data={"voip_stack": {"endpoint_registry": registry}}
    )
    entry = object()
    persisted: list[tuple[object, object, str, dict[str, bool]]] = []
    fake_phone_config = types.ModuleType(f"{PKG_NAME}.phone_config")
    fake_phone_config.CONF_PHONE_DND = "dnd"
    fake_phone_config.update_phone_subentry = (
        lambda received_hass, received_entry, endpoint_id, updates: persisted.append(
            (received_hass, received_entry, endpoint_id, updates)
        )
    )
    fake_store = types.ModuleType(f"{PKG_NAME}.store")
    fake_store.config_entry = lambda _hass: entry
    monkeypatch.setitem(
        sys.modules, f"{PKG_NAME}.phone_config", fake_phone_config
    )
    monkeypatch.setitem(sys.modules, f"{PKG_NAME}.store", fake_store)

    asyncio.run(endpoint_switch.async_set_endpoint_dnd(hass, "kitchen", True))

    assert persisted == [(hass, entry, "kitchen", {"dnd": True})]
    assert registry.require("kitchen").dnd is True


def test_endpoint_reconfigure_refreshes_device_and_entity(monkeypatch) -> None:
    registry = endpoint_registry.EndpointRegistry()
    registry.register(_endpoint())
    hass = types.SimpleNamespace(
        data={"voip_stack": {"endpoint_registry": registry}}
    )
    entry = types.SimpleNamespace(
        async_on_unload=lambda _callback: None,
        runtime_data=types.SimpleNamespace(endpoints=registry),
    )
    ensured_names: list[str] = []

    class FakeEntity:
        hass = None

        def __init__(self, endpoint):
            self.endpoint = endpoint

        def apply_endpoint(self, endpoint):
            self.endpoint = endpoint

    def ensure_device(_hass, _entry, endpoint, endpoint_registry):
        ensured_names.append(endpoint.name)
        if not endpoint.device_id:
            endpoint_registry.update(endpoint.endpoint_id, device_id="ha-device")

    monkeypatch.setattr(
        entity_manager,
        "async_ensure_endpoint_device",
        ensure_device,
    )
    manager = entity_manager.EndpointEntityManager(
        hass,
        entry,
        lambda _entities, *args, **kwargs: None,
        lambda _hass, endpoint, _registry: FakeEntity(endpoint),
    )
    manager.async_setup()

    registry.update("kitchen", name="Kitchen wall")
    registry.update(
        "kitchen", availability=phone_endpoint.EndpointAvailability.OFFLINE
    )

    assert ensured_names == ["Kitchen", "Kitchen wall"]
    assert manager.entities["kitchen"].endpoint.name == "Kitchen wall"
    assert (
        manager.entities["kitchen"].endpoint.availability
        is phone_endpoint.EndpointAvailability.OFFLINE
    )
