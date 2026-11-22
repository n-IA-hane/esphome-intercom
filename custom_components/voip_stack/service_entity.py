"""Event-driven HA entities attached to existing PBX application subentries."""

from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN
from .runtime_data import runtime_data


class NativeServiceEntity:
    """A state or command projection, without another service identity owner."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry, subentry_id, key):
        self.entry, self.subentry_id, self.runtime = entry, subentry_id, entry.runtime_data
        self._ready = False
        self._attr_unique_id = f"voip_stack:{entry.entry_id}:{subentry_id or 'pbx'}:{key}"
        if subentry_id is None:
            identifier = entry.entry_id
        else:
            row = entry.subentries[subentry_id]
            identifier = (f"group:{row.data['group_id']}" if row.subentry_type == "group" else
                          f"paging:{row.data['paging_group_id']}")
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, identifier)})

    @property
    def record(self):
        return self.entry.subentries.get(self.subentry_id) if self.subentry_id is not None else None

    @property
    def available(self):
        return (runtime_data(self.hass) is self.runtime
                and (self.subentry_id is None or self.record is not None))

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self._ready = True

    async def async_will_remove_from_hass(self):
        self._ready = False
        await super().async_will_remove_from_hass()

    @callback
    def refresh(self):
        if self._ready:
            self.async_write_ha_state()


@callback
def async_setup_native_service_entities(hass, entry, async_add_entities, factory, *, include):
    """Use native subentry membership as the platform's sole entity topology."""
    entities = {}
    closed = False

    async def remove(entity, permanent):
        identity = entity.entity_id
        if entity._ready:
            await entity.async_remove(force_remove=True)
        if permanent and identity and er.async_get(hass).async_get(identity) is not None:
            er.async_get(hass).async_remove(identity)

    @callback
    def reconcile(_event=None):
        if closed:
            return
        rows = {row.subentry_id: row for row in entry.subentries.values() if include(row)}
        for identity in entities.keys() - rows.keys():
            permanent = identity not in entry.subentries
            for entity in entities.pop(identity):
                hass.async_create_task(remove(entity, permanent), "voip-service-entity-remove")
        for identity, row in rows.items():
            if identity not in entities:
                entities[identity] = factory(entry, row)
                async_add_entities(entities[identity], config_subentry_id=identity)
            else:
                for entity in entities[identity]:
                    entity.refresh()

    async def changed(_hass, _entry):
        reconcile()

    @callback
    def close():
        nonlocal closed
        closed = True

    entry.async_on_unload(close)
    entry.async_on_unload(entry.add_update_listener(changed))
    reconcile()
    return reconcile
