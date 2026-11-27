"""Inspect and edit only stored duration fields while a migration is blocked.

This is a configuration boundary. The ordinary migration remains the sole
owner of initialization and schema advancement; recovery never clamps values.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass

from .duration_policy import SERVICE_TIMEOUT_LIMITS, validate_duration


@dataclass(frozen=True)
class DurationRepairField:
    """A known policy path; unrelated record fields are never exposed."""

    name: str
    path: tuple[str, ...]
    unit: str | None = "s"


@dataclass(frozen=True)
class DurationRepairOwner:
    """A snapshot of one saved duration policy, with its canonical validator."""

    identity: str
    title: str
    subentry_id: str | None
    path: tuple[str | int, ...]
    kind: str
    data: dict
    record_snapshot: dict
    fields: tuple[DurationRepairField, ...]

    def validated(self, values: Mapping | None = None) -> dict:
        result = deepcopy(self.data)
        known = {field.name: field for field in self.fields}
        if values is not None:
            if not isinstance(values, Mapping) or values.keys() - known.keys():
                raise ValueError("Unknown duration field")
            for name, value in values.items():
                if self.kind == "ring" and known[name].path[-1] == "step_timeout":
                    from .dial_plan import RingPolicy

                    # A new user submission is not an old legacy zero marker.
                    # Validate it before the migration's compatibility reader.
                    RingPolicy.from_metadata({"step_timeout": value})
                _set_path(result, known[name].path, value)
        _validate_policy(self.kind, result)
        return result

    def error(self) -> str:
        try:
            self.validated()
        except (ValueError, TypeError, KeyError, OverflowError) as error:
            return str(error)
        return ""


def _at_path(data, path):
    for key in path:
        data = data[key]
    return data


def _set_path(data, path, value) -> None:
    target = _at_path(data, path[:-1])
    target[path[-1]] = value


def _validate_policy(kind: str, data: Mapping) -> None:
    """Delegate constraints to existing owners, excluding unrelated settings."""
    from .duration_config import (
        INITIAL_PAGING_TIMEOUT,
        INITIAL_PARKING_TIMEOUT,
        INITIAL_SERVICE_TIMEOUTS,
        initial_service_timeouts,
    )

    if kind == "service":
        initial_service_timeouts(data)
    elif kind == "parking":
        from .parking_config import normalize_parking_config

        normalize_parking_config({
            "timeout": data.get("timeout", INITIAL_PARKING_TIMEOUT),
            "return_timeout": data.get("return_timeout"),
        })
    elif kind == "contact":
        metadata = data.get("metadata") or {}
        value = metadata.get("automation_timeout", data.get(
            "timeout", INITIAL_SERVICE_TIMEOUTS["automation_timeout"]))
        validate_duration(value, "automation_timeout")
    elif kind == "ring":
        from .dial_plan import RingPolicy
        from .group_config import materialize_ring_step

        keys = {"overall_timeout", "ring_timeout", "step_timeout"}
        policy = {key: value for key, value in data.items() if key in keys}
        if isinstance(data.get("ring_policy"), Mapping):
            policy["ring_policy"] = {key: value for key, value in data["ring_policy"].items() if key in keys}
        RingPolicy.from_metadata(materialize_ring_step(policy, legacy=True))
    elif kind == "queue":
        from .queue_config import initial_queue_policy

        initial_queue_policy({key: value for key, value in data.items()
                              if key in {"max_wait", "ring_timeout", "no_agent_wait", "retry_delay", "wrap_up"}})
    elif kind == "mailbox":
        from .mailbox_config import initial_mailbox_settings

        initial_mailbox_settings({key: value for key, value in data.items() if key == "max_seconds"})
    elif kind == "paging":
        from .ringing_policy import validate_ringing_timeout

        validate_duration(data.get("timeout", INITIAL_PAGING_TIMEOUT), "announcement_timeout")
        validate_ringing_timeout(data.get("answer_timeout"), inherit=True)
    elif kind == "advanced":
        from .sip_registration_policy import (
            AUDIO_RTCP_INTERVAL_FIELD, AUDIO_RTCP_MIN_INTERVAL,
            LOCAL_REDIRECT_HOPS_FIELD, LOCAL_REDIRECT_MAX_HOPS,
            registrar_policy_defaults, validate_registrar_policy,
        )

        validate_registrar_policy({**registrar_policy_defaults(), **data})
        for key, field in ((AUDIO_RTCP_MIN_INTERVAL, AUDIO_RTCP_INTERVAL_FIELD),
                           (LOCAL_REDIRECT_MAX_HOPS, LOCAL_REDIRECT_HOPS_FIELD)):
            if key in data:
                field.validate(data[key])
    elif kind == "trunk":
        from .sip_registration_policy import TRUNK_OPTIONS_FIELD, TRUNK_OPTIONS_INTERVAL

        TRUNK_OPTIONS_FIELD.validate(data[TRUNK_OPTIONS_INTERVAL])
    else:
        raise ValueError("Unknown duration owner")


def duration_repair_owners(entry, *, invalid_only: bool = True) -> list[DurationRepairOwner]:
    """Find editable policies without treating arbitrary storage as a form.

    Missing containers/fields are initialized by migration, not by recovery.
    Malformed containers and non-duration validation failures remain blocked;
    this narrowly scoped form cannot rewrite arbitrary configuration records.
    """
    from .const import CONF_PHONEBOOK_CONTACTS

    owners = []

    def add(kind, source, path, subentry_id, title):
        try:
            raw = _at_path(source, path)
        except (KeyError, TypeError, IndexError):
            return
        if not isinstance(raw, Mapping):
            return
        data = deepcopy(dict(raw))
        if kind == "service":
            fields = [DurationRepairField(key, (key,)) for key in SERVICE_TIMEOUT_LIMITS if key in data]
        elif kind == "advanced":
            from .sip_registration_policy import (
                AUDIO_RTCP_MIN_INTERVAL, LOCAL_REDIRECT_MAX_HOPS, REGISTRAR_POLICY_FIELDS,
            )

            counts = {"registrar_contact_limit", "registrar_auth_failure_threshold", LOCAL_REDIRECT_MAX_HOPS}
            fields = [DurationRepairField(key, (key,), None if key in counts else "s")
                      for key in (*REGISTRAR_POLICY_FIELDS, AUDIO_RTCP_MIN_INTERVAL, LOCAL_REDIRECT_MAX_HOPS)
                      if key in data]
        elif kind == "trunk":
            from .sip_registration_policy import TRUNK_OPTIONS_INTERVAL

            fields = [DurationRepairField(TRUNK_OPTIONS_INTERVAL, (TRUNK_OPTIONS_INTERVAL,))]
            if TRUNK_OPTIONS_INTERVAL not in data:
                return
        elif kind == "contact":
            metadata = data.get("metadata") or {}
            if not isinstance(metadata, Mapping) or not (data.get("type") == "automation"
                    or metadata.get("virtual_endpoint") == "automation"):
                return
            fields = ([DurationRepairField("automation_timeout", ("metadata", "automation_timeout"))]
                      if "automation_timeout" in metadata else
                      [DurationRepairField("automation_timeout", ("timeout",))] if "timeout" in data else [])
        else:
            keys = {
                "parking": ("timeout", "return_timeout"),
                "ring": ("step_timeout", "overall_timeout", "ring_timeout"),
                "queue": ("max_wait", "ring_timeout", "no_agent_wait", "retry_delay", "wrap_up"),
                "mailbox": ("max_seconds",),
                "paging": ("timeout", "answer_timeout"),
            }[kind]
            inherited = {"return_timeout", "overall_timeout", "ring_timeout", "answer_timeout"}
            fields = [DurationRepairField(key, (key,)) for key in keys
                      if key in data and not (key in inherited and data[key] is None)]
            if kind == "ring" and isinstance(data.get("ring_policy"), Mapping):
                fields.extend(DurationRepairField("nested_" + key, ("ring_policy", key))
                              for key in keys if key in data["ring_policy"]
                              and not (key in inherited and data["ring_policy"][key] is None))
        if not fields:
            return
        # Advanced PBX policy and a residual parent trunk share the root path,
        # but an edit must retain the specific policy owner selected by the user.
        identity = repr((kind, subentry_id, path))
        # Native IDs are stable; a legacy account's list index is not. Retain
        # the containing account so reordering equal mailbox policies cannot
        # redirect a submitted edit to another owner.
        snapshot = source if subentry_id else (
            _at_path(source, path[:2]) if path[:1] == ("sip_accounts",) else raw)
        owner = DurationRepairOwner(identity, title, subentry_id, path, kind, data,
                                    deepcopy(dict(snapshot)), tuple(fields))
        if not invalid_only or owner.error():
            owners.append(owner)

    add("service", entry.data, ("service_timeouts",), None, "PBX / service_timeouts")
    add("advanced", entry.data, (), None, "PBX / advanced")
    if "trunk_enabled" in entry.data:
        add("trunk", entry.data, (), None, "PBX / legacy trunk")
    add("parking", entry.data, ("parking",), None, "PBX / parking")
    contacts = entry.data.get(CONF_PHONEBOOK_CONTACTS)
    if isinstance(contacts, list):
        for index, raw in enumerate(contacts):
            title = str(raw.get("name") or raw.get("id") or index) if isinstance(raw, Mapping) else str(index)
            add("contact", entry.data, (CONF_PHONEBOOK_CONTACTS, index), None, "Contact / " + title)
    mailboxes = entry.data.get("esphome_mailboxes")
    if isinstance(mailboxes, Mapping):
        for identity in mailboxes:
            add("mailbox", entry.data, ("esphome_mailboxes", identity), None, "Mailbox / " + str(identity))
    accounts = entry.data.get("sip_accounts")
    if isinstance(accounts, list):
        for index, account in enumerate(accounts):
            if isinstance(account, Mapping):
                title = str(account.get("name") or account.get("username") or index)
                add("mailbox", entry.data, ("sip_accounts", index, "voicemail"), None, "Mailbox / " + title)
    for row in entry.subentries.values():
        if not isinstance(row.data, Mapping):
            continue
        kind = row.subentry_type
        if kind == "group" and row.data.get("kind") in {"ring", "queue"}:
            add(row.data["kind"], row.data, ("policy",), row.subentry_id, row.title)
        elif kind == "phone":
            add("mailbox", row.data, ("voicemail",), row.subentry_id, row.title)
        elif kind in {"contact", "paging", "trunk"}:
            add(kind, row.data, (), row.subentry_id, row.title)
    return owners


def duration_repair_update(entry, selected: DurationRepairOwner, values: Mapping):
    """Prepare one explicit user correction without altering any other field."""
    current = next((owner for owner in duration_repair_owners(entry, invalid_only=False)
                    if owner.identity == selected.identity), None)
    if (current is None or current.kind != selected.kind or current.title != selected.title
            or current.data != selected.data or current.record_snapshot != selected.record_snapshot):
        raise RuntimeError("configuration_changed")
    proposed = current.validated(values)
    row = entry.subentries.get(current.subentry_id) if current.subentry_id else None
    data = deepcopy(dict(row.data if row is not None else entry.data))
    if current.path:
        _set_path(data, current.path, proposed)
    else:
        data = proposed
    return row, data
