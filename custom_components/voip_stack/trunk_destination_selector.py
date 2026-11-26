"""Project native VoIP Stack destinations into the existing trunk route field."""

from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import DOMAIN
from .endpoint_device import ENDPOINT_DEVICE_PREFIX
from .runtime_data import runtime_data


def trunk_destination_projection(hass, entry, names, selected):
    """Return editor options and ephemeral registry references, never new routes."""
    options = {str(name): str(name) for name in names}
    references = {}
    if entry is None:
        return [{"value": value, "label": label} for value, label in sorted(options.items())], references
    runtime = runtime_data(hass)
    endpoints = runtime.endpoints if runtime is not None else None
    from .phone_config import phone_endpoint_id, phone_subentries
    from .group_config import group_records
    stored = {phone_endpoint_id(row): row.data for row in phone_subentries(entry)}
    groups = group_records(entry)
    devices = dr.async_get(hass)
    device_targets = {}
    for device in dr.async_entries_for_config_entry(devices, entry.entry_id):
        if device.disabled_by:
            continue
        target = ""
        for domain, identity in device.identifiers:
            if domain != DOMAIN:
                continue
            if identity.startswith(ENDPOINT_DEVICE_PREFIX):
                endpoint_id = identity.removeprefix(ENDPOINT_DEVICE_PREFIX)
                endpoint = endpoints.get(endpoint_id) if endpoints is not None else None
                raw = stored.get(endpoint_id, {})
                target = (endpoint.username or endpoint.extension or endpoint.name) if endpoint is not None else str(
                    raw.get("username") or raw.get("extension") or raw.get("name") or "")
            elif identity.startswith("group:"):
                raw = groups.get(identity.removeprefix("group:"), {})
                target = str(raw.get("name") or "")
        if not target:
            from .automation_contact_device import automation_contact_id_for_device
            target = automation_contact_id_for_device(hass, device.id, entry_id=entry.entry_id) or ""
        if not target:
            continue
        device_targets[device.id] = target
        reference = "device:" + device.id
        references[reference] = target
        label = device.name_by_user or device.name or target
        options[reference] = f"{label} · Device · {target}"
    entities = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(entities, entry.entry_id):
        if entity.platform != DOMAIN or entity.disabled_by or not entity.device_id:
            continue
        target = device_targets.get(entity.device_id)
        if target:
            reference = "entity:" + entity.entity_id
            references[reference] = target
            label = entity.name or entity.original_name or entity.entity_id
            options[reference] = f"{label} · {entity.entity_id} · {target}"
    if selected:
        options.setdefault(selected, selected)
        if endpoints is not None:
            matches = [endpoint for endpoint in endpoints.endpoints
                       if selected == (endpoint.username or endpoint.extension or endpoint.name)]
            if len(matches) == 1:
                options[selected] = matches[0].name
    return [{"value": value, "label": label} for value, label in sorted(
        options.items(), key=lambda item: (item[1].casefold(), item[0]))], references


def trunk_destination_value(value, references):
    """Resolve only this editor's live registry projection; manual routes pass."""
    value = str(value or "").strip()
    if value.startswith(("device:", "entity:")):
        if value not in references:
            raise ValueError("Select a current VoIP Stack destination")
        return references[value]
    return value
