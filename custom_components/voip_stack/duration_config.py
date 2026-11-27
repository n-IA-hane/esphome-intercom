"""Initial duration values used only at configuration and migration boundaries."""

from __future__ import annotations

from collections.abc import Mapping

from .duration_policy import CONF_SERVICE_TIMEOUTS, validate_duration

# These values are stored when the user creates or upgrades configuration.
# Runtime consumers must read duration_policy, never this initial-value table.
INITIAL_SERVICE_TIMEOUTS = {
    "contact_step_timeout": 15,
    "media_timeout": 120,
    "dtmf_timeout": 10,
    "automation_timeout": 30,
    "announcement_timeout": 120,
    "unanswered_wait": 60,
    "transcription_timeout": 120,
    "recording_timeout": 3600,
    "ivr_digit_timeout": 30,
    "ivr_interdigit_timeout": 10,
    "ivr_prompt_timeout": 30,
    "voicemail_menu_setup_timeout": 30,
    "voicemail_greeting_timeout": 30,
    "conference_sip_ringing_timeout": 30,
}
INITIAL_PARKING_TIMEOUT = 180
INITIAL_PAGING_TIMEOUT = 120


def initial_service_timeouts(value=None) -> dict:
    """Prefill missing fields and validate submitted or persisted choices."""
    if value is not None and not isinstance(value, Mapping):
        raise ValueError("Invalid action duration configuration")
    settings = dict(value or {})
    for key, initial in INITIAL_SERVICE_TIMEOUTS.items():
        settings.setdefault(key, initial)
        validate_duration(settings[key], key)
    return settings


def initialize_duration_config(data: Mapping) -> dict:
    """Materialize advanced policies only at configuration/migration boundaries."""
    from .sip_registration_policy import (registrar_policy_defaults, validate_registrar_policy,
        AUDIO_RTCP_MIN_INTERVAL, AUDIO_RTCP_INTERVAL_FIELD, LOCAL_REDIRECT_MAX_HOPS, LOCAL_REDIRECT_HOPS_FIELD)
    result = dict(data)
    for key, initial in registrar_policy_defaults().items():
        result.setdefault(key, initial)
    validate_registrar_policy(result)
    result.setdefault(AUDIO_RTCP_MIN_INTERVAL, AUDIO_RTCP_INTERVAL_FIELD.default)
    AUDIO_RTCP_INTERVAL_FIELD.validate(result[AUDIO_RTCP_MIN_INTERVAL])
    result.setdefault(LOCAL_REDIRECT_MAX_HOPS, LOCAL_REDIRECT_HOPS_FIELD.default)
    LOCAL_REDIRECT_HOPS_FIELD.validate(result[LOCAL_REDIRECT_MAX_HOPS])
    result[CONF_SERVICE_TIMEOUTS] = initial_service_timeouts(
        result.get(CONF_SERVICE_TIMEOUTS)
    )
    # This recognized key belonged exclusively to the retired trunk collector.
    result[CONF_SERVICE_TIMEOUTS].pop("trunk_first_digit_timeout", None)
    parking = result.get("parking", {})
    if not isinstance(parking, Mapping):
        raise ValueError("Invalid parking configuration")
    parking = dict(parking)
    parking.setdefault("timeout", INITIAL_PARKING_TIMEOUT)
    result["parking"] = parking
    return result


def duration_migration_plan(entry) -> tuple[dict, list[tuple[object, dict]]]:
    """Preflight all duration owners before the local 7.7 semantic bridge."""
    from .const import CONF_PHONEBOOK_CONTACTS
    from .config import normalized_trunk_config
    from .contact_config import materialize_contact_timeout
    from .dial_plan import RingPolicy
    from .group_config import materialize_ring_step
    from .mailbox_config import initial_mailbox_settings
    from .paging_config import normalize_paging_group
    from .parking_config import normalize_parking_config
    from .queue_config import initial_queue_policy
    from .roster import parse_roster_json

    from .trunk_config import (
        PENDING_TRUNK_MENU_NOTICES, legacy_trunk_collects_digits,
        migrated_trunk_routing_data, pending_trunk_menu_notices,
    )
    pending_trunk_menu_notices(entry)
    legacy_trunk_collects_digits(entry.data)
    if "trunk_enabled" in entry.data:
        normalized_trunk_config(entry.data)
    data = migrated_trunk_routing_data(initialize_duration_config(entry.data))
    data.pop(PENDING_TRUNK_MENU_NOTICES, None)
    normalize_parking_config(data["parking"])
    # Existing contacts used this initial value before a stored default existed.
    # Changing a PBX default must not rewrite a saved per-contact policy.
    contact_timeout = INITIAL_SERVICE_TIMEOUTS["automation_timeout"]
    if CONF_PHONEBOOK_CONTACTS in data:
        contacts = data[CONF_PHONEBOOK_CONTACTS]
        if not isinstance(contacts, list):
            raise ValueError("Invalid legacy contact configuration")
        contacts = [materialize_contact_timeout(raw, contact_timeout) for raw in contacts]
        parse_roster_json(contacts)
        data[CONF_PHONEBOOK_CONTACTS] = contacts
    # Most legacy ESP mailbox records were moved by 7.4. Preserve any residual
    # records and their identities while materializing the same stored limit.
    if "esphome_mailboxes" in data:
        mailboxes = data["esphome_mailboxes"]
        if not isinstance(mailboxes, Mapping):
            raise ValueError("Invalid legacy mailbox configuration")
        data["esphome_mailboxes"] = {
            identity: initial_mailbox_settings(settings) for identity, settings in mailboxes.items()
        }
    if "sip_accounts" in data:
        accounts = data["sip_accounts"]
        if not isinstance(accounts, list):
            raise ValueError("Invalid legacy account configuration")
        data["sip_accounts"] = [
            {**account, "voicemail": initial_mailbox_settings(account["voicemail"])}
            if isinstance(account, Mapping) and "voicemail" in account else account
            for account in accounts
        ]
    updates = []
    for record in entry.subentries.values():
        previous = dict(record.data)
        proposed = dict(previous)
        if record.subentry_type == "group" and previous.get("kind") == "ring":
            policy = materialize_ring_step(previous.get("policy"), legacy=True)
            RingPolicy.from_metadata(policy)
            proposed["policy"] = policy
        elif record.subentry_type == "group" and previous.get("kind") == "queue":
            proposed["policy"] = initial_queue_policy(previous.get("policy"))
        elif record.subentry_type == "phone" and "voicemail" in previous:
            proposed["voicemail"] = initial_mailbox_settings(previous["voicemail"])
        elif record.subentry_type == "contact":
            proposed = materialize_contact_timeout(previous, contact_timeout)
            parse_roster_json([proposed])
        elif record.subentry_type == "paging":
            proposed.setdefault("timeout", INITIAL_PAGING_TIMEOUT)
            normalize_paging_group(proposed, identity=record.subentry_id)
        elif record.subentry_type == "trunk":
            from .sip_registration_policy import TRUNK_OPTIONS_INTERVAL, TRUNK_OPTIONS_FIELD
            proposed.setdefault(TRUNK_OPTIONS_INTERVAL, TRUNK_OPTIONS_FIELD.default)
            TRUNK_OPTIONS_FIELD.validate(proposed[TRUNK_OPTIONS_INTERVAL])
            legacy_trunk_collects_digits(previous)
            proposed = migrated_trunk_routing_data(proposed)
            normalized_trunk_config(proposed)
        else:
            continue
        if proposed != previous:
            updates.append((record, proposed))
    return data, updates


def migrate_duration_policies(hass, entry) -> None:
    """Apply an idempotent plan; advance the schema only after all writes succeed."""
    # Validate every owner before the first write, including collector fields
    # which can fail before the duration phase on installations still at 7.5.
    duration_migration_plan(entry)
    from .trunk_config import LEGACY_TRUNK_MENU_KEYS, PENDING_TRUNK_MENU_NOTICES, migrate_trunk_menus
    if ("trunk_enabled" in entry.data or PENDING_TRUNK_MENU_NOTICES in entry.data
            or LEGACY_TRUNK_MENU_KEYS.intersection(entry.data)) or any(
            row.subentry_type == "trunk" and LEGACY_TRUNK_MENU_KEYS.intersection(row.data)
            for row in entry.subentries.values()):
        # Preserve the existing owner and per-trunk notices after preflight.
        migrate_trunk_menus(hass, entry)
    # Moving a residual parent trunk changes both the parent and subentry set.
    # Replan from that result instead of restoring the stale pre-move snapshot.
    data, updates = duration_migration_plan(entry)
    for record, proposed in updates:
        hass.config_entries.async_update_subentry(entry, record, data=proposed)
    hass.config_entries.async_update_entry(entry, data=data, minor_version=7)
