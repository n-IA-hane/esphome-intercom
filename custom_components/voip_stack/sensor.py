"""Sensor platform for VoIP Stack.

HA publishes one SIP dial-plan roster.

Entity names:
  - sensor.voip_phonebook       format per row:
      name|ip|sip_port|rtp_port|audio_mode|tx_formats|rx_formats|sip_tcp

ESP YAMLs subscribe to the unified sensor and normalize it locally into their
SIP dial plan.
"""
import asyncio
import contextlib
import logging

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from .endpoint_device import (
    async_link_endpoint_entity,
    endpoint_call_state_attributes,
    endpoint_config_subentry_id,
    endpoint_device_info,
    endpoint_public_attributes,
    enum_value,
)
from .endpoint_entity_manager import (
    EndpointEntityManager,
    event_projects_endpoint_state,
    register_endpoint_entity_manager,
)
from .peer_snapshot import async_build_peer_snapshot
from .runtime_data import require_runtime_data

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0
UNAVAILABLE_STATES = {"", "unknown", "unavailable"}
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
        config_subentry_id=endpoint_config_subentry_id(
            hass, endpoint.endpoint_id
        ),
        device_id=endpoint.device_id or None,
        has_entity_name=True,
        translation_key="phone_endpoint_call_state",
    )


def _state_is_available(state) -> bool:
    return state is not None and str(state.state or "").strip().lower() not in UNAVAILABLE_STATES


def _is_voip_roster_entity(entity_id: str) -> bool:
    return any(
        token in entity_id
        for token in (
            "voip_state",
            "voip_endpoint",
            "voip_ring_groups",
            "voip_conference_groups",
            "voip_ring_on_conference",
        )
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    unified_sensor = VoipPhonebookSensor(hass)
    async_add_entities([unified_sensor], True)
    endpoint_manager = EndpointEntityManager(
        hass,
        entry,
        async_add_entities,
        PhoneEndpointCallStateSensor,
    )
    endpoint_manager.async_setup()
    runtime = require_runtime_data(hass)
    runtime.phonebook_sensor = unified_sensor
    register_endpoint_entity_manager(
        entry, "endpoint_call_state_entity_manager", endpoint_manager
    )


class PhoneEndpointCallStateSensor(SensorEntity):
    """Durable, automation-friendly state for one logical phone endpoint."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_has_entity_name = True
    _attr_options = PHONE_CALL_STATES
    _attr_should_poll = False
    _attr_translation_key = "phone_endpoint_call_state"

    def __init__(self, hass, endpoint, registry) -> None:
        self.endpoint = endpoint
        self.registry = registry
        self._attr_unique_id = f"phone_endpoint_{endpoint.endpoint_id}_call_state"
        self._attr_device_info = endpoint_device_info(endpoint)
        self._attr_native_value = self._idle_state(endpoint)
        self._attr_extra_state_attributes = endpoint_public_attributes(endpoint)
        self._active_call_id = ""
        self._revision = -1

    async def async_added_to_hass(self) -> None:
        from .websocket_api import CALL_EVENT, _ha_softphone_state

        await super().async_added_to_hass()
        async_link_endpoint_entity(
            self.registry, self.endpoint.endpoint_id, self.entity_id
        )
        if enum_value(self.endpoint.kind) == "browser":
            self._apply_call_payload(
                _ha_softphone_state(self.hass, self.endpoint.endpoint_id)
            )
        self.async_on_remove(self.hass.bus.async_listen(CALL_EVENT, self._on_call_event))

    @staticmethod
    def _idle_state(endpoint) -> str:
        availability = enum_value(endpoint.availability)
        return "idle" if availability in {"online", "available", "registered", "connected"} else "offline"

    @callback
    def apply_endpoint(self, endpoint) -> None:
        self.endpoint = endpoint
        if not endpoint.active_call_id or self._attr_native_value in {"idle", "offline"}:
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
        terminal_reason = str(payload.get("terminal_reason") or payload.get("reason") or "")
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
    """Authoritative SIP phonebook publisher."""

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_icon = "mdi:phone-voip"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._attr_unique_id = "voip_stack_phonebook"
        self._attr_name = "VoIP Phonebook"
        self.entity_id = "sensor.voip_phonebook"
        self._attr_native_value = "0 entries"
        self._phonebook = ""
        self._roster_json = '{"version":2,"capabilities":["extension","ring_group","conference_group","conference_ring"],"contacts":[]}'
        self._count = 0
        self._tracked_entities: set[str] = set()
        self._unsub_state = None
        self._unsub_registry = None
        self._recompute_task: asyncio.Task | None = None
        self._recompute_requested = False

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "phonebook": self._phonebook,
            "roster_json": self._roster_json,
            "count": self._count,
        }

    async def async_added_to_hass(self) -> None:
        @callback
        def _on_registry_change(event) -> None:
            entity_id = event.data.get("entity_id") or ""
            if not _is_voip_roster_entity(entity_id):
                return
            self.hass.async_create_task(self._refresh_tracked_entities())

        self._unsub_registry = self.hass.bus.async_listen(
            "entity_registry_updated", _on_registry_change
        )
        await self._refresh_tracked_entities(initial=True)

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        if self._unsub_registry:
            self._unsub_registry()
            self._unsub_registry = None
        if self._recompute_task is not None:
            self._recompute_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._recompute_task
            self._recompute_task = None

    @callback
    def _schedule_recompute(self) -> None:
        """Coalesce state bursts while guaranteeing a final fresh snapshot."""
        self._recompute_requested = True
        if self._recompute_task is None or self._recompute_task.done():
            self._recompute_task = self.hass.async_create_task(self._drain_recomputes())

    async def _drain_recomputes(self) -> None:
        current = asyncio.current_task()
        try:
            while self._recompute_requested:
                self._recompute_requested = False
                await self._recompute()
        finally:
            if self._recompute_task is current:
                self._recompute_task = None
                if self._recompute_requested:
                    self._schedule_recompute()

    async def _schedule_and_wait_recompute(self) -> None:
        self._schedule_recompute()
        task = self._recompute_task
        if task is not None:
            await task

    async def _refresh_tracked_entities(self, initial: bool = False) -> None:
        entity_registry = er.async_get(self.hass)
        new_set = {
            e.entity_id
            for e in entity_registry.entities.values()
            if _is_voip_roster_entity(e.entity_id)
        }
        if new_set == self._tracked_entities and not initial:
            return
        self._tracked_entities = new_set

        @callback
        def _on_state_change(event) -> None:
            entity_id = event.data.get("entity_id") or ""
            new_state = event.data.get("new_state")
            old_state = event.data.get("old_state")
            if "voip_endpoint" in entity_id:
                old_value = old_state.state if old_state is not None else None
                new_value = new_state.state if new_state is not None else None
                old_endpoint = (old_state.attributes or {}).get("endpoint") if old_state is not None else None
                new_endpoint = (new_state.attributes or {}).get("endpoint") if new_state is not None else None
                if old_value != new_value or old_endpoint != new_endpoint:
                    self._schedule_recompute()
                return
            if (
                "voip_ring_groups" in entity_id
                or "voip_conference_groups" in entity_id
                or "voip_ring_on_conference" in entity_id
            ):
                old_value = old_state.state if old_state is not None else None
                new_value = new_state.state if new_state is not None else None
                if old_value != new_value:
                    self._schedule_recompute()
                return
            old_avail = _state_is_available(old_state)
            new_avail = _state_is_available(new_state)
            if old_avail == new_avail:
                return
            self._schedule_recompute()

        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        if new_set:
            self._unsub_state = async_track_state_change_event(
                self.hass, list(new_set), _on_state_change
            )
        await self._schedule_and_wait_recompute()

    async def _recompute(self) -> None:
        from .phonebook_runtime import (
            format_entry_unified,
            push_roster_json_to_esps,
            registered_roster_entries,
        )
        from .endpoint_routing import roster_from_peers
        from .roster import dump_roster_json

        peers = await async_build_peer_snapshot(self.hass)
        entries = [format_entry_unified(p) for p in peers]
        roster_entries = roster_from_peers(self.hass, peers, registered_roster_entries(self.hass))
        phonebook = ",".join(entries)
        roster_json = dump_roster_json(roster_entries)
        visible_count = len(roster_entries)
        new_value = f"{visible_count} entry" if visible_count == 1 else f"{visible_count} entries"
        if (
            new_value != self._attr_native_value
            or phonebook != self._phonebook
            or roster_json != self._roster_json
        ):
            self._attr_native_value = new_value
            self._phonebook = phonebook
            self._roster_json = roster_json
            self._count = visible_count
            _LOGGER.debug(
                "Phonebook recomputed (%d entries)", visible_count
            )
            if self.hass and self.entity_id:
                self.async_write_ha_state()
                await push_roster_json_to_esps(self.hass, roster_json)

    async def async_update(self) -> None:
        await self._schedule_and_wait_recompute()
