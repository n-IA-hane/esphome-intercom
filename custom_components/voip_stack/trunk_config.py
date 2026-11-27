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


def migrate_legacy_trunk(hass, entry) -> None:
    """Move the former parent-entry trunk into a stable native record once."""
    from uuid import uuid4
    from homeassistant.config_entries import ConfigSubentry
    from .sip_registration_policy import TRUNK_OPTIONS_INTERVAL

    if "trunk_enabled" not in entry.data:
        return
    configs = entry_trunk_configs(entry)
    selected = entry_default_trunk(entry, configs)
    current = next((item for item in trunk_subentries(entry)
                    if str(item.data.get("trunk_id") or item.subentry_id) == selected), None)
    # The old 6.2 ownership move runs before the current policy preflight.
    # Preserve a saved probe value verbatim so that preflight can report an
    # invalid choice through Repairs instead of aborting this earlier step.
    config = normalized_trunk_config({key: value for key, value in entry.data.items()
                                      if key != TRUNK_OPTIONS_INTERVAL})
    if TRUNK_OPTIONS_INTERVAL in entry.data:
        config[TRUNK_OPTIONS_INTERVAL] = entry.data[TRUNK_OPTIONS_INTERVAL]
    identity = selected if current is not None else uuid4().hex
    title = current.title if current is not None else "SIP trunk"
    data = {**config, **{key: value for key, value in entry.data.items()
                       if key in LEGACY_TRUNK_MENU_KEYS}, "trunk_id": identity, "name": title}
    # Disabled is saved configuration, not permission to discard the line.
    if current is None:
        hass.config_entries.async_add_subentry(entry, ConfigSubentry(
            subentry_type=TRUNK_SUBENTRY_TYPE, title=title, data=data, unique_id=f"trunk:{identity}",
        ))
    else:
        hass.config_entries.async_update_subentry(entry, current, data=data)
    parent = {key: value for key, value in entry.data.items() if not key.startswith("trunk_")}
    if config["trunk_enabled"] or current is not None or CONF_DEFAULT_TRUNK not in entry.data:
        parent[CONF_DEFAULT_TRUNK] = identity
    hass.config_entries.async_update_entry(entry, data=parent)


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
    """Validate the retry journal without exposing arbitrary stored values."""
    pending = entry.data.get(PENDING_TRUNK_MENU_NOTICES, [])
    if not isinstance(pending, list) or any(not isinstance(item, str) or not item for item in pending):
        raise ValueError("Invalid pending trunk menu notice")
    return list(dict.fromkeys(pending))


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
    """Retire automatic collection without rewriting destinations or identities."""
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
            async_report_trunk_menu_retired(hass, entry, record)
        proposed = migrated_trunk_routing_data(data)
        if proposed != data:
            hass.config_entries.async_update_subentry(entry, record, data=proposed)
    data = migrated_trunk_routing_data(entry.data)
    if data != entry.data:
        hass.config_entries.async_update_entry(entry, data=data)
