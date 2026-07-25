"""Load the integration's real service schemas without importing HA Core."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import voluptuous as vol


ROOT = Path(__file__).resolve().parents[2]
SERVICES = ROOT / "custom_components" / "voip_stack" / "services.py"


def _boolean(value: object) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "enable"}:
        return True
    if normalized in {"0", "false", "no", "off", "disable"}:
        return False
    raise vol.Invalid("invalid boolean value")


def load_service_schemas() -> dict[str, vol.Schema]:
    """Register services against a minimal HA facade and return their schemas."""

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
        "async def async_require_service_admin(_hass, _call):\n"
        "    return None\n\n"
        "async def async_require_service_control(_hass, _call):\n"
        "    return None\n",
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
    exec(compile(source, str(SERVICES), "exec"), namespace)

    schemas: dict[str, vol.Schema] = {}

    class ServiceRegistry:
        def async_register(
            self,
            _domain,
            service,
            _handler,
            *,
            schema=None,
            **_kwargs,
        ):
            if schema is not None:
                schemas[service] = schema

    hass = SimpleNamespace(services=ServiceRegistry())
    asyncio.run(namespace["async_register_services"](hass, {}))
    return schemas


def schema_fields(schema: vol.Schema) -> set[str]:
    """Return the accepted top-level keys from a voluptuous service schema."""

    fields: set[str] = set()
    for key in schema.schema:
        value = key.schema if isinstance(key, vol.Marker) else key
        fields.add(str(value))
    return fields
