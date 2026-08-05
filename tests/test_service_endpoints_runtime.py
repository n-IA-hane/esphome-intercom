#!/usr/bin/env python3
"""Runtime tests for Home Assistant phone endpoint selection."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.voip_stack"
PKG_DIR = ROOT / "custom_components" / "voip_stack"


class ServiceValidationError(ValueError):
    """Minimal Home Assistant service validation error."""

    def __init__(
        self,
        message: str,
        *,
        translation_domain: str | None = None,
        translation_key: str | None = None,
        translation_placeholders: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.translation_domain = translation_domain
        self.translation_key = translation_key
        self.translation_placeholders = translation_placeholders


class EndpointKind(Enum):
    BROWSER = "browser"
    SIP_ACCOUNT = "sip_account"
    ESPHOME = "esphome"


@dataclass
class PhoneEndpoint:
    endpoint_id: str
    name: str
    kind: EndpointKind
    device_id: str = ""
    entity_ids: frozenset[str] = frozenset()
    capabilities: frozenset[str] = frozenset()


class _Registry:
    def __init__(self, *endpoints: PhoneEndpoint) -> None:
        self.endpoints = {item.endpoint_id: item for item in endpoints}
        self.devices = {item.device_id: item for item in endpoints if item.device_id}

    def get(self, endpoint_id: str) -> PhoneEndpoint | None:
        return self.endpoints.get(endpoint_id)

    def by_device_id(self, device_id: str) -> PhoneEndpoint | None:
        return self.devices.get(device_id)


class _Call:
    def __init__(self, **data) -> None:
        self.data = data


def _load_service_endpoints(
    require_endpoint: AsyncMock,
    require_entities: AsyncMock,
):
    if "custom_components" not in sys.modules:
        root = types.ModuleType("custom_components")
        root.__path__ = [str(ROOT / "custom_components")]
        sys.modules["custom_components"] = root
    if PKG_NAME not in sys.modules:
        package = types.ModuleType(PKG_NAME)
        package.__path__ = [str(PKG_DIR)]
        sys.modules[PKG_NAME] = package

    homeassistant = types.ModuleType("homeassistant")
    homeassistant.__path__ = []
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    core.ServiceCall = object
    exceptions = types.ModuleType("homeassistant.exceptions")
    exceptions.ServiceValidationError = ServiceValidationError
    authorization = types.ModuleType(f"{PKG_NAME}.authorization")
    authorization.async_require_service_endpoint_control = require_endpoint
    authorization.async_require_service_entity_control = require_entities
    const = types.ModuleType(f"{PKG_NAME}.const")
    const.DOMAIN = "voip_stack"
    const.HA_PEER_FALLBACK_NAME = "Home Assistant"
    const.HA_SOFTPHONE_DEVICE_ID = "ha-softphone"
    phone_endpoint = types.ModuleType(f"{PKG_NAME}.phone_endpoint")
    phone_endpoint.DEFAULT_ENDPOINT_ID = "default"
    phone_endpoint.EndpointKind = EndpointKind
    phone_endpoint.PhoneEndpoint = PhoneEndpoint
    runtime_data = types.ModuleType(f"{PKG_NAME}.runtime_data")
    runtime_data.endpoint_directory = lambda hass: hass.data.get(
        "voip_stack", {}
    ).get("endpoint_registry", _Registry())
    runtime_data.preferred_browser_phone = lambda hass: runtime_data.endpoint_directory(
        hass
    ).get("default")

    module_name = f"{PKG_NAME}.service_endpoints"
    spec = importlib.util.spec_from_file_location(
        module_name,
        PKG_DIR / "service_endpoints.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load service_endpoints.py")
    module = importlib.util.module_from_spec(spec)
    dependencies = {
        "homeassistant": homeassistant,
        "homeassistant.core": core,
        "homeassistant.exceptions": exceptions,
        authorization.__name__: authorization,
        const.__name__: const,
        phone_endpoint.__name__: phone_endpoint,
        runtime_data.__name__: runtime_data,
    }
    with patch.dict(sys.modules, dependencies):
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    return module


class ServiceEndpointRuntimeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.require_endpoint = AsyncMock()
        self.require_entities = AsyncMock()
        self.module = _load_service_endpoints(
            self.require_endpoint,
            self.require_entities,
        )
        self.hass = types.SimpleNamespace(
            data={"voip_stack": {}},
            config=types.SimpleNamespace(location_name="Casa"),
        )

    def test_browser_selector_requires_a_configured_phone(self) -> None:
        with self.assertRaisesRegex(ServiceValidationError, "Select") as raised:
            self.module.service_browser_endpoint(self.hass, _Call())
        self.assertEqual(raised.exception.translation_key, "phone_selection_required")

        with self.assertRaisesRegex(ServiceValidationError, "Unknown") as raised:
            self.module.service_browser_endpoint(
                self.hass,
                _Call(device_id="missing"),
            )
        self.assertEqual(raised.exception.translation_domain, "voip_stack")
        self.assertEqual(raised.exception.translation_key, "unknown_phone_device")

    def test_browser_selector_rejects_non_browser_devices(self) -> None:
        browser = PhoneEndpoint(
            "default",
            "Dashboard",
            EndpointKind.BROWSER,
            "ha-softphone",
        )
        esp = PhoneEndpoint(
            "esphome:kitchen",
            "Kitchen",
            EndpointKind.ESPHOME,
            "esp-device",
        )
        self.hass.data["voip_stack"]["endpoint_registry"] = _Registry(browser, esp)

        endpoint_id, endpoint = self.module.service_browser_endpoint(
            self.hass,
            _Call(device_id="ha-softphone"),
            strict=True,
        )
        self.assertEqual(endpoint_id, "default")
        self.assertIs(endpoint, browser)
        with self.assertRaisesRegex(ServiceValidationError, "not a Home Assistant"):
            self.module.service_browser_endpoint(
                self.hass,
                _Call(device_id="esp-device"),
                strict=True,
            )
        try:
            self.module.service_browser_endpoint(
                self.hass,
                _Call(device_id="esp-device"),
                strict=True,
            )
        except ServiceValidationError as err:
            self.assertEqual(err.translation_domain, "voip_stack")
            self.assertEqual(err.translation_key, "phone_not_browser")
        else:  # pragma: no cover - explicit assertion failure path.
            self.fail("Expected non-browser phone rejection")

    def test_browser_selector_keeps_default_phone_aliases_and_strictness(self) -> None:
        browser = PhoneEndpoint(
            "default",
            "Dashboard",
            EndpointKind.BROWSER,
            "ha-softphone",
        )
        self.hass.data["voip_stack"]["endpoint_registry"] = _Registry(browser)

        for device_id in (None, "", "ha-softphone"):
            endpoint_id, endpoint = self.module.service_browser_endpoint(
                self.hass,
                _Call(device_id=device_id),
            )
            self.assertEqual(endpoint_id, "default")
            self.assertIs(endpoint, browser)

        esp = PhoneEndpoint(
            "default",
            "Physical default",
            EndpointKind.ESPHOME,
        )
        self.hass.data["voip_stack"]["endpoint_registry"] = _Registry(esp)
        self.assertEqual(
            self.module.service_browser_endpoint(self.hass, _Call()),
            ("default", None),
        )
        with self.assertRaisesRegex(
            ServiceValidationError,
            "The selected Device is not a Home Assistant browser phone",
        ):
            self.module.service_browser_endpoint(
                self.hass,
                _Call(),
                strict=True,
            )

    def test_configured_selector_accepts_only_integration_owned_phones(self) -> None:
        browser = PhoneEndpoint(
            "default",
            "Dashboard",
            EndpointKind.BROWSER,
            "ha-softphone",
        )
        account = PhoneEndpoint(
            "account:office",
            "Office",
            EndpointKind.SIP_ACCOUNT,
            "account-device",
        )
        esp = PhoneEndpoint(
            "esphome:kitchen",
            "Kitchen",
            EndpointKind.ESPHOME,
            "esp-device",
        )
        self.hass.data["voip_stack"]["endpoint_registry"] = _Registry(
            browser,
            account,
            esp,
        )

        self.assertEqual(
            self.module.service_configured_endpoint(
                self.hass,
                _Call(device_id="account-device"),
            ),
            ("account:office", account),
        )
        with self.assertRaisesRegex(
            ServiceValidationError,
            "integration-owned",
        ) as raised:
            self.module.service_configured_endpoint(
                self.hass,
                _Call(device_id="esp-device"),
            )
        self.assertEqual(raised.exception.translation_domain, "voip_stack")
        self.assertEqual(
            raised.exception.translation_key,
            "phone_not_integration_owned",
        )

    def test_configured_selector_requires_an_available_phone(self) -> None:
        with self.assertRaisesRegex(
            ServiceValidationError,
            "Select a Home Assistant phone",
        ):
            self.module.service_configured_endpoint(self.hass, _Call())
        with self.assertRaisesRegex(
            ServiceValidationError,
            "Unknown Home Assistant phone device",
        ):
            self.module.service_configured_endpoint(
                self.hass,
                _Call(device_id="ha-softphone"),
            )
        with self.assertRaisesRegex(
            ServiceValidationError,
            "Unknown Home Assistant phone device",
        ) as raised:
            self.module.service_configured_endpoint(
                self.hass,
                _Call(device_id="missing"),
            )
        self.assertEqual(raised.exception.translation_domain, "voip_stack")
        self.assertEqual(raised.exception.translation_key, "unknown_phone_device")

        browser = PhoneEndpoint(
            "default",
            "Dashboard",
            EndpointKind.BROWSER,
            "ha-softphone",
        )
        self.hass.data["voip_stack"]["endpoint_registry"] = _Registry(browser)
        self.assertEqual(
            self.module.service_configured_endpoint(
                self.hass,
                _Call(device_id="ha-softphone"),
            ),
            ("default", browser),
        )

    def test_browser_name_uses_endpoint_then_location_then_fallback(self) -> None:
        endpoint = PhoneEndpoint(
            "default",
            "Wall tablet",
            EndpointKind.BROWSER,
        )
        self.assertEqual(
            self.module.browser_endpoint_name(self.hass, "default", endpoint),
            "Wall tablet",
        )
        self.assertEqual(
            self.module.browser_endpoint_name(self.hass, "default"),
            "Casa",
        )
        self.hass.config.location_name = ""
        self.assertEqual(
            self.module.browser_endpoint_name(self.hass, "default"),
            "Home Assistant",
        )

    async def test_unresolved_esp_device_uses_exact_ephemeral_permissions(self) -> None:
        call = _Call()
        device = {
            "device_id": "esp-device",
            "endpoint_id": "esphome:kitchen",
            "name": "Kitchen",
            "entities": {
                "call": "button.kitchen_call",
                "invalid": "not-an-entity",
            },
        }
        await self.module.async_require_phone_service_control(
            self.hass,
            call,
            device=device,
            action_entity_ids=("button.kitchen_call",),
        )

        endpoint = self.require_endpoint.await_args.args[2]
        self.assertEqual(endpoint.endpoint_id, "esphome:kitchen")
        self.assertEqual(endpoint.kind, EndpointKind.ESPHOME)
        self.assertEqual(endpoint.entity_ids, frozenset({"button.kitchen_call"}))
        self.require_entities.assert_awaited_once_with(
            self.hass,
            call,
            ("button.kitchen_call",),
        )

    async def test_explicit_endpoint_is_not_replaced_by_device_metadata(self) -> None:
        browser = PhoneEndpoint(
            "default",
            "Dashboard",
            EndpointKind.BROWSER,
            "ha-softphone",
        )
        device = {
            "device_id": "esp-device",
            "endpoint_id": "esphome:kitchen",
            "name": "Kitchen",
            "entities": {"call": "button.kitchen_call"},
        }

        await self.module.async_require_phone_service_control(
            self.hass,
            _Call(),
            endpoint=browser,
            device=device,
        )

        self.require_endpoint.assert_awaited_once_with(
            self.hass,
            unittest.mock.ANY,
            browser,
        )
        self.require_entities.assert_not_awaited()

    async def test_registered_esp_endpoint_is_used_without_reconstruction(self) -> None:
        esp = PhoneEndpoint(
            "esphome:kitchen",
            "Kitchen",
            EndpointKind.ESPHOME,
            "esp-device",
        )
        self.hass.data["voip_stack"]["endpoint_registry"] = _Registry(esp)

        await self.module.async_require_phone_service_control(
            self.hass,
            _Call(),
            device={"device_id": "esp-device"},
        )

        self.require_endpoint.assert_awaited_once_with(
            self.hass,
            unittest.mock.ANY,
            esp,
        )


if __name__ == "__main__":
    unittest.main()
