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


def managed_phone_endpoint(registry, phone):
    """Prepare the account projection before publishing its native owner."""
    previous = registry.get(phone.endpoint_id)
    candidate = previous if previous is not None else PhoneEndpoint(
        endpoint_id=phone.endpoint_id, device_id=phone.device_id,
        kind=EndpointKind.SIP_ACCOUNT, name=phone.display_name,
        capabilities=frozenset({"audio", "dtmf"}))
    return replace(candidate,
        kind=EndpointKind.SIP_ACCOUNT, device_id=phone.device_id, name=phone.display_name, username=phone.username,
        extension=phone.extension, ring_group=phone.ring_group, queues=phone.queues,
        conference_group=phone.conference_group, conference_ring=phone.conference_ring)


def sync_managed_phone_endpoint(registry, phone):
    """Project the configured account; transport discovery owns runtime fields."""
    return upsert_esphome_endpoint(registry, managed_phone_endpoint(registry, phone))


def device_is_phonebook_available(hass: HomeAssistant, device: dict) -> bool:
    """Project SIP registration; API entity state cannot make a phone routable."""
    return bool(device.get('managed') and device.get('registered'))


def device_transport(device: dict) -> str:
    """Read the endpoint-declared SIP signaling transport."""
    value = str(device.get("sip_transport") or "").lower()
    return value if value in ("udp", "tcp", "tls") else ""


def restore_esp_identities(hass, runtime):
    """Restore configured SIP accounts; hardware discovery cannot create a phone route."""
    from .managed_phones import managed_account_subentries, managed_records
    from .store import config_entry

    entry = config_entry(hass)
    if entry is None:
        return
    rows = managed_account_subentries(entry)
    for device_id, phone in managed_records(entry).items():
        row = rows.get(device_id)
        if row is None or row.data.get('kind') != EndpointKind.SIP_ACCOUNT.value:
            continue
        runtime.endpoints.subentry_ids[phone.endpoint_id] = row.subentry_id
        sync_managed_phone_endpoint(runtime.endpoints, phone)


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
        if not endpoint_id or not device.get('managed'):
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
