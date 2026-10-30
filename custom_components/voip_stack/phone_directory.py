"""The runtime phonebook shared by routing, presentation and LDAP."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import replace
import logging

from .roster import RosterEntry, RosterError

_LOGGER = logging.getLogger(__name__)


class PhoneDirectory:
    """One canonical set of existing roster records, independent of reachability.

    The HA profile and endpoint owners update these records. Consumers share
    their identities and never create a separate LDAP database. Offline phones
    remain until their owner explicitly removes them.
    """

    def __init__(self, entries: Iterable[RosterEntry] = ()) -> None:
        self._entries: dict[str, RosterEntry] = {}
        self._subscribers: set[Callable[[], None]] = set()
        self.generation = 0
        self.replace(entries)

    @staticmethod
    def _key(identity: str) -> str:
        key = str(identity).strip().casefold()
        if not key:
            raise RosterError("Directory entries need an identity")
        return key

    @staticmethod
    def _owned(entry: RosterEntry) -> RosterEntry:
        # The record remains the existing public routing type. Take ownership
        # of its metadata mapping so a producer's later update is explicit.
        return replace(entry, metadata=dict(entry.metadata))

    def snapshot(self) -> tuple[RosterEntry, ...]:
        """Return the same records to every phonebook consumer."""
        return tuple(self._entries.values())

    def get(self, identity: str) -> RosterEntry | None:
        return self._entries.get(self._key(identity))

    def upsert(self, entry: RosterEntry) -> bool:
        key = self._key(str(entry.metadata.get("endpoint_id") or entry.id))
        if self._entries.get(key) == entry:
            return False
        self._entries[key] = self._owned(entry)
        self._changed()
        return True

    def remove(self, identity: str) -> bool:
        if self._entries.pop(self._key(identity), None) is None:
            return False
        self._changed()
        return True

    def replace(self, entries: Iterable[RosterEntry]) -> bool:
        wanted: dict[str, RosterEntry] = {}
        for entry in entries:
            key = self._key(str(entry.metadata.get("endpoint_id") or entry.id))
            if key in wanted:
                raise RosterError("Duplicate directory identity")
            wanted[key] = self._owned(entry)
        if list(wanted.items()) == list(self._entries.items()):
            return False
        self._entries = wanted
        self._changed()
        return True

    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._subscribers.add(callback)
        return lambda: self._subscribers.discard(callback)

    def _changed(self) -> None:
        self.generation += 1
        for subscriber in tuple(self._subscribers):
            try:
                subscriber()
            except Exception as error:
                _LOGGER.warning("Directory observer failed (%s)", type(error).__name__)
