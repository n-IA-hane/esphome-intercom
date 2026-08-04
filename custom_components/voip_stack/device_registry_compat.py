"""Small compatibility helpers for Home Assistant Device Registry changes."""

from __future__ import annotations


def device_config_entry_ids(device: object) -> tuple[str, ...]:
    """Return owning config entries across the HA 2026.7 and 2026.8 models."""

    entry_id = str(getattr(device, "config_entry_id", "") or "").strip()
    if entry_id:
        return (entry_id,)
    return tuple(
        str(item)
        for item in (getattr(device, "config_entries", ()) or ())
        if str(item)
    )
