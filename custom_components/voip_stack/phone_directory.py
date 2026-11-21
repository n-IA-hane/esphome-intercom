"""One public directory projection of the existing persistent phone owners."""

from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import logging

from .roster import RosterEntry, RosterError, normalize_roster_key

_LOGGER = logging.getLogger(__name__)
_PRIVATE_FIELDS = {"password", "auth_password", "secret", "token", "pin", "pin_hash", "authorization"}


def service_directory_reference(entry):
    """Use the same existing service identity in composition and draft validation."""
    service = str(entry.metadata["virtual_endpoint"])
    return entry.metadata.get("endpoint_id") or (
        "service:assist_pipeline" if service == "assist_pipeline" else f"service:{service}:{entry.id}")


def contact_key_reference(entry):
    """A configured contact is callable, but is never a phone/BLF principal."""
    if (entry.metadata.get("endpoint_kind", "contact") != "contact"
            or entry.metadata.get("virtual_endpoint") or entry.metadata.get("group_type")
            or entry.metadata.get("local_ha") or not (entry.number or entry.address or entry.sip_uri)):
        return None
    return "contact:" + str(entry.id)


def contact_dial_target(entry):
    """Recognize the SIP user written by older physical phone provisioning."""
    reference = contact_key_reference(entry)
    if reference is None:
        raise ValueError("invalid_phone_key_target")
    return "contact+" + sha256(reference.casefold().encode()).hexdigest()[:20]


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
        self._public_aliases = None
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

    def programmable_contacts(self):
        return tuple(self._copy(entry) for entry in self._entries.values()
                     if entry.enabled and contact_key_reference(entry) is not None)

    def key_contact(self, reference):
        wanted = str(reference).casefold()
        matches = [entry for entry in self._entries.values()
                   if str(contact_key_reference(entry) or "").casefold() == wanted]
        if len(matches) > 1:
            raise ValueError("invalid_phone_key_target")
        return self._copy(matches[0]) if matches and matches[0].enabled else None

    def dial_users(self, entry, values=None):
        """Return public SIP users that the PBX routes to this directory record."""
        from .router import RouteAction, RouteReason, resolve_ha_router

        contact = contact_key_reference(entry) is not None
        if values is None:
            first = entry.number if contact and not (entry.sip_uri or entry.address) else entry.extension
            values = (first, entry.display_name)
        for value in dict.fromkeys(filter(None, values)):
            try:
                route = resolve_ha_router(value, self._matching_rows(normalize_roster_key(value)), trunk_ready=contact)
            except ValueError:
                continue
            if route.entry == entry and (
                route.action is not RouteAction.REJECT or route.reason is RouteReason.TARGET_UNREACHABLE
            ):
                yield value

    def _matching_rows(self, key):
        # Project aliases once per directory generation so an LDAP search can
        # check every number without rescanning the entire phonebook for each.
        if self._public_aliases is None:
            aliases = {}
            for row in self._entries.values():
                values = (row.id, row.name, row.extension, row.number)
                for alias in {normalize_roster_key(value) for value in values if value}:
                    aliases.setdefault(alias, []).append(row)
            self._public_aliases = aliases
        return self._public_aliases.get(key, ())

    def get(self, identity) -> RosterEntry | None:
        entry = self._entries.get(self._key(identity))
        return self._copy(entry) if entry is not None else None

    @staticmethod
    def resolve_entries(entries, target, *, include_extension=True, include_number=True,
                        literal_only=False, include_internal_ids=False):
        """Resolve exact public aliases first, then the shared legacy spelling.

        Internal references are an explicit adapter for persisted membership,
        never an implicit public destination namespace. Every selected tier
        rejects aliases owned by different destinations.
        """
        raw = str(target or "").strip().casefold()
        if not raw:
            return None

        def aliases(entry, *, internal):
            values = [entry.id, entry.name]
            if include_extension:
                values.append(entry.extension)
            if include_number:
                values.append(getattr(entry, "number", ""))
            if internal and include_internal_ids:
                metadata = getattr(entry, "metadata", {})
                values.extend((metadata.get("endpoint_id"), metadata.get("device_id")))
            return [str(value).strip() for value in values if value]

        rows = tuple(entries)
        for internal, normalize in ((True, lambda value: value.casefold()), (False, normalize_roster_key)):
            wanted = normalize(raw)
            matches = {}
            for entry in rows:
                if wanted and any(normalize(value) == wanted for value in aliases(entry, internal=internal)):
                    identity = str(getattr(entry, "metadata", {}).get("endpoint_id") or entry.id).casefold()
                    matches[identity] = entry
            if len(matches) > 1:
                raise RosterError("Ambiguous directory destination")
            if matches:
                return next(iter(matches.values()))
            if literal_only:
                break
        return None

    def resolve(self, target) -> RosterEntry | None:
        key = normalize_roster_key(str(target or ""))
        rows = (entry for entry in self._matching_rows(key) if entry.enabled) if key else ()
        entry = self.resolve_entries(rows, target)
        return self._copy(entry) if entry is not None else None

    def resolve_reference(self, target) -> RosterEntry | None:
        """Adapt saved configuration references with the same ambiguity checks."""
        entry = self.resolve_entries((row for row in self._entries.values() if row.enabled),
                                     target, include_internal_ids=True)
        return self._copy(entry) if entry is not None else None

    def caller_display_name(self, user: str) -> str:
        """Return a name only for a unique published caller number or SIP user."""
        key = normalize_roster_key(user)
        matches = [
            row for row in self._matching_rows(key)
            if row.enabled and not row.metadata.get("virtual_endpoint") and not row.metadata.get("group_type")
            and any(normalize_roster_key(value) == key for value in (row.id, row.extension, row.number) if value)
        ]
        return matches[0].name if len(matches) == 1 else ""

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
        self._public_aliases = None
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
        self._public_aliases = None
        self._subscribers.clear()
