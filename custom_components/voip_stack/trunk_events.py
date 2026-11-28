"""Resolve configured trunk display names for Home Assistant call events."""

from .store import config_entry
from .trunk_config import trunk_identity, trunk_subentries


def trunk_display_name(hass, identity: str) -> str:
    """Return the current subentry title for one stable trunk identity."""
    entry = config_entry(hass)
    if entry is None or not identity:
        return ""
    for record in trunk_subentries(entry):
        if trunk_identity(record) == identity:
            return str(record.title or "")
    return ""


def add_trunk_names(hass, payload: dict) -> None:
    """Attach current display names without changing stable trunk identities."""
    inbound = str(payload.get("inbound_trunk_id") or "").strip()
    if inbound:
        name = trunk_display_name(hass, inbound)
        if name:
            payload["inbound_trunk_name"] = name
    outbound = str(payload.get("outbound_trunk_id") or "").strip()
    if outbound:
        name = trunk_display_name(hass, outbound)
        if name:
            payload["outbound_trunk_name"] = name
