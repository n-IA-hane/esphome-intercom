"""Reconcile ESPHome phone inventory through the existing endpoint registry."""

from __future__ import annotations

from dataclasses import replace
import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .phone_endpoint import (
    EndpointAvailability,
    EndpointKind,
    PhoneEndpoint,
)
from .const import DOMAIN
from .endpoint_registry import EndpointCollisionError
from .phone_queue_membership import normalize_phone_queues
from .browser_phone_data import _endpoint_registry

_LOGGER = logging.getLogger(__name__)


def upsert_esphome_endpoint(registry, candidate):
    """Write one physical identity without adopting another transport owner."""
    previous = registry.get(candidate.endpoint_id)
    if previous is not None and (previous.username != candidate.username
                                 or previous.device_id != candidate.device_id):
        raise EndpointCollisionError(field="endpoint_id", value=candidate.endpoint_id,
            endpoint_id=candidate.endpoint_id, conflicting_endpoint_id=previous.endpoint_id)
    return registry.upsert(candidate)


def sync_managed_phone_endpoint(registry, phone):
    """Project the configured account; transport discovery owns runtime fields."""
    previous = registry.get(phone.endpoint_id)
    candidate = previous if previous is not None else PhoneEndpoint(
        endpoint_id=phone.endpoint_id, device_id=phone.device_id,
        kind=EndpointKind.SIP_ACCOUNT, name=phone.display_name,
        capabilities=frozenset({"audio", "dtmf"}))
    return upsert_esphome_endpoint(registry, replace(candidate,
        kind=EndpointKind.SIP_ACCOUNT, device_id=phone.device_id, name=phone.display_name, username=phone.username,
        extension=phone.extension, ring_group=phone.ring_group, queues=phone.queues,
        conference_group=phone.conference_group, conference_ring=phone.conference_ring))


def device_is_phonebook_available(hass: HomeAssistant, device: dict) -> bool:
    """Return whether an ESP should be advertised to other VoIP peers."""
    if device.get("managed"):
        return bool(device.get("registered"))
    entities = device.get("entities") or {}
    state_entity = entities.get("voip_state")
    if not state_entity:
        return False
    state = hass.states.get(state_entity)
    return state is not None and str(state.state).strip().lower() not in {
        "", "unknown", "unavailable",
    } and bool(device.get("sip_port") and device_transport(device))


def device_transport(device: dict) -> str:
    """Read the endpoint-declared SIP signaling transport."""
    value = str(device.get("sip_transport") or "").lower()
    return value if value in ("udp", "tcp", "tls") else ""


def restore_esp_identities(hass, runtime):
    """Rebuild physical identities from their existing HA owners, even offline."""
    from homeassistant.helpers import device_registry as dr, entity_registry as er
    from .device_resolver import entity_role
    from .endpoint_device import account_device_id_for_endpoint
    from .managed_phones import managed_records
    from .phone_config import esphome_phone_policy
    from .store import config_entry
    from .repairs import ESPHOME_ENDPOINT_COLLISION_ISSUE_PREFIX, async_sync_esphome_endpoint_collision_issue
    from homeassistant.helpers import issue_registry as ir

    entry = config_entry(hass)
    records = managed_records(entry) if entry is not None else {}
    for phone in records.values():
        sync_managed_phone_endpoint(runtime.endpoints, phone)
    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    eligible_devices = set()
    owned_devices = (device for origin in hass.config_entries.async_entries("esphome")
                     for device in dr.async_entries_for_config_entry(devices, origin.entry_id))
    for device in owned_devices:
        origin = hass.config_entries.async_get_entry(device.config_entry_id)
        if origin is None or origin.domain != "esphome":
            continue
        provisioner = runtime.phone_provisioning
        if provisioner is not None and provisioner.is_removed(origin.entry_id, device.id):
            continue
        phone = records.get(device.id)
        native_entities = [entity for entity in er.async_entries_for_device(entities, device.id)
                           if entity.platform == "esphome"]
        roles = {entity_role(entity): entity for entity in native_entities}
        if "voip_state" not in roles:
            continue
        eligible_devices.add(device.id)
        if runtime.endpoints.by_device_id(device.id) is not None:
            # No attempt was made to apply its desired name/aliases here.
            # Only the provisioner's successful sync can clear that conflict.
            continue
        fields = {}
        for role, field in (("voip_extension", "extension"), ("voip_ring_groups", "ring_group"),
                            ("voip_conference_groups", "conference_group"), ("voip_conference_ring", "conference_ring")):
            state = hass.states.get(roles[role].entity_id) if role in roles else None
            fallback = getattr(phone, field, False if field == "conference_ring" else "")
            value = state.state if state is not None and state.state not in {"unknown", "unavailable"} else fallback
            fields[field] = str(value).casefold() in {"on", "true", "1"} if field == "conference_ring" else str(value)
        endpoint_id = phone.username if phone is not None else str(origin.data.get("device_name") or "")
        if not endpoint_id:
            continue
        candidate = PhoneEndpoint(endpoint_id=endpoint_id, device_id=device.id,
            account_device_id=account_device_id_for_endpoint(hass, entry, endpoint_id) if phone else "",
            name=(device.name_by_user or (phone.display_name if phone is not None else "")
                  or device.name or origin.title or (phone.username if phone is not None else "ESPHome")),
            username=phone.username if phone is not None else str(origin.data.get("device_name") or ""),
            kind=EndpointKind.SIP_ACCOUNT if phone else EndpointKind.ESPHOME, entity_ids=frozenset(entity.entity_id for entity in native_entities),
            availability=EndpointAvailability.OFFLINE, capabilities=frozenset({"audio", "dtmf"}),
            policy=esphome_phone_policy(entry, endpoint_id), **fields)
        try:
            upsert_esphome_endpoint(runtime.endpoints, candidate)
        except EndpointCollisionError as error:
            async_sync_esphome_endpoint_collision_issue(hass, device.id, name=candidate.name, error=error)
        else:
            async_sync_esphome_endpoint_collision_issue(hass, device.id)
    # The issue registry owns diagnostics. Reconcile its derived rows after
    # every restore, clearing only removed or no-longer-phone devices here.
    for domain, issue_id in tuple(ir.async_get(hass).issues):
        if domain == DOMAIN and issue_id.startswith(ESPHOME_ENDPOINT_COLLISION_ISSUE_PREFIX):
            device_id = issue_id.removeprefix(ESPHOME_ENDPOINT_COLLISION_ISSUE_PREFIX)
            if device_id not in eligible_devices:
                async_sync_esphome_endpoint_collision_issue(hass, device_id)


async def _get_voip_devices(hass: HomeAssistant) -> list[dict[str, Any]]:
    from homeassistant.helpers import entity_registry as er
    from .mailbox import mailbox_snapshot
    from .device_resolver import get_resolver
    from .endpoint_device import account_device_id_for_endpoint
    from .phone_config import esphome_phone_policy
    from .repairs import async_sync_esphome_action_issue
    from .store import config_entry

    devices = await get_resolver(hass).list_devices()
    registry = _endpoint_registry(hass)
    if registry is None:
        return devices

    discovered_ids: set[str] = set()
    published = []
    entry = config_entry(hass)
    for device in devices:
        device_id = str(device.get("device_id") or "").strip()
        name = str(device.get("name") or "").strip()
        if not device_id or not name:
            continue
        endpoint_id = str(device.get("endpoint_username") or device.get("sip_uri_user") or "")
        if not endpoint_id:
            continue
        discovered_ids.add(endpoint_id)
        previous = registry.get(endpoint_id)
        capabilities = frozenset(
            str(value).strip().casefold()
            for value in (device.get("capabilities") or ("audio", "dtmf"))
            if str(value).strip()
        )
        account_device_id = previous.account_device_id if previous else ""
        if not account_device_id and entry is not None and device.get("endpoint_id"):
            account_device_id = account_device_id_for_endpoint(hass, entry, endpoint_id)
        entity_ids = {value for value in (device.get("entities") or {}).values()
                      if isinstance(value, str) and "." in value}
        if account_device_id:
            entity_ids.update(row.entity_id for row in er.async_entries_for_device(
                er.async_get(hass), account_device_id))
        endpoint = PhoneEndpoint(
            endpoint_id=endpoint_id,
            name=name,
            kind=EndpointKind.SIP_ACCOUNT if device.get("managed") else EndpointKind.ESPHOME,
            extension=str(device.get("extension") or ""),
            username=str(device.get("endpoint_username") or device.get("sip_uri_user") or ""),
            device_id=device_id,
            account_device_id=account_device_id,
            entity_ids=frozenset(entity_ids),
            availability=(EndpointAvailability.AVAILABLE
                          if device_is_phonebook_available(hass, device)
                          else EndpointAvailability.OFFLINE),
            capabilities=capabilities,
            ring_group=str(device.get("ring_group") or ""),
            queues=normalize_phone_queues(device.get("queues", previous.queues if previous is not None else ())),
            conference_group=str(device.get("conference_group") or ""),
            conference_ring=bool(device.get("conference_ring", False)),
            active_call_id=(previous.active_call_id if previous is not None else ""),
            policy=previous.policy if previous is not None and device.get("managed") else esphome_phone_policy(entry, endpoint_id),
        )
        try:
            upsert_esphome_endpoint(registry, endpoint)
        except ValueError as err:
            _LOGGER.warning(
                "Skipping ESPHome phone endpoint device_id=%s name=%s: %s",
                device_id,
                name,
                err,
            )
            continue
        device["availability"] = endpoint.availability.value
        device["endpoint_id"] = endpoint_id
        device["endpoint_type"] = EndpointKind.ESPHOME.value
        device["voicemail"] = mailbox_snapshot(hass, endpoint_id)
        from .parking_config import parking_settings
        parking = parking_settings(hass)
        device["parking"] = {"enabled": bool(parking.get("enabled")),
                             "unavailable_reason": parking.get("unavailable_reason", "")}
        device["capabilities"] = sorted(capabilities)
        async_sync_esphome_action_issue(hass, device)
        published.append(device)

    # Keep physical devices represented when temporarily unreachable; only
    # their transport availability changes. VoIP Stack never creates/adopts
    # their Device Registry entry.
    for endpoint in registry.endpoints:
        if (
            endpoint.kind is EndpointKind.ESPHOME
            and endpoint.endpoint_id not in discovered_ids
            and endpoint.availability is not EndpointAvailability.OFFLINE
        ):
            registry.update(
                endpoint.endpoint_id,
                availability=EndpointAvailability.OFFLINE,
            )
    return published
