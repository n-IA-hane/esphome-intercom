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

    assert runtime.calls is registry
    assert task in runtime.tasks
    assert not {"call_registry", "runtime_tasks"}.intersection(
        hass.data.get(DOMAIN, {})
    )

    await cancel_runtime_tasks(hass)
    assert task.cancelled()
    assert runtime.tasks == set()


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
    runtime.calls = MagicMock()
    runtime.calls.active_count.return_value = 2
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
    assert not {"call_id", "device_id", "caller", "callee"}.intersection(health)

    runtime.media_capture = False
    async_sync_runtime_issues(hass)
    assert ir.async_get(hass).async_get_issue(DOMAIN, MEDIA_CAPTURE_ISSUE_ID) is None


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
