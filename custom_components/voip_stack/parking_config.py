"""Parking configuration and feature addresses, independent of call state."""

import math
import re

DEFAULT_SLOTS = tuple(str(number) for number in range(701, 721))
_ADDRESS = re.compile(r"[A-Za-z0-9*#_-]{1,64}")


def parking_settings(hass):
    from .store import config_entry

    entry = config_entry(hass)
    if entry is None:
        return {"enabled": False, "unavailable_reason": "not_ready"}
    try:
        settings = normalize_parking_config(entry.data.get("parking", {}))
    except (TypeError, ValueError):
        return {"enabled": False, "unavailable_reason": "invalid_configuration"}
    from .config_validation import entry_route_mappings
    mappings = entry_route_mappings(entry, entry.data, include_features=False)
    conflict = parking_namespace_conflicts(settings, mappings)
    return {**settings, "enabled": not conflict,
            "unavailable_reason": "route_conflict" if conflict else ""}


def parking_addresses(value):
    """Read route identities without depending on a residence-time policy."""
    if not isinstance(value, dict):
        raise ValueError("Invalid parking configuration")
    access = str(value.get("access") or "700").strip()
    slots = tuple(str(item).strip() for item in value.get("slots", DEFAULT_SLOTS))
    if (not _ADDRESS.fullmatch(access) or not slots or len(slots) > 100
            or any(not _ADDRESS.fullmatch(slot) for slot in slots)
            or len({slot.casefold() for slot in slots}) != len(slots)
            or {access.casefold(), "park"} & {slot.casefold() for slot in slots}
            or any(value.casefold().startswith("*8") for value in (access, *slots))):
        raise ValueError("Choose a parking access code and up to 100 distinct slots")
    return {"access": access, "slots": list(slots)}


def normalize_parking_config(value):
    result = parking_addresses(value)
    for name in ("timeout", "return_timeout"):
        number = value.get(name)
        if name == "return_timeout" and number is None:
            result[name] = None
            continue
        minimum = 0 if name == "return_timeout" else 1
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or not minimum <= number <= 3600:
            raise ValueError("Parking residence is 1..3600 seconds; return ringing is 0..3600 or inherited")
        result[name] = number
    for name in ("return_destination", "hold_media"):
        item = value.get(name, "")
        if not isinstance(item, str) or len(item) > 2048:
            raise ValueError("Invalid parking destination or hold media")
        result[name] = item.strip()
    return result


def parse_parking_address(settings, target):
    if settings.get("enabled") is False:
        return None
    raw = str(target or "").strip().casefold()
    if "@" in raw or raw.startswith(("sip:", "sips:")):
        return None
    slots = {str(slot).casefold(): str(slot) for slot in settings.get("slots", DEFAULT_SLOTS)}
    if raw in {str(settings.get("access") or "700").casefold(), "park"}:
        return "deposit", ""
    for prefix, mode in (("park.", "deposit"), ("pickup.", "pickup")):
        if raw.startswith(prefix):
            slot = slots.get(raw[len(prefix):])
            if slot:
                return mode, slot
            return ("directed_pickup", str(target).strip()[len(prefix):]) if mode == "pickup" else None
    if raw.startswith("*8"):
        return "directed_pickup", str(target).strip()[2:]
    return ("pickup", slots[raw]) if raw in slots else None


def parking_namespace_conflicts(settings, mappings):
    from .config_validation import ROUTE_ALIAS_FIELDS, mapping_group_values
    from .mailbox_address import parse_mailbox_address

    if any(parse_mailbox_address(value) is not None for value in (settings["access"], *settings["slots"])):
        return True
    return any(parse_parking_address(settings, value) is not None
               for item in mappings
               for value in (*(item.get(field) for field in ROUTE_ALIAS_FIELDS), *mapping_group_values(item)))
