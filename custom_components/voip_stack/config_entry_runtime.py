"""Live config-entry and canonical phonebook synchronization."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_SERVICE_REGISTERED
from homeassistant.core import Event, HomeAssistant, callback

from .const import CONF_PHONEBOOK_CONTACTS, CONF_SIP_ACCOUNTS, DOMAIN
from .endpoint_lifecycle import create_runtime_task
from .phone_config import (
    phone_subentries,
    sync_registry_from_entry,
)
from .phone_endpoint import EndpointKind
from .phonebook_runtime import push_roster_json_to_esps
from .runtime_data import sip_registrar
from .store import manual_roster_entries, sip_accounts
from .websocket_api import (
    _async_load_ha_softphone_store,
    _publish_ha_softphone_state,
)


_LOGGER = logging.getLogger(__name__)


async def async_refresh_phonebook_sensor(hass: HomeAssistant) -> None:
    sensor = hass.data.get(DOMAIN, {}).get("phonebook_sensor")
    if sensor is not None:
        await sensor.async_update()


async def async_current_roster_json(hass: HomeAssistant) -> str:
    sensor = hass.data.get(DOMAIN, {}).get("phonebook_sensor")
    if sensor is not None:
        return str(sensor.extra_state_attributes.get("roster_json", "") or "")
    state = hass.states.get("sensor.voip_phonebook")
    if state is None:
        return ""
    return str(state.attributes.get("roster_json") or "")


async def async_refresh_and_push_phonebook(hass: HomeAssistant) -> None:
    await async_refresh_phonebook_sensor(hass)
    roster_json = await async_current_roster_json(hass)
    await push_roster_json_to_esps(hass, roster_json)


async def async_deferred_phonebook_sync(hass: HomeAssistant) -> None:
    """Push the canonical phonebook after entry setup/reload settles."""

    for delay in (0.0, 2.0, 10.0):
        if delay:
            await asyncio.sleep(delay)
        await async_refresh_and_push_phonebook(hass)


def entry_runtime_signature(entry: ConfigEntry) -> dict:
    """Return parent-entry fields whose mutation requires a transport reload."""

    return {
        key: value
        for key, value in entry.data.items()
        if key not in {CONF_PHONEBOOK_CONTACTS, CONF_SIP_ACCOUNTS}
    }


def entry_phone_signature(entry: ConfigEntry) -> tuple:
    """Return an equality-stable snapshot of native logical-phone subentries."""

    return tuple(
        (subentry.subentry_id, subentry.title, dict(subentry.data))
        for subentry in sorted(
            phone_subentries(entry), key=lambda item: item.subentry_id
        )
    )


async def async_config_entry_updated(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Apply native config/subentry changes as soon as HA persists them."""

    bucket = hass.data.setdefault(DOMAIN, {})
    runtime_signature = entry_runtime_signature(entry)
    phone_signature = entry_phone_signature(entry)
    contacts_signature = tuple(
        dict(item)
        for item in entry.data.get(CONF_PHONEBOOK_CONTACTS, [])
        if isinstance(item, dict)
    )
    previous_runtime = bucket.get("entry_runtime_signature")
    previous_phones = bucket.get("entry_phone_signature")
    previous_contacts = bucket.get("entry_contacts_signature")
    bucket["entry_runtime_signature"] = runtime_signature
    bucket["entry_phone_signature"] = phone_signature
    bucket["entry_phone_records"] = {
        str(subentry.data.get("endpoint_id") or "").strip(): dict(subentry.data)
        for subentry in phone_subentries(entry)
    }
    bucket["entry_contacts_signature"] = contacts_signature

    if previous_runtime is not None and previous_runtime != runtime_signature:
        await hass.config_entries.async_reload(entry.entry_id)
        return

    phones_changed = previous_phones is not None and previous_phones != phone_signature
    contacts_changed = (
        previous_contacts is not None and previous_contacts != contacts_signature
    )
    if phones_changed:
        previous_browser_ids = {
            endpoint.endpoint_id
            for endpoint in tuple(
                getattr(bucket.get("endpoint_registry"), "endpoints", ())
            )
            if endpoint.kind is EndpointKind.BROWSER
        }
        sync_registry_from_entry(hass, entry)
        for subentry in phone_subentries(entry):
            endpoint_id = str(subentry.data.get("endpoint_id") or "").strip()
            endpoint_registry = bucket.get("endpoint_registry")
            endpoint = (
                endpoint_registry.get(endpoint_id)
                if endpoint_registry is not None and endpoint_id
                else None
            )
            if endpoint is None or endpoint.kind is not EndpointKind.BROWSER:
                continue
            await _async_load_ha_softphone_store(
                hass,
                entry,
                endpoint_id=endpoint.endpoint_id,
                endpoint_data=dict(subentry.data),
            )
        endpoint_registry = bucket.get("endpoint_registry")
        current_browser_ids = {
            endpoint.endpoint_id
            for endpoint in tuple(getattr(endpoint_registry, "endpoints", ()))
            if endpoint.kind is EndpointKind.BROWSER
        }
        removed_browser_ids = previous_browser_ids - current_browser_ids
        presence = bucket.setdefault("ha_softphone_presence", {})
        for endpoint_id in removed_browser_ids:
            presence.pop(endpoint_id, None)
        for endpoint_id in sorted(previous_browser_ids | current_browser_ids):
            _publish_ha_softphone_state(hass, endpoint_id=endpoint_id)
        endpoint_sensor = bucket.get("ha_softphone_endpoint_sensor")
        if endpoint_sensor is not None:
            await endpoint_sensor.async_update()

        registrar = sip_registrar(hass)
        if registrar is not None:
            registrar.update_accounts(sip_accounts(hass))

    if contacts_changed:
        bucket["manual_roster_entries"] = manual_roster_entries(hass)
    if phones_changed or contacts_changed:
        await async_refresh_and_push_phonebook(hass)


def register_phonebook_service_event_sync(hass: HomeAssistant) -> None:
    """Refresh the phonebook when an ESPHome roster service appears."""

    bucket = hass.data.setdefault(DOMAIN, {})
    if bucket.get("phonebook_service_event_unsub") is not None:
        return

    @callback
    def _on_service_registered(event: Event) -> None:
        if event.data.get("domain") != "esphome":
            return
        service = str(event.data.get("service") or "")
        if not service.endswith("_set_roster_json"):
            return
        create_runtime_task(hass, async_refresh_and_push_phonebook(hass))

    bucket["phonebook_service_event_unsub"] = hass.bus.async_listen(
        EVENT_SERVICE_REGISTERED,
        _on_service_registered,
    )
