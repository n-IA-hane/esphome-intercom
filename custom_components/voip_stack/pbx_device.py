"""Native HA grouping for PBX projections, with settings owned by the parent."""

from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import DOMAIN

PBX_SUBENTRY_TYPE = "pbx"
PBX_NAME = "VoIP Stack PBX"


def pbx_subentry_id(entry) -> str | None:
    """Read the system grouping without recreating an explicitly deleted one."""
    return next((row.subentry_id for row in entry.subentries.values()
                 if row.subentry_type == PBX_SUBENTRY_TYPE), None)


def pbx_device_info(entry) -> dr.DeviceInfo:
    """Keep the established device identifier independent of its grouping."""
    return dr.DeviceInfo(identifiers={(DOMAIN, entry.entry_id)}, name=PBX_NAME,
        manufacturer="VoIP Stack", model=PBX_NAME, entry_type=dr.DeviceEntryType.SERVICE)


@callback
def async_ensure_pbx_device(hass, entry) -> None:
    """Bootstrap only during normal entry setup and migrate registries in place."""
    identity = pbx_subentry_id(entry)
    if identity is None:
        row = ConfigSubentry(subentry_type=PBX_SUBENTRY_TYPE, title=PBX_NAME,
            unique_id="pbx", data={})
        hass.config_entries.async_add_subentry(entry, row)
        identity = row.subentry_id

    devices, entities = dr.async_get(hass), er.async_get(hass)
    device = devices.async_get_device_by_identifier((DOMAIN, entry.entry_id), entry.entry_id)
    unique_ids = {"voip_stack_call_event", *(
        f"voip_stack:{entry.entry_id}:pbx:{key}"
        for key in ("active_calls", "registered_contacts"))}
    # Core removes old-subentry entities when the device move is dispatched.
    # Move their registry ownership first, in this synchronous callback.
    for entity in er.async_entries_for_config_entry(entities, entry.entry_id):
        if (entity.platform == DOMAIN
                and (entity.unique_id in unique_ids
                     or (device is not None and entity.device_id == device.id))
                and entity.config_subentry_id != identity):
            entities.async_update_entity(entity.entity_id, config_subentry_id=identity)
    if device is not None and device.config_subentry_id != identity:
        devices.async_update_device(device.id, new_config_subentry_id=identity)
    devices.async_get_or_create(config_entry_id=entry.entry_id,
        config_subentry_id=identity, **pbx_device_info(entry))
