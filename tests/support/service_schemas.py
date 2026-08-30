"""Load the integration's real service schemas without importing HA Core."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import types
from types import SimpleNamespace

import voluptuous as vol
import yaml


ROOT = Path(__file__).resolve().parents[2]
SERVICES = ROOT / "custom_components" / "voip_stack" / "services.py"
SERVICES_YAML = ROOT / "custom_components" / "voip_stack" / "services.yaml"


def _boolean(value: object) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "enable"}:
        return True
    if normalized in {"0", "false", "no", "off", "disable"}:
        return False
    raise vol.Invalid("invalid boolean value")


def load_service_registrations() -> dict[str, dict[str, object]]:
    """Register services against a minimal HA facade and return their contracts."""

    source = SERVICES.read_text()
    source = source.replace(
        "from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse\n",
        "HomeAssistant = object\nServiceCall = object\n"
        "SupportsResponse = SimpleNamespace(OPTIONAL='optional', ONLY='only')\n",
    )
    source = source.replace(
        "from homeassistant.helpers import config_validation as cv\n",
        "",
    )
    source = source.replace(
        "from .authorization import (\n"
        "    async_require_service_admin,\n"
        "    async_require_service_control,\n"
        ")\n",
        "AUTHORIZATION_CALLS = []\n"
        "async def async_require_service_admin(_hass, _call):\n"
        "    AUTHORIZATION_CALLS.append('admin')\n\n"
        "async def async_require_service_control(_hass, _call):\n"
        "    AUTHORIZATION_CALLS.append('control')\n",
    )
    source = source.replace(
        "from .const import DOMAIN\n",
        'DOMAIN = "voip_stack"\n',
    )
    namespace = {
        "__name__": "voip_stack_services_schema_test",
        "SimpleNamespace": SimpleNamespace,
        "cv": SimpleNamespace(
            string=str,
            entity_id=str,
            boolean=_boolean,
        ),
    }
    module = types.ModuleType(namespace["__name__"])
    module.__dict__.update(namespace)
    sys.modules[module.__name__] = module
    exec(compile(source, str(SERVICES), "exec"), module.__dict__)
    namespace = module.__dict__

    registrations: dict[str, dict[str, object]] = {}

    class ServiceRegistry:
        def async_register(
            self,
            _domain,
            service,
            _handler,
            *,
            schema=None,
            **kwargs,
        ):
            registrations[service] = {
                "schema": schema,
                "handler": _handler,
                "authorization_calls": namespace["AUTHORIZATION_CALLS"],
                **kwargs,
            }

    hass = SimpleNamespace(services=ServiceRegistry())
    service_names = set(yaml.safe_load(SERVICES_YAML.read_text()))
    async def _handler(_call):
        return None

    handlers = {name: _handler for name in service_names}
    asyncio.run(namespace["async_register_services"](hass, handlers))
    return registrations


def load_service_schemas() -> dict[str, vol.Schema]:
    """Return every schema registered by the real service table."""

    return {
        name: registration["schema"]
        for name, registration in load_service_registrations().items()
        if registration.get("schema") is not None
    }


def schema_fields(schema: vol.Schema) -> set[str]:
    """Return the accepted top-level keys from a voluptuous service schema."""

    fields: set[str] = set()
    for key in schema.schema:
        value = key.schema if isinstance(key, vol.Marker) else key
        fields.add(str(value))
    return fields
