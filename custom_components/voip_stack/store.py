"""Config-entry backed storage helpers for VoIP Stack."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError

from .const import CONF_PHONEBOOK_CONTACTS, CONF_SIP_ACCOUNTS, DOMAIN
from .runtime_data import sip_registrar

_LOGGER = logging.getLogger(__name__)


def config_entry(hass: HomeAssistant) -> ConfigEntry | None:
    return next(iter(hass.config_entries.async_entries(DOMAIN)), None)


def sip_account_dicts(hass: HomeAssistant) -> list[dict]:
    entry = config_entry(hass)
    if entry is None:
        return []
    from .phone_config import sip_account_dicts_from_subentries

    configured = sip_account_dicts_from_subentries(entry)
    if configured:
        return configured
    # Read compatibility for entries which have not reached migration setup.
    return [
        dict(item)
        for item in entry.data.get(CONF_SIP_ACCOUNTS, [])
        if isinstance(item, dict)
    ]


def sip_accounts(hass: HomeAssistant):
    from .sip_registrar import account_from_mapping

    accounts = []
    for raw in sip_account_dicts(hass):
        try:
            accounts.append(account_from_mapping(raw))
        except ValueError as err:
            _LOGGER.warning("Ignoring invalid SIP account in config entry: %s", err)
    return accounts


def phonebook_contact_dicts(hass: HomeAssistant) -> list[dict]:
    entry = config_entry(hass)
    if entry is None:
        return []
    return [dict(item) for item in entry.data.get(CONF_PHONEBOOK_CONTACTS, []) if isinstance(item, dict)]


def manual_roster_entries(hass: HomeAssistant):
    from .roster import RosterError, parse_roster_json

    try:
        return parse_roster_json(phonebook_contact_dicts(hass))
    except (RosterError, ValueError, TypeError) as err:
        _LOGGER.warning("Ignoring invalid manual phonebook contacts in config entry: %s", err)
        return []


def store_manual_roster_entries(hass: HomeAssistant, entries) -> None:
    from .roster import dump_roster_json, parse_roster_json

    entry = config_entry(hass)
    if entry is None:
        raise ConfigEntryError("VoIP Stack config entry is required for phonebook contacts")
    contacts = parse_roster_json(dump_roster_json(list(entries)))
    payload = [
        {
            "id": item.id,
            "name": item.name,
            "address": item.address,
            "sip_uri": item.sip_uri,
            "extension": item.extension,
            "number": item.number,
            "port": item.port,
            "ha_bridge": item.ha_bridge,
            "enabled": item.enabled,
            "metadata": item.metadata,
        }
        for item in contacts
    ]
    data = dict(entry.data)
    data[CONF_PHONEBOOK_CONTACTS] = payload
    hass.config_entries.async_update_entry(entry, data=data)
    hass.data.setdefault(DOMAIN, {})["manual_roster_entries"] = contacts


def update_sip_accounts(hass: HomeAssistant, accounts: list[dict]) -> None:
    entry = config_entry(hass)
    if entry is None:
        raise ConfigEntryError("VoIP Stack config entry is required for SIP accounts")
    from .phone_config import replace_sip_account_subentries

    replace_sip_account_subentries(hass, entry, accounts)
    registrar = sip_registrar(hass)
    if registrar is not None:
        registrar.update_accounts(sip_accounts(hass))
