"""Resolve native ESPHome hardware and controls independently of SIP routing."""

from __future__ import annotations

from typing import Optional

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .runtime_data import runtime_data

_ENTITY_ROLE_TOKENS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("voip_state", ("voip_state",)),
    ("voip_extension", ("voip_extension",)),
    ("voip_transport", ("voip_transport",)),
    ("voip_ring_groups", ("voip_ring_groups",)),
    ("voip_queues", ("voip_queues",)),
    ("voip_conference_groups", ("voip_conference_groups",)),
    ("voip_conference_ring", ("voip_ring_on_conference",)),
    ("video_send", ("video_send", "send_video")),
    ("auto_answer", ("auto_answer",)),
    ("dnd", ("do_not_disturb", "_dnd_", "_dnd")),
    ("incoming_caller", ("incoming_caller", "_caller")),
    ("destination", ("destination",)),
    ("last_reason", ("voip_last_reason", "last_reason", "end_reason")),
    ("previous", ("previous",)),
    ("next", ("next",)),
    ("decline", ("decline",)),
    ("call", ("call",)),
)
_ENTITY_ROLE_DOMAINS: dict[str, frozenset[str]] = {
    "voip_state": frozenset({"sensor", "text_sensor"}),
    "voip_extension": frozenset({"text"}),
    "voip_transport": frozenset({"sensor", "text_sensor"}),
    "voip_ring_groups": frozenset({"text"}),
    "voip_queues": frozenset({"text"}),
    "voip_conference_groups": frozenset({"text"}),
    "voip_conference_ring": frozenset({"switch"}),
    "video_send": frozenset({"switch"}),
    "auto_answer": frozenset({"switch"}),
    "dnd": frozenset({"switch"}),
    "incoming_caller": frozenset({"sensor", "text_sensor"}),
    "destination": frozenset({"sensor", "text_sensor"}),
    "last_reason": frozenset({"sensor", "text_sensor"}),
    "previous": frozenset({"button"}),
    "next": frozenset({"button"}),
    "decline": frozenset({"button"}),
    "call": frozenset({"button"}),
}


def _normalized_identity(value: object) -> str:
    return "".join(
        character if character.isalnum() else "_"
        for character in str(value or "").casefold()
    )


def entity_role(entity: object) -> str:
    """Resolve a stable ESPHome entity role before using its mutable entity ID."""

    entity_id = str(getattr(entity, "entity_id", "") or "")
    domain = entity_id.partition(".")[0]
    if domain == "camera":
        return "camera"
    identities = (
        _normalized_identity(getattr(entity, "unique_id", "")),
        _normalized_identity(getattr(entity, "translation_key", "")),
        _normalized_identity(getattr(entity, "original_name", "")),
        _normalized_identity(entity_id),
    )
    for role, tokens in _ENTITY_ROLE_TOKENS:
        if domain not in _ENTITY_ROLE_DOMAINS[role]:
            continue
        if role == "call" and "decline" in identities[3]:
            continue
        if any(token in identity for token in tokens for identity in identities if identity):
            return role
    return ""


def slugify_route_id(raw: str) -> str:
    """Match the slug ESPHome uses for `esphome.{slug}_start_call` services."""
    return "".join(c if c.isalnum() else "_" for c in (raw or "").lower()).strip("_")


class VoipDeviceResolver:
    """Resolve native phone hardware and its existing Home Assistant controls."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def list_devices(self) -> list[dict]:
        """Read native phone hardware; DeviceInfo never supplies a SIP route."""
        entity_registry = er.async_get(self.hass)
        device_registry = dr.async_get(self.hass)
        entry = next((item for item in self.hass.config_entries.async_entries(DOMAIN)
                      if getattr(item, "source", None) != "ignore"), None)
        provisioner = getattr(getattr(entry, "runtime_data", None), "phone_provisioning", None)
        entities_by_device = {}
        phone_device_ids = set()
        for entity in entity_registry.entities.values():
            if entity.device_id is None or entity.platform != "esphome":
                continue
            entities_by_device.setdefault(entity.device_id, []).append(entity)
            if entity_role(entity) == "voip_state":
                phone_device_ids.add(entity.device_id)
        result = []
        for device_id in sorted(phone_device_ids):
            device = device_registry.async_get(device_id)
            if device is None:
                continue
            origin = self.hass.config_entries.async_get_entry(str(device.config_entry_id or ""))
            if origin is None or origin.domain != "esphome":
                continue
            if provisioner is not None and provisioner.is_removed(origin.entry_id, device_id):
                continue
            native = getattr(origin, "runtime_data", None)
            info = getattr(native, "device_info", None)
            node_name = str(getattr(info, "name", "") or origin.data.get("device_name") or "")
            route_id = slugify_route_id(node_name) or self._route_id_from_device(device)
            entities = self._collect_entities(entities_by_device[device_id])
            if route_id:
                entities["start_call_service"] = f"esphome.{route_id}_start_call"
            result.append({
                "device_id": device_id, "origin_entry_id": origin.entry_id,
                "name": device.name_by_user or device.name or getattr(info, "friendly_name", "") or node_name or "ESPHome phone",
                "route_id": route_id, "api_host": str(origin.data.get("host") or ""),
                "esphome_id": self._device_esphome_id(device), "entities": entities,
                "camera_entity_id": entities.get("camera", ""),
                "api_available": bool(getattr(native, "available", False)
                    and getattr(getattr(native, "client", None), "is_connected", False)),
            })
        return result

    async def resolve_target(self, call: ServiceCall) -> Optional[dict]:
        """Match a service call's target selector to one of our devices."""
        device_ids: set[str] = set()
        entity_registry = er.async_get(self.hass)
        for source in [call.data, getattr(call, "target", None) or {}]:
            ids = source.get("device_id")
            if isinstance(ids, str):
                device_ids.add(ids)
            elif isinstance(ids, list):
                device_ids.update(ids)
            eids = source.get("entity_id")
            if isinstance(eids, str):
                eids = [eids]
            if eids:
                for eid in eids:
                    entry = entity_registry.async_get(eid)
                    if entry and entry.device_id:
                        device_ids.add(entry.device_id)
        if not device_ids:
            return None
        runtime = runtime_data(self.hass)
        if runtime is not None:
            device_ids.update(endpoint.device_id for selected in tuple(device_ids)
                              if (endpoint := runtime.endpoints.by_device_id(selected)) is not None)
        for dev in await self.list_devices():
            if dev["device_id"] in device_ids:
                return dev
        return None

    def _route_id_from_device(self, device) -> str:
        """Find the owning ESPHome entry and return its service slug."""
        entry_id = str(device.config_entry_id or "")
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if entry and entry.domain == "esphome":
            raw = entry.data.get("device_name") or entry.title or ""
            return slugify_route_id(raw)
        return ""

    @staticmethod
    def _device_esphome_id(device) -> Optional[str]:
        for domain, identifier in device.identifiers:
            if domain == "esphome":
                return identifier
        return None

    @staticmethod
    def _collect_entities(entities) -> dict[str, str]:
        out: dict[str, str] = {}
        for entity in entities:
            eid = entity.entity_id
            role = entity_role(entity)
            if role and role not in out:
                out[role] = eid
        return out


def get_resolver(hass: HomeAssistant) -> VoipDeviceResolver:
    runtime = runtime_data(hass)
    resolver = runtime.device_resolver if runtime is not None else None
    if resolver is None:
        resolver = VoipDeviceResolver(hass)
        if runtime is not None:
            runtime.device_resolver = resolver
    return resolver
