"""Administrative provisioning actions, separate from normal SIP call control."""

import secrets

import voluptuous as vol
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .physical_phone import physical_phone_for_device, physical_phone_records, physical_phone_subentries
from .runtime_data import require_runtime_data

SERVICES = ("provision_phone", "get_phone_provisioning_state", "refresh_phone_profiles", "rotate_phone_provisioning_token")


async def _manager(call):
    from .authorization import async_require_service_admin
    await async_require_service_admin(call.hass, call)
    manager = require_runtime_data(call.hass).physical_phones
    if manager is None or not manager.owned():
        raise ServiceValidationError("Phone provisioning is disabled")
    return manager


def _phone(manager, data):
    identity = str(data.get("phone_id") or "")
    phone = physical_phone_records(manager.entry).get(identity)
    if phone is None and data.get("device_id"):
        phone = physical_phone_for_device(manager.hass, manager.entry, data["device_id"])
    if phone is None:
        raise ServiceValidationError("Choose a configured physical phone")
    return phone


async def async_provision_phone(call):
    manager = await _manager(call)
    phone = _phone(manager, call.data)
    manager.schedule({phone["phone_id"]})
    return {"phone_id": phone["phone_id"], "status": "queued"}


async def async_provisioning_state(call):
    manager = await _manager(call)
    phone = _phone(manager, call.data)
    result = {"phone_id": phone["phone_id"], **manager.public_state(phone["phone_id"])}
    if call.data.get("include_url", False):
        from .physical_phone_provisioning import phone_provisioning_url
        result["provisioning_url"] = phone_provisioning_url(manager.hass, manager.entry, phone)
    return result


async def async_refresh_profiles(call):
    manager = await _manager(call)
    manifest = await manager.catalog.async_manifest(refresh=True)
    return {"catalog_commit": manager.catalog.commit, "profiles": len(manifest["profiles"])}


async def async_rotate_provisioning_token(call):
    manager = await _manager(call)
    phone = _phone(manager, call.data)
    for subentry in physical_phone_subentries(manager.entry):
        if subentry.data["phone_id"] == phone["phone_id"]:
            manager.hass.config_entries.async_update_subentry(manager.entry, subentry,
                data={**subentry.data, "provisioning_token": secrets.token_urlsafe(32)})
            manager.files.pop(phone["phone_id"], None)
            manager.schedule({phone["phone_id"]})
            break
    return {"phone_id": phone["phone_id"], "status": "credential_rotated"}


def register_physical_phone_services(hass):
    schema = vol.Schema({vol.Optional("device_id"): cv.string, vol.Optional("phone_id"): cv.string})
    for name, handler in zip(SERVICES, (async_provision_phone, async_provisioning_state,
                                      async_refresh_profiles, async_rotate_provisioning_token), strict=True):
        hass.services.async_register("voip_stack", name, handler,
            schema=(vol.Schema({}) if name == "refresh_phone_profiles" else
                    schema.extend({vol.Optional("include_url", default=False): cv.boolean})
                    if name == "get_phone_provisioning_state" else schema),
            supports_response=SupportsResponse.OPTIONAL)


def unregister_physical_phone_services(hass):
    for name in SERVICES:
        hass.services.async_remove("voip_stack", name)
