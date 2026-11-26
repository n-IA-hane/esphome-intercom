"""Live config-entry and canonical phonebook synchronization."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant


from .const import CONF_PHONEBOOK_CONTACTS, CONF_SIP_ACCOUNTS, CONF_REGISTRAR_ENABLED
from .phone_config import (
    CONF_ESPHOME_PHONE_POLICIES,
    CONF_PHONE_KIND,
    CONF_PHONE_SEND_VIDEO,
    phone_subentries,
    phone_endpoint_id,
    phone_subentry_data,
    sync_registry_from_entry,
)
from .phone_endpoint import EndpointKind
from .phonebook_coordinator import PhonebookCoordinator, entry_phonebook
from .runtime_data import runtime_data, sip_registrar
from .trunk_policy import EXTERNAL_CALL_BLOCKED_ENDPOINTS
from .store import sip_accounts
from .browser_phone_runtime import (
    _async_load_ha_softphone_store,
    _publish_ha_softphone_state,
)


_LOGGER = logging.getLogger(__name__)


async def async_refresh_directory(hass: HomeAssistant) -> None:
    runtime = runtime_data(hass)
    if runtime is not None:
        await entry_phonebook(hass, runtime).async_refresh(deliver=True)


async def async_initialize_directory(coordinator: PhonebookCoordinator) -> None:
    """Initialize the directory once; its coordinator owns subsequent updates."""
    if not coordinator.closed:
        await coordinator.async_refresh(deliver=True)


def entry_runtime_signature(entry: ConfigEntry) -> dict:
    """Return parent-entry fields whose mutation requires a transport reload."""

    return {
        key: value
        for key, value in entry.data.items()
        if key not in {
            CONF_PHONEBOOK_CONTACTS, CONF_SIP_ACCOUNTS,
            EXTERNAL_CALL_BLOCKED_ENDPOINTS, CONF_ESPHOME_PHONE_POLICIES,
            "default_trunk_id", "default_ringing_timeout", "max_dialog_subscription_expires",
            "esphome_mailboxes", "voicemail", "parking",
            "managed_esphome_phones", "esphome_sip_enrollments",
            "esphome_phone_memberships",
            "phone_services",
            "phone_provisioning",
            "group_membership_version", "legacy_phone_references", CONF_REGISTRAR_ENABLED,
        }
    }


def entry_phone_signature(entry: ConfigEntry) -> tuple:
    """Return an equality-stable snapshot of native logical-phone subentries."""

    return tuple(
        (subentry.subentry_id, subentry.title, phone_subentry_data(subentry))
        for subentry in sorted(
            phone_subentries(entry), key=lambda item: item.subentry_id
        )
    )


def _only_browser_camera_send_changed(previous: tuple, current: tuple) -> bool:
    """Camera transmission intent does not change the public directory."""
    if len(previous) != len(current):
        return False
    changed = False
    for (old_id, old_title, old), (new_id, new_title, new) in zip(previous, current):
        if (old_id, old_title) != (new_id, new_title):
            return False
        if old == new:
            continue
        if old.get(CONF_PHONE_KIND) not in {EndpointKind.BROWSER.value, EndpointKind.SIP_ACCOUNT.value}:
            return False
        if (
            {key: value for key, value in old.items() if key != CONF_PHONE_SEND_VIDEO}
            != {key: value for key, value in new.items() if key != CONF_PHONE_SEND_VIDEO}
        ):
            return False
        changed = True
    return changed


async def async_config_entry_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply native config/subentry changes as soon as HA persists them."""

    runtime = runtime_data(hass)
    if runtime is None:
        return
    if runtime.ha_bindings is not None:
        runtime.ha_bindings.refresh_config()
    from .group_config import async_apply_group_updates, group_records, membership_signature
    from .paging_config import paging_group_configs
    async with runtime.group_update_lock:
        await async_apply_group_updates(hass, entry, runtime.entry_group_records)
        groups_now = group_records(entry)
        groups_changed = groups_now != runtime.entry_group_records
        runtime.entry_group_records = groups_now
        paging_now = paging_group_configs(entry)
        memberships_now = membership_signature(hass, entry)
        groups_changed = (groups_changed or paging_now != runtime.entry_paging_records
                          or (runtime.entry_membership_signature is not None
                              and memberships_now != runtime.entry_membership_signature))
        runtime.entry_paging_records = paging_now
        runtime.entry_membership_signature = memberships_now
        if runtime.queues is not None:
            runtime.queues.refresh()
    runtime.transport_config[EXTERNAL_CALL_BLOCKED_ENDPOINTS] = tuple(
        entry.data.get(EXTERNAL_CALL_BLOCKED_ENDPOINTS, ())
    )
    from .config import entry_transport_config
    runtime.transport_config["default_ringing_timeout"] = entry.data.get("default_ringing_timeout")
    transport_config = entry_transport_config(entry)
    runtime.transport_config["max_dialog_subscription_expires"] = transport_config["max_dialog_subscription_expires"]
    registrar_enabled = bool(transport_config.get(CONF_REGISTRAR_ENABLED))
    runtime.transport_config[CONF_REGISTRAR_ENABLED] = registrar_enabled
    if registrar := sip_registrar(hass):
        registrar.enabled = registrar_enabled
    runtime_signature = entry_runtime_signature(entry)
    from .contact_config import contact_dicts

    previous_runtime = runtime.entry_runtime_signature

    if previous_runtime is not None and previous_runtime != runtime_signature:
        await hass.config_entries.async_reload(entry.entry_id)
        return

    from .repairs import async_sync_pbx_configuration_issues
    async_sync_pbx_configuration_issues(hass)
    from .parking_config import parking_settings
    settings = parking_settings(hass)
    lot = runtime.sip.component("parking") if runtime.sip is not None else None
    if lot is not None and settings.get("enabled"):
        # Reserved calls retain their slot and timer; new deposits use the new
        # list. The configuration flow prevents removing an occupied slot.
        lot.slots = tuple(settings["slots"])
    from .trunk_runtime import async_reconcile_trunks
    await async_reconcile_trunks(hass, entry)
    from .phone_services import async_reconcile_phone_services
    await async_reconcile_phone_services(hass, entry)

    # Other configuration writers may have completed while integrations were
    # reconciling. Their canonical subentries, not the old projection, win.
    phone_signature = entry_phone_signature(entry)
    contacts_signature = tuple(contact_dicts(entry))
    previous_phones = runtime.entry_phone_signature
    previous_contacts = runtime.entry_contacts_signature
    phones_changed = previous_phones is not None and previous_phones != phone_signature
    contacts_changed = (
        previous_contacts is not None and previous_contacts != contacts_signature
    )
    if phones_changed:
        from .mailbox import async_remove_mailbox

        previous_ids = {str(data.get("endpoint_id") or "") for _identity, _title, data in previous_phones}
        current_ids = {str(data.get("endpoint_id") or "") for _identity, _title, data in phone_signature}
        for endpoint_id in previous_ids - current_ids:
            if endpoint_id:
                await async_remove_mailbox(hass, endpoint_id)
        previous_browser_ids = {
            endpoint.endpoint_id
            for endpoint in tuple(getattr(runtime.endpoints, "endpoints", ()))
            if endpoint.supports_ha_client
        }
        sync_registry_from_entry(hass, entry)
        for subentry in phone_subentries(entry):
            endpoint_id = phone_endpoint_id(subentry)
            endpoint = runtime.endpoints.get(endpoint_id) if endpoint_id else None
            if endpoint is None or not endpoint.supports_ha_client:
                continue
            await _async_load_ha_softphone_store(
                hass,
                entry,
                endpoint_id=endpoint.endpoint_id,
                endpoint_data=phone_subentry_data(subentry),
            )
        current_browser_ids = {
            endpoint.endpoint_id
            for endpoint in tuple(runtime.endpoints.endpoints)
            if endpoint.supports_ha_client
        }
        removed_browser_ids = previous_browser_ids - current_browser_ids
        presence = runtime.softphone_presence
        for endpoint_id in removed_browser_ids:
            presence.pop(endpoint_id, None)
        for endpoint_id in sorted(previous_browser_ids | current_browser_ids):
            _publish_ha_softphone_state(hass, endpoint_id=endpoint_id)
        registrar = sip_registrar(hass)
        if registrar is not None:
            registrar.update_accounts(sip_accounts(hass))
        from .account_runtime import recompute_account_availability
        recompute_account_availability(hass)

    # A failed sync must remain unapplied so a later reconciliation can retry.
    runtime.entry_runtime_signature = runtime_signature
    runtime.entry_phone_signature = phone_signature
    runtime.entry_phone_records = {
        str(data.get("endpoint_id") or "").strip(): dict(data)
        for _identity, _title, data in phone_signature
    }
    runtime.entry_contacts_signature = contacts_signature

    directory_phones_changed = phones_changed and not _only_browser_camera_send_changed(
        previous_phones, phone_signature
    )
    if directory_phones_changed or contacts_changed or groups_changed:
        await async_refresh_directory(hass)
    from .mailbox import async_refresh_mailbox_state
    await async_refresh_mailbox_state(hass)
    from .physical_phone_runtime import async_reconcile_physical_phone_runtime
    await async_reconcile_physical_phone_runtime(hass, entry)
