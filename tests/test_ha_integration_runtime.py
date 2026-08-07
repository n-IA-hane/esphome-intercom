"""Home Assistant level service tests through supported public APIs."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from homeassistant import loader
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry


DOMAIN = "voip_stack"
INTEGRATION_DEPENDENCIES = {
    "assist_pipeline",
    "http",
    "lovelace",
    "network",
}
pytestmark = pytest.mark.ha


def _prepare_integration_dependencies(hass: HomeAssistant) -> None:
    """Provide dependency surfaces without starting unrelated HA services."""
    hass.config.components.update(INTEGRATION_DEPENDENCIES)
    hass.http = MagicMock()
    hass.http.async_register_static_paths = AsyncMock()


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations) -> Iterator[None]:
    """Allow loading the checked-out custom integration."""
    import custom_components

    component_root = str(Path(__file__).parents[1] / "custom_components")
    original_path = custom_components.__path__
    custom_components.__path__ = [*original_path, component_root]
    yield
    custom_components.__path__ = original_path


async def test_physical_phone_call_response_uses_the_selected_esp_endpoint(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    components = await loader.async_get_custom_components(hass)
    assert DOMAIN in components
    _prepare_integration_dependencies(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    from custom_components import voip_stack

    service_response = {
        "schema_version": 2,
        "success": True,
        "operation": "originate",
        "phone": {
            "device_id": "device-kitchen",
            "kind": "esphome",
            "name": "Waveshare P4 Touch",
        },
        "call": {
            "call_id": "",
            "state": "accepted",
            "destination": "Casa",
        },
    }
    action_result = MagicMock()
    action_result.as_service_response.return_value = service_response
    originate = AsyncMock(return_value=action_result)
    monkeypatch.setattr(voip_stack, "_originate_phone_action", originate)

    response = await hass.services.async_call(
        DOMAIN,
        "call",
        {
            "device_id": "device-kitchen",
            "destination": "Casa",
            "send_video": True,
        },
        blocking=True,
        return_response=True,
    )

    assert response == service_response
    originate.assert_awaited_once()
    action_result.as_service_response.assert_called_once_with()


async def test_default_browser_call_response_does_not_require_an_open_card(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_integration_dependencies(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    from custom_components import voip_stack

    action_result = MagicMock()
    action_result.as_service_response.return_value = {
        "schema_version": 2,
        "success": True,
    }
    originate = AsyncMock(return_value=action_result)
    monkeypatch.setattr(voip_stack, "_originate_phone_action", originate)

    response = await hass.services.async_call(
        DOMAIN,
        "call",
        {"destination": "427"},
        blocking=True,
        return_response=True,
    )

    assert response == {"schema_version": 2, "success": True}
    originate.assert_awaited_once()


async def test_transfer_service_returns_the_refer_subscription_result(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_integration_dependencies(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    from custom_components import voip_stack
    from custom_components.voip_stack import call_transfer
    from custom_components.voip_stack.sip_client import SipTransferResult

    phones = SimpleNamespace(resolve_source=AsyncMock())
    monkeypatch.setattr(
        voip_stack,
        "_runtime_data",
        lambda _hass: SimpleNamespace(phones=phones),
    )
    transfer = AsyncMock(
        return_value=SipTransferResult(True, 200, "completed")
    )
    monkeypatch.setattr(call_transfer, "async_transfer_call", transfer)

    response = await hass.services.async_call(
        DOMAIN,
        "transfer",
        {
            "device_id": "phone-device",
            "call_id": "active-call",
            "destination": "427",
        },
        blocking=True,
        return_response=True,
    )

    assert response == {
        "schema_version": 1,
        "success": True,
        "call_id": "active-call",
        "destination": "427",
        "replaces_call_id": "",
        "status": 200,
        "state": "completed",
    }
    phones.resolve_source.assert_awaited_once()
    transfer.assert_awaited_once()


async def test_default_phone_call_state_keeps_its_historical_entity_id(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from homeassistant.helpers import entity_registry as er

    from custom_components.voip_stack.sensor import (
        LEGACY_DEFAULT_CALL_STATE_UNIQUE_ID,
        _migrate_default_call_state_entity,
    )

    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        LEGACY_DEFAULT_CALL_STATE_UNIQUE_ID,
        suggested_object_id="voip_stack_call_state",
        config_entry=entry,
    )
    duplicate = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "phone_endpoint_default_call_state",
        suggested_object_id="casa_stato_chiamata",
        config_entry=entry,
    )
    monkeypatch.setattr(
        "custom_components.voip_stack.sensor.endpoint_config_subentry_id",
        lambda *_args: None,
    )

    _migrate_default_call_state_entity(
        hass,
        entry,
        SimpleNamespace(endpoint_id="default", device_id=""),
    )

    migrated = registry.async_get(old.entity_id)
    assert migrated is not None
    assert migrated.unique_id == "phone_endpoint_default_call_state"
    assert migrated.entity_id == old.entity_id
    assert registry.async_get(duplicate.entity_id) is None


async def test_initial_phone_becomes_preferred_when_multiple_phones_exist() -> None:
    from custom_components.voip_stack import _preferred_phone_device_id
    from custom_components.voip_stack.endpoint_registry import EndpointRegistry
    from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint

    registry = EndpointRegistry()
    registry.register(
        PhoneEndpoint(
            endpoint_id="default",
            device_id="device-casa",
            name="Casa",
            kind=EndpointKind.BROWSER,
        )
    )
    registry.register(
        PhoneEndpoint(
            endpoint_id="browser:test",
            device_id="device-test",
            name="Test",
            kind=EndpointKind.BROWSER,
        )
    )

    assert _preferred_phone_device_id(registry, "") == "device-casa"
    assert _preferred_phone_device_id(registry, "device-test") == "device-test"


async def test_purge_devices_is_disabled_before_resolving_or_removing_devices(
    hass: HomeAssistant,
) -> None:
    _prepare_integration_dependencies(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "purge_devices",
            {},
            blocking=True,
        )


async def test_entry_runtime_exposes_configuration_without_global_mirrors(
    hass: HomeAssistant,
) -> None:
    from custom_components.voip_stack.config import (
        assist_config,
        debug_mode,
        media_capture_enabled,
        transport_config,
        trunk_config,
    )
    from custom_components.voip_stack.runtime_data import VoipStackRuntime

    runtime = VoipStackRuntime(
        transport_config={"sip_port": 5099, "rtp_port": 45000},
        assist_config={"assist_extension": "900"},
        trunk_config={"trunk_enabled": True},
        endpoints=MagicMock(),
        phones=MagicMock(),
        debug_mode=True,
    )
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    entry.runtime_data = runtime
    assert debug_mode(hass)
    assert not media_capture_enabled(hass)
    assert transport_config(hass)["sip_port"] == 5099
    assert assist_config(hass)["assist_extension"] == "900"
    assert trunk_config(hass)["trunk_enabled"] is True

    runtime.media_capture = True
    assert media_capture_enabled(hass)
    assert not {
        "transport_config",
        "assist_config",
        "trunk_config",
        "debug_mode",
        "media_capture",
    }.intersection(hass.data.get(DOMAIN, {}))


async def test_entry_runtime_owns_call_projection_and_detached_tasks(
    hass: HomeAssistant,
) -> None:
    from custom_components.voip_stack.endpoint_lifecycle import (
        call_registry,
        cancel_runtime_tasks,
        create_runtime_task,
    )
    from custom_components.voip_stack.runtime_data import VoipStackRuntime

    runtime = VoipStackRuntime(
        transport_config={},
        assist_config={},
        trunk_config={},
        endpoints=MagicMock(),
        phones=MagicMock(),
    )
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    entry.runtime_data = runtime

    registry = call_registry(hass)
    task = create_runtime_task(hass, asyncio.sleep(60))

    assert runtime.sip is registry
    assert task in runtime.tasks
    assert not {"call_registry", "runtime_tasks"}.intersection(
        hass.data.get(DOMAIN, {})
    )

    await cancel_runtime_tasks(hass)
    assert task.cancelled()
    assert runtime.tasks == set()


async def test_phonebook_pushes_only_changed_content_and_rehydrates_one_device(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from homeassistant.const import EVENT_SERVICE_REGISTERED

    from custom_components.voip_stack.config_entry_runtime import (
        register_phonebook_service_event_sync,
    )
    from custom_components.voip_stack.phonebook_runtime import (
        push_roster_json_to_esps,
    )
    from custom_components.voip_stack.runtime_data import VoipStackRuntime

    runtime = VoipStackRuntime(
        transport_config={},
        assist_config={},
        trunk_config={},
        endpoints=MagicMock(),
        phones=MagicMock(),
        phonebook_sensor=SimpleNamespace(
            async_update=AsyncMock(),
            extra_state_attributes={"roster_json": '{"contacts":["Casa"]}'},
        ),
    )
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    entry.runtime_data = runtime

    from custom_components.voip_stack import phonebook_runtime

    monkeypatch.setattr(
        phonebook_runtime,
        "_get_voip_devices",
        AsyncMock(
            return_value=[
                {"host": "192.0.2.10", "name": "P4"},
                {"host": "192.0.2.11", "name": "S3"},
            ]
        ),
    )
    resolver = MagicMock()
    resolver.route_id_for_host.side_effect = {
        "192.0.2.10": "p4",
        "192.0.2.11": "s3",
    }.get
    monkeypatch.setattr(phonebook_runtime, "get_resolver", lambda _hass: resolver)

    deliveries: list[tuple[str, str]] = []

    async def _record_delivery(call) -> None:
        deliveries.append((call.service, call.data["roster_json"]))

    hass.services.async_register("esphome", "p4_set_roster_json", _record_delivery)
    hass.services.async_register("esphome", "s3_set_roster_json", _record_delivery)

    first = '{"contacts":["Casa"]}'
    await asyncio.gather(
        push_roster_json_to_esps(hass, first),
        push_roster_json_to_esps(hass, first),
    )
    assert deliveries == [
        ("p4_set_roster_json", first),
        ("s3_set_roster_json", first),
    ]

    changed = '{"contacts":["Casa","Test"]}'
    await push_roster_json_to_esps(hass, changed)
    assert deliveries[-2:] == [
        ("p4_set_roster_json", changed),
        ("s3_set_roster_json", changed),
    ]

    runtime.phonebook_sensor.extra_state_attributes["roster_json"] = changed
    register_phonebook_service_event_sync(hass)
    runtime.phonebook_delivered_roster.pop("p4_set_roster_json")
    hass.bus.async_fire(
        EVENT_SERVICE_REGISTERED,
        {"domain": "esphome", "service": "p4_set_roster_json"},
    )
    await hass.async_block_till_done()

    assert deliveries.count(("p4_set_roster_json", changed)) == 2
    assert deliveries.count(("s3_set_roster_json", changed)) == 1


async def test_phonebook_keeps_configured_peer_stable_during_esphome_restart(
    hass: HomeAssistant,
) -> None:
    from custom_components.voip_stack.peer import Peer
    from custom_components.voip_stack.runtime_data import VoipStackRuntime
    from custom_components.voip_stack.sensor import VoipPhonebookSensor

    runtime = VoipStackRuntime(
        transport_config={},
        assist_config={},
        trunk_config={},
        endpoints=MagicMock(),
        phones=MagicMock(),
    )
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    entry.runtime_data = runtime

    endpoint_entity = "text_sensor.s3_voip_endpoint"
    extension_entity = "text.s3_voip_extension"
    groups_entity = "text.s3_voip_ring_groups"
    conference_entity = "text.s3_voip_conference_groups"
    conference_ring_entity = "switch.s3_voip_conference_ring"
    entities = {
        "voip_endpoint": endpoint_entity,
        "voip_extension": extension_entity,
        "voip_ring_groups": groups_entity,
        "voip_conference_groups": conference_entity,
        "voip_conference_ring": conference_ring_entity,
    }
    for entity_id, value in (
        (endpoint_entity, "S3|192.0.2.11|5060|40000|full_duplex"),
        (extension_entity, "669"),
        (groups_entity, "home"),
        (conference_entity, "all"),
        (conference_ring_entity, "on"),
    ):
        hass.states.async_set(entity_id, value)

    sensor = VoipPhonebookSensor(hass)
    sensor._tracked_entities = set(entities.values())
    original = Peer(
        name="S3",
        host="192.0.2.11",
        endpoint_id="esphome:s3",
        endpoint_kind="esphome",
        extension="669",
        ring_group="home",
        conference_group="all",
        conference_ring=True,
        device={"device_id": "s3", "route_id": "s3", "entities": entities},
    )

    assert sensor._stable_phonebook_peers([original]) == [original]
    sensor._rehydrate_services.clear()
    runtime.phonebook_delivered_roster["s3_set_roster_json"] = "current"
    assert sensor._stable_phonebook_peers([]) == [original]
    assert "s3_set_roster_json" not in runtime.phonebook_delivered_roster

    for entity_id in entities.values():
        hass.states.async_set(entity_id, "unavailable")
    restoring = Peer(
        name="S3",
        host="192.0.2.11",
        endpoint_id="esphome:s3",
        endpoint_kind="esphome",
        device={"device_id": "s3", "route_id": "s3", "entities": entities},
    )
    assert sensor._stable_phonebook_peers([restoring]) == [original]
    assert sensor._rehydrate_services == {"s3_set_roster_json"}

    sensor._tracked_entities.clear()
    assert sensor._stable_phonebook_peers([]) == []


async def test_stale_esphome_state_event_uses_entry_generation(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.voip_stack import esphome_state_bridge
    from custom_components.voip_stack.endpoint_registry import EndpointRegistry
    from custom_components.voip_stack.runtime_data import VoipStackRuntime

    runtime = VoipStackRuntime(
        transport_config={},
        assist_config={},
        trunk_config={},
        endpoints=EndpointRegistry(),
        phones=MagicMock(),
        esp_state_event_generations={"sensor.p4_voip_state": 2},
    )
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    entry.runtime_data = runtime
    get_devices = AsyncMock()
    monkeypatch.setattr(esphome_state_bridge, "_get_voip_devices", get_devices)

    await esphome_state_bridge.async_emit_state_event(
        hass,
        "sensor.p4_voip_state",
        "idle",
        "in_call",
        generation=1,
    )

    get_devices.assert_not_awaited()
    assert "esp_state_event_generations" not in hass.data.get(DOMAIN, {})


async def test_renamed_esphome_state_entity_keeps_its_stable_role(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from homeassistant.helpers import entity_registry as er

    from custom_components.voip_stack import esphome_state_bridge
    from custom_components.voip_stack.endpoint_registry import EndpointRegistry
    from custom_components.voip_stack.runtime_data import VoipStackRuntime

    runtime = VoipStackRuntime(
        transport_config={},
        assist_config={},
        trunk_config={},
        endpoints=EndpointRegistry(),
        phones=MagicMock(),
    )
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    entry.runtime_data = runtime
    entity = er.async_get(hass).async_get_or_create(
        "sensor",
        "esphome",
        "p4-text_sensor-voip_state",
        suggested_object_id="renamed_phone_status",
    )
    emit = AsyncMock()
    monkeypatch.setattr(esphome_state_bridge, "async_emit_state_event", emit)
    esphome_state_bridge.register_state_event_bridge(hass)

    hass.states.async_set(entity.entity_id, "ringing")
    await hass.async_block_till_done()

    emit.assert_awaited_once()
    assert emit.await_args.args[1] == entity.entity_id


async def test_device_resolver_is_cached_on_entry_runtime(
    hass: HomeAssistant,
) -> None:
    from custom_components.voip_stack.device_resolver import get_resolver
    from custom_components.voip_stack.endpoint_registry import EndpointRegistry
    from custom_components.voip_stack.runtime_data import VoipStackRuntime

    runtime = VoipStackRuntime(
        transport_config={},
        assist_config={},
        trunk_config={},
        endpoints=EndpointRegistry(),
        phones=MagicMock(),
    )
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    entry.runtime_data = runtime

    assert get_resolver(hass) is get_resolver(hass) is runtime.device_resolver
    assert "device_resolver" not in hass.data.get(DOMAIN, {})


async def test_system_health_and_media_capture_repair_are_privacy_safe(
    hass: HomeAssistant,
) -> None:
    from homeassistant.helpers import issue_registry as ir

    from custom_components.voip_stack.phone_endpoint import (
        EndpointAvailability,
        EndpointKind,
    )
    from custom_components.voip_stack.repairs import (
        MEDIA_CAPTURE_ISSUE_ID,
        async_sync_runtime_issues,
    )
    from custom_components.voip_stack.runtime_data import VoipStackRuntime
    from custom_components.voip_stack.system_health import system_health_info

    runtime = VoipStackRuntime(
        transport_config={},
        assist_config={},
        trunk_config={},
        endpoints=SimpleNamespace(
            endpoints=(
                SimpleNamespace(
                    kind=EndpointKind.ESPHOME,
                    availability=EndpointAvailability.AVAILABLE,
                ),
            )
        ),
        phones=MagicMock(),
        media_capture=True,
    )
    runtime.sip = MagicMock()
    runtime.sip.active_count.return_value = 2
    runtime.rtp_port_pool = {"used": {40000}}
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    entry.runtime_data = runtime

    async_sync_runtime_issues(hass)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, MEDIA_CAPTURE_ISSUE_ID)
    health = await system_health_info(hass)

    assert issue is not None
    assert health["configured_phones"] == 1
    assert health["online_esphome_phones"] == 1
    assert health["active_calls"] == 2
    assert health["reserved_rtp_ports"] == 1
    assert health["advertise_host"] == "automatic"
    assert health["media_workers"] == 0
    assert not {"call_id", "device_id", "caller", "callee"}.intersection(health)

    runtime.media_capture = False
    async_sync_runtime_issues(hass)
    assert ir.async_get(hass).async_get_issue(DOMAIN, MEDIA_CAPTURE_ISSUE_ID) is None


async def test_esphome_call_control_repair_tracks_missing_actions(
    hass: HomeAssistant,
) -> None:
    from homeassistant.helpers import issue_registry as ir

    from custom_components.voip_stack.repairs import (
        ESPHOME_ACTION_ISSUE_PREFIX,
        async_sync_esphome_action_issue,
    )

    device = {
        "device_id": "p4-device",
        "name": "P4",
        "route_id": "p4",
    }
    issue_id = f"{ESPHOME_ACTION_ISSUE_PREFIX}p4-device"

    async_sync_esphome_action_issue(hass, device)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)

    assert issue is not None
    assert issue.translation_placeholders == {
        "phone": "P4",
        "actions": "start_call, answer_call, decline_call, hangup_call",
    }

    async def handle_action(_call) -> None:
        return None

    for action in ("start_call", "answer_call", "decline_call", "hangup_call"):
        hass.services.async_register("esphome", f"p4_{action}", handle_action)

    async_sync_esphome_action_issue(hass, device)

    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_deleting_the_last_browser_phone_does_not_restore_a_default(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.voip_stack.config_entry_runtime import (
        async_config_entry_updated,
        entry_phone_signature,
        entry_runtime_signature,
    )

    entry = MockConfigEntry(domain=DOMAIN, data={}, version=4)
    entry.add_to_hass(hass)
    from custom_components.voip_stack.endpoint_registry import EndpointRegistry
    from custom_components.voip_stack.runtime_data import VoipStackRuntime

    entry.runtime_data = VoipStackRuntime(
        transport_config={},
        assist_config={},
        trunk_config={},
        endpoints=EndpointRegistry(),
        phones=MagicMock(),
        entry_runtime_signature=entry_runtime_signature(entry),
        entry_phone_signature=entry_phone_signature(entry),
        entry_contacts_signature=(),
    )
    reload_entry = AsyncMock()
    monkeypatch.setattr(hass.config_entries, "async_reload", reload_entry)

    await async_config_entry_updated(hass, entry)

    assert not entry.subentries
    reload_entry.assert_not_awaited()


async def test_new_entry_bootstraps_one_normal_named_browser_phone(
    hass: HomeAssistant,
) -> None:
    from custom_components.voip_stack.phone_config import (
        CONF_PHONE_ENDPOINT_ID,
        CONF_PHONE_KIND,
        CONF_PHONE_NAME,
        async_bootstrap_browser_phone,
    )

    entry = MockConfigEntry(domain=DOMAIN, data={}, version=5)
    entry.add_to_hass(hass)
    hass.config.location_name = "Casa"

    created = async_bootstrap_browser_phone(hass, entry)

    assert created is not None
    assert created.data[CONF_PHONE_ENDPOINT_ID].startswith("browser:")
    assert created.data[CONF_PHONE_KIND] == "browser"
    assert created.data[CONF_PHONE_NAME] == "Casa"
    assert async_bootstrap_browser_phone(hass, entry) is None
    assert len(entry.subentries) == 1


async def test_v4_migration_marks_existing_phone_bootstrap_as_complete(
    hass: HomeAssistant,
) -> None:
    from custom_components.voip_stack import async_migrate_entry
    from custom_components.voip_stack.const import CONF_INITIAL_PHONE_CREATED

    entry = MockConfigEntry(domain=DOMAIN, data={}, version=4)
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)

    assert entry.version == 5
    assert entry.data[CONF_INITIAL_PHONE_CREATED] is True
    assert not entry.subentries
