"""Canonical phonebook composition shared by routing and public consumers."""

from dataclasses import replace

from .phone_directory import PhoneDirectory, public_metadata
from .phone_endpoint import EndpointKind, EndpointAvailability, PhoneEndpoint
from .roster import RosterEntry
from .runtime_data import require_runtime_data, runtime_data


def restore_esp_identities(hass, runtime):
    """Rebuild physical identities from their existing HA owners, even offline."""
    from homeassistant.helpers import device_registry as dr, entity_registry as er
    from .device_resolver import entity_role
    from .managed_phones import managed_records
    from .phone_config import esphome_phone_policy
    from .store import config_entry

    entry = config_entry(hass)
    records = managed_records(entry) if entry is not None else {}
    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    owned_devices = (device for origin in hass.config_entries.async_entries("esphome")
                     for device in dr.async_entries_for_config_entry(devices, origin.entry_id))
    for device in owned_devices:
        origin = hass.config_entries.async_get_entry(device.config_entry_id)
        if origin is None or origin.domain != "esphome":
            continue
        provisioner = runtime.phone_provisioning
        if provisioner is not None and provisioner.is_removed(origin.entry_id, device.id):
            continue
        if runtime.endpoints.by_device_id(device.id) is not None:
            continue
        phone = records.get(device.id)
        native_entities = [entity for entity in er.async_entries_for_device(entities, device.id)
                           if entity.platform == "esphome"]
        roles = {entity_role(entity): entity for entity in native_entities}
        if "voip_state" not in roles:
            continue
        fields = {}
        for role, field in (("voip_extension", "extension"), ("voip_ring_groups", "ring_group"),
                            ("voip_conference_groups", "conference_group"), ("voip_conference_ring", "conference_ring")):
            state = hass.states.get(roles[role].entity_id) if role in roles else None
            fallback = getattr(phone, field, False if field == "conference_ring" else "")
            value = state.state if state is not None and state.state not in {"unknown", "unavailable"} else fallback
            fields[field] = str(value).casefold() in {"on", "true", "1"} if field == "conference_ring" else str(value)
        endpoint_id = phone.endpoint_id if phone is not None else f"esphome:{device.id}"
        runtime.endpoints.register(PhoneEndpoint(endpoint_id=endpoint_id, device_id=device.id,
            name=(device.name_by_user or (phone.display_name if phone is not None else "")
                  or device.name or origin.title or (phone.username if phone is not None else "ESPHome")),
            username=phone.username if phone is not None else str(origin.data.get("device_name") or ""),
            kind=EndpointKind.ESPHOME, entity_ids=frozenset(entity.entity_id for entity in native_entities),
            availability=EndpointAvailability.OFFLINE, capabilities=frozenset({"audio", "dtmf"}),
            policy=esphome_phone_policy(entry, endpoint_id), **fields))


def _endpoint_entry(endpoint):
    browser = endpoint.kind.is_softphone
    return RosterEntry(id=endpoint.username or endpoint.sip_uri_user, name=endpoint.name,
        extension=endpoint.extension, ha_bridge=True,
        enabled=endpoint.availability is not EndpointAvailability.UNAVAILABLE,
        metadata={"endpoint_id": endpoint.endpoint_id, "endpoint_kind": endpoint.kind.value,
                  "device_id": endpoint.device_id, "account_id": "ha", "local_ha": browser,
                  "capabilities": sorted(endpoint.capabilities), "registered": False,
                  "availability": endpoint.availability.value,
                  "ring_group": endpoint.ring_group, "conference_group": endpoint.conference_group,
                  "conference_ring": endpoint.conference_ring, "call_policy": endpoint.policy.as_data()})


def compose_directory_entries(hass, peers, registered):
    """Use current wire bindings and persistent identities in one composition."""
    from .endpoint_routing import assemble_directory_entries
    from .groups import collect_groups

    runtime = require_runtime_data(hass)
    normalized = []
    for peer in peers:
        endpoint = runtime.endpoints.get(peer.endpoint_id) if peer.endpoint_id else None
        if endpoint is None and peer.device_id:
            endpoint = runtime.endpoints.by_device_id(peer.device_id)
        if isinstance(endpoint, PhoneEndpoint):
            peer = replace(peer, endpoint_id=endpoint.endpoint_id, endpoint_kind=endpoint.kind.value)
        normalized.append(peer)
    candidates = assemble_directory_entries(hass, normalized, registered)
    entries = {}
    for row in candidates:
        if row.metadata.get("auto") and row.metadata.get("group_type"):
            continue
        identity = str(row.metadata.get("endpoint_id") or row.id).casefold()
        entries[identity] = row
    for endpoint in runtime.endpoints.endpoints:
        if isinstance(endpoint, PhoneEndpoint) and endpoint.endpoint_id.casefold() not in entries:
            entries[endpoint.endpoint_id.casefold()] = _endpoint_entry(endpoint)
    if runtime.queues is not None:
        runtime.queues.refresh()
        for row in runtime.queues.roster_entries():
            entries[row.metadata["endpoint_id"]] = row
    from .paging_config import paging_group_entries

    from .store import config_entry
    entry = config_entry(hass)
    if entry is not None:
        for row in paging_group_entries(entry):
            entries[row.metadata["endpoint_id"]] = row
        from .group_config import group_roster_entries
        for row in group_roster_entries(hass, entry):
            entries[row.metadata["endpoint_id"]] = row
    for group in collect_groups([], entries.values(), [], existing_entries=entries.values()).values():
        entries[f"group:{group.group_type}:{group.name}".casefold()] = RosterEntry(
            id=group.name, name=group.name, ha_bridge=True, enabled=bool(group.members),
            metadata={"endpoint_kind": "group", "group_type": group.group_type, "members": group.members,
                      "ring_members": group.ring_members, "auto": group.auto})
    result = []
    for row in entries.values():
        metadata = public_metadata(row.metadata)
        endpoint = runtime.endpoints.get(metadata.get("endpoint_id", ""))
        if metadata.get("group_type") or metadata.get("virtual_endpoint") in {"ring_group", "conference"}:
            kind = "group"
            identity = metadata.get("endpoint_id") or f"group:{metadata.get('group_type') or 'explicit'}:{row.id}"
        elif metadata.get("virtual_endpoint"):
            kind = "service"
            service = str(metadata["virtual_endpoint"])
            identity = metadata.get("endpoint_id") or (
                "service:assist_pipeline" if service == "assist_pipeline" else f"service:{service}:{row.id}")
        elif isinstance(endpoint, PhoneEndpoint):
            from .account_runtime import account_has_ha_binding

            kind, identity = endpoint.kind.value, endpoint.endpoint_id
            metadata.update(account_id="ha", availability=endpoint.availability.value)
            if endpoint.supports_ha_client:
                bound = endpoint.kind.is_softphone or account_has_ha_binding(hass, identity)
                # A bare account routes to the deferred HA offer; one with
                # Contacts is composed by the common fork instead.
                metadata.update(ha_client_supported=True, ha_binding=bound,
                                local_ha=bound and not bool(metadata.get("sip_contacts")))
        else:
            # Explicit static phone metadata may declare its account context.
            # Unknown contacts never become phones because of a URI or label.
            kind = str(metadata.get("endpoint_kind") or "contact")
            if kind not in {"esphome", "browser", "sip_account"} or not (
                    metadata.get("endpoint_id") and metadata.get("account_id")):
                kind = "contact"
            identity = metadata.get("endpoint_id") or f"contact:{row.id}"
        metadata.update(endpoint_id=str(identity), endpoint_kind=kind)
        result.append(replace(row, metadata=metadata))
    return result


async def async_refresh_directory(hass, *, peers=None, registered=None):
    """Publish only while the runtime and latest refresh still own the result."""
    from .peer_snapshot import async_build_peer_snapshot, async_advertise_host
    from .phonebook_runtime import registered_roster_entries

    runtime = require_runtime_data(hass)
    if runtime.directory is None:
        runtime.directory = PhoneDirectory()
    directory = runtime.directory
    if directory.closed:
        return None
    generation = directory.begin_refresh()
    restore_esp_identities(hass, runtime)
    if peers is None:
        peers = await async_build_peer_snapshot(hass)
    if runtime_data(hass) is not runtime or runtime.directory is not directory or directory.closed:
        return None
    if registered is None:
        registered = registered_roster_entries(hass)
    entries = compose_directory_entries(hass, peers, registered)
    host = str(runtime.transport_config.get("advertise_host") or "")
    if not host:
        host = next((peer.host for peer in peers if peer.is_ha and peer.host), "") or await async_advertise_host(hass)
    if (runtime_data(hass) is not runtime or runtime.directory is not directory
            or not directory.owns_refresh(generation)):
        return None
    directory.advertise_host = host
    directory.replace(entries, refresh_generation=generation)
    return directory.snapshot()


def directory_uri(hass, row):
    """Return a canonical PBX address while retaining the row's call policy."""
    from .core.sip import SipUri

    runtime = require_runtime_data(hass)
    host = runtime.transport_config.get("advertise_host") or getattr(runtime.directory, "advertise_host", "")
    return str(SipUri(row.id, host, int(runtime.transport_config["sip_port"]))) if host else row.sip_uri
