"""One public directory projection of the existing persistent phone owners."""

from copy import deepcopy
from dataclasses import replace
import logging

from .roster import RosterEntry, RosterError, normalize_roster_key

_LOGGER = logging.getLogger(__name__)
_PRIVATE_FIELDS = {"password", "auth_password", "secret", "token", "pin", "pin_hash", "authorization"}


def public_metadata(value):
    """Own public metadata while keeping credentials outside the directory."""
    if isinstance(value, dict):
        return {key: public_metadata(item) for key, item in value.items()
                if str(key).casefold() not in _PRIVATE_FIELDS}
    if isinstance(value, (list, tuple)):
        return type(value)(public_metadata(item) for item in value)
    return deepcopy(value)


class PhoneDirectory:
    """A derived runtime snapshot, never another account or persistence store."""

    def __init__(self, entries=()):
        self._entries = {}
        self._subscribers = set()
        self.generation = 0
        self._refresh_generation = 0
        self.closed = False
        self.advertise_host = ""
        self.replace(entries)

    @staticmethod
    def _key(identity):
        key = str(identity or "").strip().casefold()
        if not key:
            raise RosterError("Directory entries require an identity")
        return key

    @staticmethod
    def _copy(entry):
        return replace(entry, metadata=public_metadata(entry.metadata))

    def snapshot(self) -> tuple[RosterEntry, ...]:
        return tuple(self._copy(entry) for entry in self._entries.values())

    def get(self, identity) -> RosterEntry | None:
        entry = self._entries.get(self._key(identity))
        return self._copy(entry) if entry is not None else None

    def resolve(self, target) -> RosterEntry | None:
        direct = self.get(target)
        if direct is not None:
            return direct
        key = normalize_roster_key(str(target or ""))
        matches = [entry for entry in self._entries.values() if entry.enabled and key and key in {
            normalize_roster_key(value) for value in (entry.id, entry.name, entry.extension, entry.number) if value}]
        if len(matches) > 1:
            raise RosterError("Ambiguous directory destination")
        return self._copy(matches[0]) if matches else None

    def begin_refresh(self):
        self._refresh_generation += 1
        return self._refresh_generation

    def owns_refresh(self, generation):
        return not self.closed and generation == self._refresh_generation

    def replace(self, entries, *, refresh_generation=None):
        if self.closed or (refresh_generation is not None and refresh_generation != self._refresh_generation):
            return False
        wanted = {}
        for entry in entries:
            key = self._key(entry.metadata.get("endpoint_id") or entry.id)
            if key in wanted:
                raise RosterError("Duplicate directory identity")
            wanted[key] = self._copy(entry)
        if list(wanted.items()) == list(self._entries.items()):
            return False
        self._entries = wanted
        self._changed()
        return True

    def remove(self, identity):
        if self.closed or self._entries.pop(self._key(identity), None) is None:
            return False
        self._refresh_generation += 1
        self._changed()
        return True

    def upsert(self, entry):
        if self.closed:
            return False
        key = self._key(entry.metadata.get("endpoint_id") or entry.id)
        owned = self._copy(entry)
        if self._entries.get(key) == owned:
            return False
        self._refresh_generation += 1
        self._entries[key] = owned
        self._changed()
        return True

    def subscribe(self, callback):
        if self.closed:
            raise RuntimeError("Directory is closed")
        self._subscribers.add(callback)
        return lambda: self._subscribers.discard(callback)

    def _changed(self):
        self.generation += 1
        for subscriber in tuple(self._subscribers):
            try:
                subscriber()
            except Exception as error:
                _LOGGER.warning("Directory observer failed (%s)", type(error).__name__)

    def close(self):
        self.closed = True
        self._refresh_generation += 1
        self._entries.clear()
        self._subscribers.clear()
