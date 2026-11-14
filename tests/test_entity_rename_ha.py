"""Entity Registry rename regressions using Home Assistant's real lifecycle."""

from datetime import timedelta
from pathlib import Path
from collections.abc import Iterator
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock
from homeassistant.helpers.entity import Entity

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.exceptions import Unauthorized
from pytest_homeassistant_custom_component.common import MockConfigEntry

pytestmark = pytest.mark.ha


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations) -> Iterator[None]:
    import custom_components

    original = custom_components.__path__
    custom_components.__path__ = [
        *original,
        str(Path(__file__).parents[1] / "custom_components"),
    ]
    yield
    custom_components.__path__ = original


@pytest.mark.parametrize(
    "kind",
    ["connectivity", "state", "event", "dnd", "conference", "answer", "video", "text"],
)
async def test_phone_entity_rename_preserves_identity(
    hass: HomeAssistant, kind: str
) -> None:
    from custom_components.voip_stack.binary_sensor import (
        PhoneEndpointConnectivityBinarySensor,
    )
    from custom_components.voip_stack.sensor import PhoneEndpointCallStateSensor
    from custom_components.voip_stack.event import PhoneEndpointCallEvent
    from custom_components.voip_stack.switch import (
        PhoneEndpointDndSwitch,
        PhoneEndpointConferenceRingSwitch,
        PhoneEndpointAutoAnswerSwitch,
        PhoneEndpointSendVideoSwitch,
    )
    from custom_components.voip_stack.text import PhoneEndpointSettingText, _SETTINGS
    from custom_components.voip_stack.endpoint_registry import EndpointRegistry
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
    from custom_components.voip_stack.authorization import _require_endpoint_permission

    endpoint = PhoneEndpoint(
        endpoint_id="desk",
        name="Desk",
        kind=EndpointKind.SIP_ACCOUNT,
        entity_ids=frozenset({"sensor.sibling"}),
        active_call_id="ongoing",
    )
    registry = EndpointRegistry()
    registry.register(endpoint)
    factories = {
        "connectivity": ("binary_sensor", PhoneEndpointConnectivityBinarySensor),
        "state": ("sensor", PhoneEndpointCallStateSensor),
        "event": ("event", PhoneEndpointCallEvent),
        "dnd": ("switch", PhoneEndpointDndSwitch),
        "conference": ("switch", PhoneEndpointConferenceRingSwitch),
        "answer": ("switch", PhoneEndpointAutoAnswerSwitch),
        "video": ("switch", PhoneEndpointSendVideoSwitch),
        "text": (
            "text",
            lambda *args: PhoneEndpointSettingText(*args, setting=_SETTINGS[0]),
        ),
    }
    domain, factory = factories[kind]
    entity = factory(hass, endpoint, registry)
    entry = MockConfigEntry(domain="voip_stack", data={})
    entry.add_to_hass(hass)
    platform = EntityPlatform(
        hass=hass,
        logger=logging.getLogger(__name__),
        domain=domain,
        platform_name="voip_stack",
        platform=None,
        scan_interval=timedelta(seconds=30),
        entity_namespace=None,
    )
    platform.config_entry = entry
    await platform.async_add_entities([entity])
    added = AsyncMock(wraps=entity.async_added_to_hass)
    entity.async_added_to_hass = added
    original = entity.entity_id
    stable_unique_id = entity.unique_id
    # Model the synchronous manager subscriber, including its state writes.
    unsubscribe = registry.subscribe(
        lambda event: entity.apply_endpoint(event.endpoint)
    )
    try:
        previous = original
        for new in [f"{domain}.renamed_once", f"{domain}.renamed_twice", original]:
            er.async_get(hass).async_update_entity(previous, new_entity_id=new)
            await hass.async_block_till_done()
            assert entity.entity_id == new
            assert entity.unique_id == stable_unique_id
            assert hass.states.get(previous) is None
            assert hass.states.get(new) is not None
            assert registry.by_entity_id(previous) is None
            current = registry.by_entity_id(new)
            assert current.endpoint_id == "desk"
            assert current.active_call_id == "ongoing"
            assert current.entity_ids == frozenset({"sensor.sibling", new})
            user = SimpleNamespace(
                id="user",
                permissions=SimpleNamespace(
                    check_entity=lambda eid, policy: eid == new
                ),
            )
            assert (
                _require_endpoint_permission(hass, user, current, "control") == "user"
            )
            user.permissions.check_entity = lambda eid, policy: eid == previous
            with pytest.raises(Unauthorized):
                _require_endpoint_permission(hass, user, current, "control")
            previous = new
        assert added.await_count == (
            0 if hasattr(Entity, "async_entity_id_changed") else 3
        )
    finally:
        unsubscribe()
        await platform.async_reset()


async def test_shared_entity_permissions_follow_registry_not_name(
    hass: HomeAssistant,
) -> None:
    from custom_components.voip_stack.authorization import require_websocket_control
    from custom_components.voip_stack.entity_identity import (
        shared_entity_ids,
        phonebook_state,
    )

    registry = er.async_get(hass)
    event = registry.async_get_or_create(
        "event",
        "voip_stack",
        "voip_stack_call_event",
        suggested_object_id="voip_stack_call",
    )
    phonebook = registry.async_get_or_create(
        "sensor",
        "voip_stack",
        "voip_stack_phonebook",
        suggested_object_id="voip_phonebook",
    )
    registry.async_update_entity(event.entity_id, new_entity_id="event.renamed_calls")
    registry.async_update_entity(
        phonebook.entity_id, new_entity_id="sensor.renamed_contacts"
    )
    registry.async_get_or_create(
        "event", "other", "namesake", suggested_object_id="voip_stack_call"
    )
    hass.states.async_set("sensor.voip_phonebook", "0", {"roster_json": "wrong"})
    hass.states.async_set("sensor.renamed_contacts", "1", {"roster_json": "correct"})
    assert shared_entity_ids(hass) == {
        "call_event": "event.renamed_calls",
        "phonebook": "sensor.renamed_contacts",
    }
    assert phonebook_state(hass).attributes["roster_json"] == "correct"
    user = SimpleNamespace(
        id="reader",
        permissions=SimpleNamespace(
            check_entity=lambda eid, policy: eid == "event.voip_stack_call"
        ),
    )
    with pytest.raises(Unauthorized):
        require_websocket_control(hass, SimpleNamespace(user=user))
    user.permissions.check_entity = lambda eid, policy: eid == "event.renamed_calls"
    assert require_websocket_control(hass, SimpleNamespace(user=user)) == "reader"
    registry.async_remove("event.renamed_calls")
    with pytest.raises(Unauthorized):
        require_websocket_control(hass, SimpleNamespace(user=user))


async def test_esphome_rename_updates_owned_roles_without_adopting_device(
    hass: HomeAssistant,
) -> None:
    from custom_components.voip_stack.endpoint_registry import EndpointRegistry
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
    from custom_components.voip_stack.runtime_data import VoipStackRuntime
    from custom_components.voip_stack.esphome_state_bridge import (
        register_state_event_bridge,
    )
    from homeassistant.helpers import device_registry as dr

    entry = MockConfigEntry(domain="voip_stack", data={})
    entry.add_to_hass(hass)
    esp_entry = MockConfigEntry(domain="esphome", data={})
    esp_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=esp_entry.entry_id, identifiers={("esphome", "real-device")}
    )
    entity = er.async_get(hass).async_get_or_create(
        "sensor",
        "esphome",
        "device-voip_state",
        config_entry=esp_entry,
        device_id=device.id,
    )
    runtime = VoipStackRuntime(
        transport_config={},
        assist_config={},
        trunk_config={},
        endpoints=EndpointRegistry(),
        phones=SimpleNamespace(),
    )
    entry.runtime_data = runtime
    runtime.endpoints.register(
        PhoneEndpoint(
            endpoint_id=f"esphome:{device.id}",
            name="ESP",
            kind=EndpointKind.ESPHOME,
            device_id=device.id,
            entity_ids=frozenset({entity.entity_id}),
            active_call_id="current-call",
        )
    )
    register_state_event_bridge(hass)
    try:
        er.async_get(hass).async_update_entity(
            entity.entity_id, new_entity_id="sensor.renamed_esp"
        )
        await hass.async_block_till_done()
        assert runtime.endpoints.by_entity_id(entity.entity_id) is None
        assert (
            runtime.endpoints.by_entity_id("sensor.renamed_esp").active_call_id
            == "current-call"
        )
        current_device = dr.async_get(hass).async_get(device.id)
        if hasattr(current_device, "config_entry_id"):
            assert current_device.config_entry_id == esp_entry.entry_id
        else:
            assert current_device.config_entries == {esp_entry.entry_id}
    finally:
        runtime.esp_state_event_bridge_unsub()


@pytest.mark.parametrize("kind", ["event", "sensor"])
async def test_shared_entity_rename_keeps_subscriptions(
    hass: HomeAssistant, monkeypatch, kind: str
) -> None:
    from custom_components.voip_stack.event import VoipStackCallEvent
    from custom_components.voip_stack.sensor import VoipPhonebookSensor
    from custom_components.voip_stack.entity_identity import shared_entity_ids

    entry = MockConfigEntry(domain="voip_stack", data={})
    entry.add_to_hass(hass)
    if kind == "event":
        entity = VoipStackCallEvent(hass, entry)
        role = "call_event"
    else:
        entity = VoipPhonebookSensor(hass)
        monkeypatch.setattr(entity, "_refresh_tracked_entities", AsyncMock())
        role = "phonebook"
    platform = EntityPlatform(
        hass=hass,
        logger=logging.getLogger(__name__),
        domain=kind,
        platform_name="voip_stack",
        platform=None,
        scan_interval=timedelta(seconds=30),
        entity_namespace=None,
    )
    platform.config_entry = entry
    await platform.async_add_entities([entity])
    old_id = entity.entity_id
    added = AsyncMock(wraps=entity.async_added_to_hass)
    entity.async_added_to_hass = added
    listeners = hass.bus.async_listeners()
    try:
        er.async_get(hass).async_update_entity(
            old_id, new_entity_id=f"{kind}.renamed_shared"
        )
        await hass.async_block_till_done()
        assert shared_entity_ids(hass)[role] == entity.entity_id
        assert hass.states.get(old_id) is None
        assert hass.states.get(entity.entity_id) is not None
        assert hass.bus.async_listeners() == listeners
        assert added.await_count == (
            0 if hasattr(Entity, "async_entity_id_changed") else 1
        )
        # A platform reload keeps the stable registry identity and the user's name.
        await platform.async_reset()
        await platform.async_add_entities([entity])
        assert entity.entity_id == f"{kind}.renamed_shared"
        assert shared_entity_ids(hass)[role] == entity.entity_id
    finally:
        await platform.async_reset()


@pytest.mark.parametrize("domain, role", [("text", "voip_ring_groups"), ("switch", "voip_ring_on_conference")])
async def test_phonebook_tracks_renamed_esphome_roles(hass: HomeAssistant, monkeypatch, domain: str, role: str) -> None:
    from custom_components.voip_stack.sensor import VoipPhonebookSensor

    registry = er.async_get(hass)
    entity = registry.async_get_or_create(domain, "esphome", f"board-{role}",
                                          suggested_object_id="board_voip_ring_groups")
    sensor = VoipPhonebookSensor(hass)
    monkeypatch.setattr(sensor, "_schedule_and_wait_recompute", AsyncMock())
    scheduled = []
    monkeypatch.setattr(sensor, "_schedule_recompute", lambda: scheduled.append(True))
    entry = MockConfigEntry(domain="voip_stack", data={})
    entry.add_to_hass(hass)
    platform = EntityPlatform(hass=hass, logger=logging.getLogger(__name__), domain="sensor",
        platform_name="voip_stack", platform=None, scan_interval=timedelta(seconds=30), entity_namespace=None)
    platform.config_entry = entry
    await platform.async_add_entities([sensor])
    try:
        hass.states.async_set(entity.entity_id, "before")
        registry.async_update_entity(entity.entity_id, new_entity_id=f"{domain}.renamed_groups")
        await hass.async_block_till_done()
        assert sensor._tracked_entities == {f"{domain}.renamed_groups"}
        hass.states.async_set(f"{domain}.renamed_groups", "first")
        await hass.async_block_till_done()
        scheduled.clear()
        hass.states.async_set(f"{domain}.renamed_groups", "second")
        await hass.async_block_till_done()
        assert scheduled == [True]
    finally:
        await platform.async_reset()
