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
        "endpoint_id": "esphome:kitchen",
        "endpoint_type": "esphome",
        "device_id": "device-kitchen",
        "name": "Waveshare P4 Touch",
        "call_id": "",
        "state": "accepted",
        "destination": "Casa",
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
    action_result.as_service_response.assert_called_once_with(
        include_legacy_fields=True
    )


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
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, MEDIA_CAPTURE_ISSUE_ID) is None
    )


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
    bucket = hass.data.setdefault(DOMAIN, {})
    bucket["entry_runtime_signature"] = entry_runtime_signature(entry)
    bucket["entry_phone_signature"] = entry_phone_signature(entry)
    bucket["entry_contacts_signature"] = ()
    reload_entry = AsyncMock()
    monkeypatch.setattr(hass.config_entries, "async_reload", reload_entry)

    await async_config_entry_updated(hass, entry)

    assert not entry.subentries
    reload_entry.assert_not_awaited()
