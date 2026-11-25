"""Entry-owned directory refresh and Home Assistant presentation."""

import asyncio
import contextlib
import logging

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .device_resolver import entity_role
from .directory_runtime import async_refresh_directory
from .phonebook_runtime import format_entry_unified
from .roster import dump_roster_json
from .runtime_data import runtime_data

_LOGGER = logging.getLogger(__name__)
PHONEBOOK_UPDATED = "voip_stack_phonebook_updated"


def _is_camera_projection_entity(entity: object) -> bool:
    return getattr(entity, "platform", None) == "esphome" and entity_role(entity) == "camera"


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
            self._refresh_camera_entities(initial=True)
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
        if (_is_camera_projection_entity(entity) or entity_id in self._tracked_entities
                or event.data.get("old_entity_id") in self._tracked_entities):
            self._refresh_camera_entities()
            self._schedule_recompute()

    @callback
    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self._recompute_task is not None and self.runtime.phonebook is self and self.runtime.directory is not None:
            self.runtime.directory.begin_refresh()
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
    def _refresh_camera_entities(self, initial=False) -> bool:
        entity_registry = er.async_get(self.hass)
        new_set = {
            e.entity_id
            for e in entity_registry.entities.values()
            if _is_camera_projection_entity(e)
        }
        if new_set == self._tracked_entities and not initial:
            return False
        self._tracked_entities = new_set

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
        """Explain ambiguous dial aliases without forbidding shared labels."""
        from .phone_directory import PhoneDirectory
        from .roster import RosterError, normalize_roster_key

        entries = tuple(entry for entry in entries if entry.enabled)
        names = {}
        for entry in entries:
            key = normalize_roster_key(entry.display_name)
            if key:
                names.setdefault(key, []).append(entry)
        conflicts = []
        for matches in names.values():
            if len(matches) < 2:
                continue
            ambiguous = []
            for label in sorted({entry.display_name for entry in matches}):
                try:
                    PhoneDirectory.resolve_entries(entries, label)
                except RosterError:
                    ambiguous.append(label)
            # A public group/service name or account login can take precedence
            # over a presentation alias. Use the real resolver's decision.
            if not ambiguous:
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
                username = (f", username {entry.id}" if metadata.get("endpoint_kind") in
                            {"sip_account", "esphome", "browser", "ha_softphone"} else "")
                extension = f", extension {entry.extension}" if entry.extension else ""
                destinations.append(f"{entry.display_name} ({kind}{username}{extension})")
            conflicts.append(f"{', '.join(ambiguous)}: " + "; ".join(sorted(destinations)))
        current = tuple(sorted(conflicts))
        if current == self._name_conflicts:
            return
        self._name_conflicts = current
        notification_id = "voip_stack_duplicate_phonebook_names"
        if not current:
            persistent_notification.async_dismiss(self.hass, notification_id)
            return
        message = (
            "Phones may share a display name. The following names are ambiguous "
            "when dialed without a username or extension:\n\n"
            + "\n".join(f"- {conflict}" for conflict in current)
            + "\n\nUse the configured username or extension to select the intended "
            "destination. Renaming its display label is optional."
        )
        _LOGGER.warning("Ambiguous VoIP dial names: %s. Use a username or extension.",
                      " | ".join(current))
        persistent_notification.async_create(
            self.hass, message, title="VoIP Stack: ambiguous dial names",
            notification_id=notification_id,
        )
