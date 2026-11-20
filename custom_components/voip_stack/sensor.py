"""Home Assistant sensor projections for the phonebook and logical phone call state."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .endpoint_device import (
    endpoint_call_state_attributes,
    endpoint_config_subentry_id,
    endpoint_public_attributes,
    enum_value,
)
from .endpoint_entity_manager import (
    EndpointEntityManager,
    event_projects_endpoint_state,
    register_endpoint_entity_manager,
)
from .phonebook_coordinator import PHONEBOOK_UPDATED, PhonebookCoordinator
from .phone_entity import PhoneEndpointEntity
from .runtime_data import require_runtime_data, runtime_data

PARALLEL_UPDATES = 0
PHONE_CALL_STATES = [
    "offline",
    "idle",
    "calling",
    "remote_ringing",
    "ringing",
    "connecting",
    "in_call",
    "held",
    "terminating",
]
TERMINAL_CALL_STATES = {
    "idle",
    "busy",
    "declined",
    "cancelled",
    "media_incompatible",
    "transport_unreachable",
    "auth_required_unsupported",
    "protocol_error",
    "error",
}
LEGACY_DEFAULT_CALL_STATE_UNIQUE_ID = "voip_stack_ha_softphone_call_state"


@callback
def _migrate_default_call_state_entity(hass, entry, endpoint) -> None:
    """Move the historical HA-phone entity onto the unified endpoint class."""

    entity_registry = er.async_get(hass)
    old_entity_id = entity_registry.async_get_entity_id(
        "sensor", entry.domain, LEGACY_DEFAULT_CALL_STATE_UNIQUE_ID
    )
    if old_entity_id is None:
        return
    new_unique_id = f"phone_endpoint_{endpoint.endpoint_id}_call_state"
    duplicate_id = entity_registry.async_get_entity_id(
        "sensor", entry.domain, new_unique_id
    )
    if duplicate_id is not None and duplicate_id != old_entity_id:
        entity_registry.async_remove(duplicate_id)
    entity_registry.async_update_entity(
        old_entity_id,
        new_unique_id=new_unique_id,
        config_entry_id=entry.entry_id,
        config_subentry_id=endpoint_config_subentry_id(hass, endpoint.endpoint_id),
        device_id=endpoint.device_id or None,
        has_entity_name=True,
        translation_key="phone_endpoint_call_state",
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    from .physical_phone_entity import async_setup_physical_phone_sensors
    await async_setup_physical_phone_sensors(hass, entry, async_add_entities)
    runtime = require_runtime_data(hass)
    async_add_entities([VoipPhonebookSensor(runtime.phonebook)])
    endpoint_manager = EndpointEntityManager(
        hass,
        entry,
        async_add_entities,
        PhoneEndpointCallStateSensor,
    )
    endpoint_manager.async_setup()
    register_endpoint_entity_manager(
        entry, "endpoint_call_state_entity_manager", endpoint_manager
    )


class PhoneEndpointCallStateSensor(PhoneEndpointEntity, SensorEntity):
    """Durable, automation-friendly state for one logical phone endpoint."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = PHONE_CALL_STATES
    _attr_translation_key = "phone_endpoint_call_state"

    def __init__(self, hass, endpoint, registry) -> None:
        super().__init__(endpoint, registry, key="call_state")
        self._attr_native_value = self._idle_state(endpoint)
        self._attr_extra_state_attributes = endpoint_public_attributes(endpoint)
        self._active_call_id = ""
        self._revision = -1

    async def async_added_to_hass(self) -> None:
        from .const import CALL_EVENT
        from .browser_phone_projection import _ha_softphone_state

        await super().async_added_to_hass()
        if self.endpoint.account_device_id:
            from .phone_state import phone_state_observer

            observer = phone_state_observer(self.hass)
            self.async_on_remove(observer.subscribe(self._on_managed_phone_state))
            if phone := observer.previous.get(self.endpoint.endpoint_id):
                self._apply_managed_phone_state(phone)
            return
        runtime = runtime_data(self.hass)
        if (self.endpoint.supports_ha_client and runtime is not None
                and runtime.endpoints is self.registry):
            self._apply_call_payload(
                _ha_softphone_state(self.hass, self.endpoint.endpoint_id)
            )
        self.async_on_remove(
            self.hass.bus.async_listen(CALL_EVENT, self._on_call_event)
        )

    @callback
    def _on_managed_phone_state(self, update, context):
        if update["endpoint_id"] == self.endpoint.endpoint_id and (phone := update["phone"]):
            self._apply_managed_phone_state(phone)
            if context is not None:
                self.async_set_context(context)
            self.async_write_ha_state()

    def _apply_managed_phone_state(self, phone):
        """Show the same PBX call state used for the line's BLF and monitor."""
        self._attr_native_value = "offline" if phone["state"] == "unavailable" else phone["state"]
        calls = phone["calls"]
        self._attr_extra_state_attributes = endpoint_call_state_attributes(
            self.endpoint, calls[0] if len(calls) == 1 else {})

    @staticmethod
    def _idle_state(endpoint) -> str:
        availability = enum_value(endpoint.availability)
        return (
            "idle"
            if availability in {"online", "available", "registered", "connected"}
            else "offline"
        )

    @callback
    def apply_endpoint(self, endpoint) -> None:
        super().apply_endpoint(endpoint)
        if endpoint.account_device_id:
            return
        if not endpoint.active_call_id or self._attr_native_value in {
            "idle",
            "offline",
        }:
            self._attr_native_value = self._idle_state(endpoint)
        self._attr_extra_state_attributes = endpoint_public_attributes(endpoint)
        if self.hass is not None:
            self.async_write_ha_state()

    @callback
    def _on_call_event(self, event: Event) -> None:
        payload = dict(event.data)
        if not event_projects_endpoint_state(
            payload,
            self.endpoint,
            self.registry,
        ):
            return
        if not self._apply_call_payload(payload):
            return
        self.async_set_context(event.context)
        self.async_write_ha_state()

    @callback
    def _apply_call_payload(self, payload: dict[str, object]) -> bool:
        state = str(payload.get("state") or "idle").strip().lower()
        call_id = str(payload.get("call_id") or "").strip()
        revision = int(payload.get("revision") or payload.get("sequence") or 0)
        terminal_reason = str(
            payload.get("terminal_reason") or payload.get("reason") or ""
        )
        terminal = state in TERMINAL_CALL_STATES
        if (
            call_id
            and self._active_call_id
            and call_id != self._active_call_id
            and self._attr_native_value not in {"idle", "offline"}
        ):
            return False
        if (
            not terminal
            and call_id == self._active_call_id
            and revision < self._revision
        ):
            return False
        if terminal:
            state = self._idle_state(self.endpoint)
        elif state not in PHONE_CALL_STATES:
            state = self._attr_native_value
        if call_id:
            self._active_call_id = call_id
            self._revision = revision
        if terminal and terminal_reason != "forwarded":
            self._active_call_id = ""
            self._revision = -1
        self._attr_native_value = state
        self._attr_extra_state_attributes = endpoint_call_state_attributes(
            self.endpoint,
            payload,
        )
        return True


class VoipPhonebookSensor(SensorEntity):
    """Expose the latest committed phonebook projection to cards and automations."""

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_icon = "mdi:phone-voip"
    _attr_unique_id = "voip_stack_phonebook"
    _attr_name = "VoIP Phonebook"
    _unrecorded_attributes = frozenset({"phonebook", "roster_json"})

    def __init__(self, coordinator: PhonebookCoordinator) -> None:
        self.coordinator = coordinator
        self.entity_id = "sensor.voip_phonebook"

    @property
    def native_value(self) -> str:
        count = self.coordinator.count
        return "1 entry" if count == 1 else f"{count} entries"

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {"phonebook": self.coordinator.phonebook, "roster_json": self.coordinator.roster_json,
                "count": self.coordinator.count}

    @callback
    def async_entity_id_changed(self, old_entity_id: str) -> None:
        """Keep the projection subscribed during a native entity rename."""
        super().async_entity_id_changed(old_entity_id)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(self.hass, PHONEBOOK_UPDATED, self._directory_updated))

    @callback
    def _directory_updated(self, coordinator: PhonebookCoordinator) -> None:
        if self.coordinator is coordinator:
            self.async_write_ha_state()
