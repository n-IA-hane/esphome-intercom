"""Entry-owned directory refresh and Home Assistant presentation."""

import asyncio
import contextlib
import logging

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event

from .device_resolver import entity_role
from .directory_runtime import async_refresh_directory
from .phonebook_runtime import format_entry_unified
from .roster import dump_roster_json
from .runtime_data import runtime_data

_LOGGER = logging.getLogger(__name__)
PHONEBOOK_UPDATED = "voip_stack_phonebook_updated"
UNAVAILABLE_STATES = {"", "unknown", "unavailable"}


def _state_is_available(state) -> bool:
    return (
        state is not None
        and str(state.state or "").strip().lower() not in UNAVAILABLE_STATES
    )


def _is_voip_roster_entity(entity: object) -> bool:
    return getattr(entity, "platform", None) == "esphome" and entity_role(entity) in {
        "voip_state", "voip_endpoint", "voip_ring_groups",
        "voip_conference_groups", "voip_conference_ring",
    }


def entry_phonebook(hass, runtime):
    """Return the coordinator owned by this exact config-entry runtime."""
    if runtime.phonebook is None:
        runtime.phonebook = PhonebookCoordinator(hass, runtime)
    return runtime.phonebook


class PhonebookCoordinator:
    """Track inputs and deliver only a committed entry-owned directory."""

    def __init__(self, hass: HomeAssistant, runtime) -> None:
        self.hass = hass
        self.runtime = runtime
        self.phonebook = ""
        self.roster_json = dump_roster_json([])
        self.count = 0
        self._name_conflicts: tuple[str, ...] | None = None
        self._tracked_entities: set[str] = set()
        self._unsub_state = None
        self._unsub_registry = None
        self._recompute_task: asyncio.Task | None = None
        self._recompute_requested = False
        self.closed = False

    def _current(self) -> bool:
        return not self.closed and runtime_data(self.hass) is self.runtime and self.runtime.phonebook is self

    async def async_start(self) -> None:
        """Attach to HA once and build the initial view before platforms start."""
        if self.closed or self._unsub_registry is not None:
            return
        self._unsub_registry = self.hass.bus.async_listen("entity_registry_updated", self._registry_changed)
        try:
            self._refresh_tracked_entities(initial=True)
            await self.async_refresh()
        except BaseException:
            await self.async_close()
            raise

    @callback
    def _registry_changed(self, event) -> None:
        if not self._current():
            return
        entity_id = event.data.get("entity_id") or ""
        entity = er.async_get(self.hass).async_get(entity_id)
        if (_is_voip_roster_entity(entity) or entity_id in self._tracked_entities
                or event.data.get("old_entity_id") in self._tracked_entities):
            if self._refresh_tracked_entities():
                self._schedule_recompute()

    @callback
    def _stop_state_tracking(self) -> None:
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None

    @callback
    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self._recompute_task is not None and self.runtime.phonebook is self and self.runtime.directory is not None:
            self.runtime.directory.begin_refresh()
        self._stop_state_tracking()
        if self._unsub_registry is not None:
            self._unsub_registry()
            self._unsub_registry = None
        self._recompute_requested = False
        if self._recompute_task is not None:
            self._recompute_task.cancel()

    async def async_close(self) -> None:
        self.close()
        if self._recompute_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._recompute_task

    @callback
    def request_refresh(self) -> None:
        """Coalesce a committed account change without delaying SIP replies."""
        self._schedule_recompute()

    def _schedule_recompute(self) -> None:
        if not self._current():
            return
        self._recompute_requested = True
        if self._recompute_task is None or self._recompute_task.done():
            self._recompute_task = self.hass.async_create_task(self._drain_recomputes())

    async def _drain_recomputes(self) -> None:
        try:
            while self._current() and self._recompute_requested:
                self._recompute_requested = False
                await self._recompute()
        finally:
            self._recompute_task = None
            if self._recompute_requested:
                self._schedule_recompute()

    async def async_refresh(self, *, deliver=False) -> None:
        if not self._current():
            return
        self._schedule_recompute()
        if self._recompute_task is not None:
            await asyncio.shield(self._recompute_task)
        if deliver and self._current() and self.runtime.phone_provisioning is not None:
            self.runtime.phone_provisioning.schedule()

    @callback
    def _refresh_tracked_entities(self, initial=False) -> bool:
        entity_registry = er.async_get(self.hass)
        new_set = {
            e.entity_id
            for e in entity_registry.entities.values()
            if _is_voip_roster_entity(e)
        }
        if new_set == self._tracked_entities and not initial:
            return False
        self._tracked_entities = new_set

        @callback
        def _on_state_change(event) -> None:
            entity_id = event.data.get("entity_id") or ""
            new_state = event.data.get("new_state")
            old_state = event.data.get("old_state")
            role = entity_role(er.async_get(self.hass).async_get(entity_id))
            if role == "voip_endpoint":
                old_value = old_state.state if old_state is not None else None
                new_value = new_state.state if new_state is not None else None
                old_endpoint = (
                    (old_state.attributes or {}).get("endpoint")
                    if old_state is not None
                    else None
                )
                new_endpoint = (
                    (new_state.attributes or {}).get("endpoint")
                    if new_state is not None
                    else None
                )
                if old_value != new_value or old_endpoint != new_endpoint:
                    self._schedule_recompute()
                return
            if (
                role in {"voip_ring_groups", "voip_conference_groups", "voip_conference_ring"}
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

        self._stop_state_tracking()
        if new_set:
            self._unsub_state = async_track_state_change_event(
                self.hass, list(new_set), _on_state_change
            )
        return True

    async def _recompute(self) -> None:
        compact_rows = []

        def prepare_peers(live_peers):
            peers = live_peers
            compact_rows.extend(format_entry_unified(peer) for peer in peers)
            return peers

        rows = await async_refresh_directory(self.hass, prepare_peers=prepare_peers)
        if not self._current():
            return
        if rows is None:
            # A concurrent directory refresh won. Re-read its current inputs
            # before publishing the legacy projection or attempting delivery.
            if self.runtime.directory is not None and not self.runtime.directory.closed:
                self._recompute_requested = True
            return
        from .repairs import async_sync_extension_issues

        async_sync_extension_issues(self.hass, rows)
        self._report_name_conflicts(rows)
        phonebook = ",".join(compact_rows)
        roster_json = dump_roster_json(rows)
        changed = self.phonebook != phonebook or self.roster_json != roster_json
        if changed:
            self.phonebook, self.roster_json, self.count = phonebook, roster_json, len(rows)
            from .services import async_update_phone_service_selectors

            await async_update_phone_service_selectors(self.hass, self.runtime.directory)
            if not self._current():
                return
            async_dispatcher_send(self.hass, PHONEBOOK_UPDATED, self)
        if changed and self._current() and self.runtime.phone_provisioning is not None:
            # Deliver LDAP configuration/refresh commands, never directory rows.
            self.runtime.phone_provisioning.schedule()

    @callback
    def _report_name_conflicts(self, entries) -> None:
        """Report ambiguous phonebook names once, until the conflict changes."""
        from .roster import normalize_roster_key

        names = {}
        for entry in entries:
            if not entry.enabled:
                continue
            key = normalize_roster_key(entry.display_name)
            if key:
                names.setdefault(key, []).append(entry)
        conflicts = []
        for matches in names.values():
            if len(matches) < 2:
                continue
            destinations = []
            for entry in matches:
                metadata = entry.metadata or {}
                kind = (
                    "Assist pipeline" if metadata.get("virtual_endpoint") == "assist_pipeline"
                    else "browser phone" if metadata.get("endpoint_kind") == "browser"
                    else "group" if metadata.get("group_type")
                    else "phone/contact"
                )
                extension = f", extension {entry.extension}" if entry.extension else ""
                destinations.append(f"{entry.display_name} ({kind}{extension})")
            conflicts.append("; ".join(sorted(destinations)))
        current = tuple(sorted(conflicts))
        if current == self._name_conflicts:
            return
        self._name_conflicts = current
        notification_id = "voip_stack_duplicate_phonebook_names"
        if not current:
            persistent_notification.async_dismiss(self.hass, notification_id)
            return
        message = (
            "Phonebook names must be unique, including browser phones and Assist pipelines. "
            "Rename one destination in each conflicting pair:\n\n"
            + "\n".join(f"- {conflict}" for conflict in current)
            + "\n\nUse the VoIP Stack phone settings, the contact editor, or the Assist "
            "pipeline settings to change the corresponding name. Different extension "
            "numbers do not make duplicate names valid."
        )
        _LOGGER.error("Duplicate VoIP phonebook names: %s. Rename the conflicting destinations.",
                      " | ".join(current))
        persistent_notification.async_create(
            self.hass, message, title="VoIP Stack: duplicate phonebook names",
            notification_id=notification_id,
        )
