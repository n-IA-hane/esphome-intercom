"""Native HA records for independently configured SIP trunks."""

from .config import normalized_trunk_config
from .trunk_policy import validate_static_trunk_sources

TRUNK_SUBENTRY_TYPE = "trunk"
CONF_DEFAULT_TRUNK = "default_trunk_id"
TRUNK_DEVICE_MODEL = "SIP trunk"
TRUNK_UPDATE_SIGNAL = "voip_stack_trunk_updated"


def trunk_subentries(entry):
    return [item for item in entry.subentries.values() if item.subentry_type == TRUNK_SUBENTRY_TYPE]


def entry_trunk_configs(entry) -> dict[str, dict]:
    records = trunk_subentries(entry)
    configs = {str(item.data.get("trunk_id") or item.subentry_id): normalized_trunk_config(item.data) for item in records}
    if len(configs) != len(records):
        raise ValueError("Trunk identities must be unique")
    validate_static_trunk_sources(configs)
    return configs


def entry_default_trunk(entry, configs) -> str:
    selected = str(entry.data.get(CONF_DEFAULT_TRUNK) or "")
    if CONF_DEFAULT_TRUNK in entry.data:
        return selected if selected in configs else ""
    return next(iter(configs)) if len(configs) == 1 else ""


def clear_removed_trunk_default(hass, entry, configs) -> None:
    """Forget a deleted provider without silently selecting a replacement."""
    selected = str(entry.data.get(CONF_DEFAULT_TRUNK) or "")
    if selected and selected not in configs:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_DEFAULT_TRUNK: ""}
        )


def _legacy_trunk_is_empty(data) -> bool:
    """Recognize only the disabled, unconfigured form left by old versions."""
    from .sip_registration_policy import TRUNK_OPTIONS_INTERVAL, TRUNK_OPTIONS_FIELD

    if data.get("trunk_enabled") or legacy_trunk_collects_digits(data):
        return False
    defaults = {**normalized_trunk_config({}),
                TRUNK_OPTIONS_INTERVAL: TRUNK_OPTIONS_FIELD.default,
                "trunk_inbound_unknown_target": "fallback"}
    # Unknown or nondefault settings are evidence of a configured line. The
    # retired, inactive collector fields cannot describe a live destination.
    return all(
        key in LEGACY_TRUNK_MENU_KEYS or (
            key in defaults and (value is None or value == "" or value == defaults[key])
        )
        for key, value in data.items()
        if key.startswith("trunk_") and key != PENDING_TRUNK_MENU_NOTICES
    )


def _migration_trunk_config(data):
    """Leave the newer probe field for the current policy preflight/Repair."""
    from .sip_registration_policy import TRUNK_OPTIONS_INTERVAL

    config = normalized_trunk_config({key: value for key, value in data.items()
                                      if key != TRUNK_OPTIONS_INTERVAL})
    if TRUNK_OPTIONS_INTERVAL in data:
        config[TRUNK_OPTIONS_INTERVAL] = data[TRUNK_OPTIONS_INTERVAL]
    return config


def _legacy_trunk_matches(parent, record):
    from .sip_registration_policy import TRUNK_OPTIONS_INTERVAL

    config = _migration_trunk_config(parent)
    candidate = _migration_trunk_config(record.data)
    if TRUNK_OPTIONS_INTERVAL not in parent:
        candidate.pop(TRUNK_OPTIONS_INTERVAL, None)
    # Selecting a default is not identity evidence. Compare the definition and
    # any unrecognized parent fields before adopting an existing native owner.
    return candidate == config and all(
        record.data.get(key) == value for key, value in parent.items()
        if key.startswith("trunk_") and key not in config and key not in LEGACY_TRUNK_MENU_KEYS
        and key not in {"trunk_id", "trunk_inbound_unknown_target", PENDING_TRUNK_MENU_NOTICES}
    )


def legacy_trunk_source_conflicts(entry):
    """Return native owners whose enabled static source rules overlap the parent."""
    from .trunk_policy import trusted_networks

    if "trunk_enabled" not in entry.data:
        return []
    parent = _migration_trunk_config(entry.data)
    if (not parent["trunk_enabled"] or parent["trunk_register"] or parent["trunk_inbound_account"]):
        return []
    records = trunk_subentries(entry)
    if any(_legacy_trunk_matches(entry.data, row) for row in records):
        return []
    networks = trusted_networks(parent["trunk_allowed_ips"])
    conflicts = []
    for row in records:
        config = _migration_trunk_config(row.data)
        if (not config["trunk_enabled"] or config["trunk_register"] or config["trunk_inbound_account"]
                or config["trunk_transport"] != parent["trunk_transport"]):
            continue
        if any(left.version == right.version and left.overlaps(right)
               for left in networks for right in trusted_networks(config["trunk_allowed_ips"])):
            conflicts.append(row)
    return conflicts


def migrate_legacy_trunk(hass, entry) -> None:
    """Move the former parent-entry trunk into a stable native record once."""
    from uuid import uuid4
    from homeassistant.config_entries import ConfigSubentry
    from .sip_registration_policy import TRUNK_OPTIONS_INTERVAL

    if "trunk_enabled" not in entry.data:
        return

    parent = {key: value for key, value in entry.data.items()
              if not key.startswith("trunk_") or key == PENDING_TRUNK_MENU_NOTICES}
    if _legacy_trunk_is_empty(entry.data):
        # Do this before choosing a native record: an empty parent must neither
        # create a phantom line nor replace an already configured provider.
        hass.config_entries.async_update_entry(entry, data=parent)
        return

    records = trunk_subentries(entry)
    configs = {str(item.data.get("trunk_id") or item.subentry_id): _migration_trunk_config(item.data)
               for item in records}
    if len(configs) != len(records):
        raise ValueError("Trunk identities must be unique")
    validate_static_trunk_sources(configs)
    if legacy_trunk_source_conflicts(entry):
        from .repairs import async_sync_trunk_migration_source_issue

        async_sync_trunk_migration_source_issue(hass, entry, blocked=True)
        raise ValueError("Legacy and native static trunks have overlapping source rules")
    selected = entry_default_trunk(entry, configs)
    config = _migration_trunk_config(entry.data)
    legacy_id = entry.data.get("trunk_id")
    if legacy_id is not None and (not isinstance(legacy_id, str) or not legacy_id.strip()):
        raise ValueError("Invalid legacy trunk identity")
    matches = [record for record in records if _legacy_trunk_matches(entry.data, record)]
    identified = next((record for record in records
                       if str(record.data.get("trunk_id") or record.subentry_id) == legacy_id), None)
    if identified is not None and identified in matches:
        current = identified
    elif len(matches) == 1:
        current = matches[0]
    else:
        current = next((record for record in matches
                        if str(record.data.get("trunk_id") or record.subentry_id) == selected), None)
        if matches and current is None:
            current = min(matches, key=lambda record: record.subentry_id)
    if current is not None and TRUNK_OPTIONS_INTERVAL in current.data:
        # A parent saved by the old schema may not know this newer field.
        config.setdefault(TRUNK_OPTIONS_INTERVAL, current.data[TRUNK_OPTIONS_INTERVAL])
    identity = (str(current.data.get("trunk_id") or current.subentry_id) if current is not None
                else legacy_id if legacy_id and legacy_id not in configs else uuid4().hex)
    title = current.title if current is not None else "SIP trunk"
    if current is None:
        data = {**config, **{key: value for key, value in entry.data.items()
                            if key.startswith("trunk_") and key != PENDING_TRUNK_MENU_NOTICES},
                "trunk_id": identity, "name": title}
        if legacy_id and identity != legacy_id:
            data["legacy_trunk_id"] = legacy_id
    else:
        # Equivalence allows retiring the parent without replacing the native
        # record's literal settings, extension metadata or stable identity.
        data = {**current.data, **{key: value for key, value in entry.data.items()
                                  if key in LEGACY_TRUNK_MENU_KEYS}}
    validate_static_trunk_sources({**configs, identity: _migration_trunk_config(data)})
    # Disabled is saved configuration, not permission to discard the line.
    if current is None:
        hass.config_entries.async_add_subentry(entry, ConfigSubentry(
            subentry_type=TRUNK_SUBENTRY_TYPE, title=title, data=data, unique_id=f"trunk:{identity}",
        ))
    else:
        hass.config_entries.async_update_subentry(entry, current, data=data)
    if CONF_DEFAULT_TRUNK not in entry.data:
        # Materializing a second provider must preserve the previously implicit
        # choice as well. An explicit choice, including empty, is never changed.
        parent[CONF_DEFAULT_TRUNK] = selected or (identity if not records else "")
    hass.config_entries.async_update_entry(entry, data=parent)
    from .repairs import async_sync_trunk_migration_source_issue

    async_sync_trunk_migration_source_issue(hass, entry)


def sync_trunk_devices(hass, entry) -> None:
    """Expose stable named line devices for native HA selectors."""
    from homeassistant.helpers import device_registry as dr
    from .const import DOMAIN

    devices = dr.async_get(hass)
    for record in trunk_subentries(entry):
        identity = str(record.data.get("trunk_id") or record.subentry_id)
        devices.async_get_or_create(
            config_entry_id=entry.entry_id, config_subentry_id=record.subentry_id,
            identifiers={(DOMAIN, f"trunk:{identity}")}, name=record.title,
            manufacturer="VoIP Stack", model=TRUNK_DEVICE_MODEL,
            entry_type=dr.DeviceEntryType.SERVICE,
        )


LEGACY_TRUNK_MENU_KEYS = frozenset({
    "trunk_inbound_mode", "trunk_dtmf_enabled", "trunk_dtmf_timeout_ms", "trunk_dtmf_terminator",
})
PENDING_TRUNK_MENU_NOTICES = "trunk_menu_retirement_pending"


def pending_trunk_menu_notices(entry) -> list[str]:
    """Read the durable retirement journal without arbitrary stored values."""
    pending = entry.data.get(PENDING_TRUNK_MENU_NOTICES, [])
    if not isinstance(pending, list) or any(not isinstance(item, str) or not item for item in pending):
        raise ValueError("Invalid pending trunk menu notice")
    return list(dict.fromkeys(pending))


def trunk_menu_notice_id(subentry_id: str) -> str:
    """Keep the native issue identity shared by publication and completion."""
    return "trunk_dtmf_retired_" + subentry_id


def replay_trunk_menu_notices(hass, entry) -> None:
    """Recover missing notices without repeating a completed policy migration."""
    from homeassistant.helpers import issue_registry as ir
    from .const import DOMAIN
    from .repairs import async_report_trunk_menu_retired

    pending = set(pending_trunk_menu_notices(entry))
    if not pending:
        return
    registry = ir.async_get(hass)
    for record in trunk_subentries(entry):
        if record.subentry_id in pending and registry.async_get_issue(
                DOMAIN, trunk_menu_notice_id(record.subentry_id)) is None:
            async_report_trunk_menu_retired(hass, entry, record)


async def async_complete_trunk_menu_notices(hass, entry) -> None:
    """Acknowledge only native issue records actually present in storage.

    The IssueRegistry's public creation event is emitted before its delayed
    save. Reading its in-memory Store would also include unsaved writes.
    Inspect only the committed file, read-only, using the native issue's own
    serialization for comparison. An unreadable or unfamiliar storage format
    conservatively leaves recovery pending; this never writes HA's registry.
    """
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers import issue_registry as ir
    from homeassistant.helpers.storage import STORAGE_DIR
    from homeassistant.util.json import load_json
    from .const import DOMAIN

    if PENDING_TRUNK_MENU_NOTICES not in entry.data:
        return
    pending = set(pending_trunk_menu_notices(entry))
    records = {row.subentry_id for row in trunk_subentries(entry)}
    completed = pending - records
    if pending & records:
        try:
            saved = await hass.async_add_executor_job(
                load_json, hass.config.path(STORAGE_DIR, ir.STORAGE_KEY))
        except (HomeAssistantError, OSError, ValueError, TypeError):
            return
        if (not isinstance(saved, dict)
                or saved.get("version") != ir.STORAGE_VERSION_MAJOR
                or saved.get("minor_version") != ir.STORAGE_VERSION_MINOR
                or saved.get("key") != ir.STORAGE_KEY
                or not isinstance(saved.get("data"), dict)
                or not isinstance(saved["data"].get("issues"), list)):
            return
        registry = ir.async_get(hass)
        persisted = {row.get("issue_id"): row for row in saved["data"]["issues"]
                     if isinstance(row, dict) and isinstance(row.get("issue_id"), str)
                     and row.get("domain") == DOMAIN and row.get("config_entry_id") == entry.entry_id}
        for subentry_id in pending & records:
            issue = registry.async_get_issue(DOMAIN, trunk_menu_notice_id(subentry_id))
            if (issue is not None and issue.is_persistent
                    and issue.config_entry_id == entry.entry_id
                    and issue.to_json() == persisted.get(issue.issue_id)):
                completed.add(subentry_id)
    # Configuration may have changed during file IO. Remove only notices
    # confirmed in this snapshot, retaining any newly appended journal entry.
    if hass.config_entries.async_get_entry(entry.entry_id) is not entry:
        return
    current = pending_trunk_menu_notices(entry)
    remaining = [identity for identity in current if identity not in completed]
    if remaining == current and current:
        return
    data = dict(entry.data)
    if remaining:
        data[PENDING_TRUNK_MENU_NOTICES] = remaining
    else:
        data.pop(PENDING_TRUNK_MENU_NOTICES, None)
    if data != entry.data:
        hass.config_entries.async_update_entry(entry, data=data)


def legacy_trunk_collects_digits(data) -> bool:
    """Read retired collector settings only while validating an upgrade."""
    mode = str(data.get("trunk_inbound_mode") or "").strip().lower()
    raw_timeout = data.get("trunk_dtmf_timeout_ms")
    if isinstance(raw_timeout, bool):
        raise ValueError("Invalid legacy trunk digit interval")
    try:
        timeout = 3000 if raw_timeout in (None, "") else int(raw_timeout)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("Invalid legacy trunk digit interval") from error
    if mode not in {"direct", "dtmf"}:
        mode = "dtmf" if data.get("trunk_dtmf_enabled", False) and timeout > 0 else "direct"
    return mode == "dtmf" and timeout > 0


def migrated_trunk_routing_data(data) -> dict:
    """Translate legacy reject to the existing empty-destination contract."""
    from .trunk_policy import inbound_trunk_destination

    result = {key: value for key, value in data.items() if key not in LEGACY_TRUNK_MENU_KEYS}
    if "trunk_inbound_unknown_target" in result:
        result["trunk_inbound_default_target"] = inbound_trunk_destination(result)
        result.pop("trunk_inbound_unknown_target")
    return result


def migrate_trunk_menus(hass, entry) -> None:
    """Retire collection and replay durable notices through their native owner."""
    from .repairs import async_report_trunk_menu_retired

    pending = pending_trunk_menu_notices(entry)
    migrate_legacy_trunk(hass, entry)
    records = trunk_subentries(entry)
    pending = list(dict.fromkeys([*pending, *(record.subentry_id for record in records
                               if legacy_trunk_collects_digits(record.data))]))
    if pending and entry.data.get(PENDING_TRUNK_MENU_NOTICES) != pending:
        # The config entry and issue registry are saved independently. Retain
        # enough ownership to restore every warning after an interrupted save.
        hass.config_entries.async_update_entry(entry,
            data={**entry.data, PENDING_TRUNK_MENU_NOTICES: pending})
    for record in records:
        data = dict(record.data)
        # Match the retired normalizer: an explicit DTMF mode determined the
        # effective flag, including persisted records with a stale false flag.
        if record.subentry_id in pending:
            # Native get-or-create preserves dismissed_version. Replaying the
            # same issue repairs a lost write without undoing the owner's Ignore.
            async_report_trunk_menu_retired(hass, entry, record)
        proposed = migrated_trunk_routing_data(data)
        if proposed != data:
            hass.config_entries.async_update_subentry(entry, record, data=proposed)
    data = migrated_trunk_routing_data(entry.data)
    if data != entry.data:
        hass.config_entries.async_update_entry(entry, data=data)
