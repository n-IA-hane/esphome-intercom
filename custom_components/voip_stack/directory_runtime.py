"""Canonical phonebook composition shared by routing and public consumers."""

from dataclasses import replace

from .phone_directory import PhoneDirectory, public_metadata, service_directory_reference
from .phone_endpoint import EndpointAvailability, PhoneEndpoint
from .roster import RosterEntry
from .runtime_data import require_runtime_data, runtime_data


def routing_snapshot(runtime, *, entries=None):
    """Join configured identities with current binding owners without I/O."""
    directory = getattr(runtime, "directory", None)
    if directory is None or directory.closed:
        raise ValueError("Phone directory is unavailable")
    entries = list(directory.snapshot() if entries is None else entries)
    video_disabled = {row.metadata.get('endpoint_id') for row in entries
                      if row.metadata.get('configured_video_enabled') is False}
    sip_runtime = getattr(runtime, "sip", None)
    registrar = sip_runtime.component("registrar") if sip_runtime is not None else None
    if registrar is None:
        return entries
    from .endpoint_routing import merge_registered_entries

    registered = registrar.registered_roster_entries()
    configured = []
    for row in entries:
        endpoint = runtime.endpoints.get(row.metadata.get("endpoint_id", ""))
        username = endpoint.username if endpoint is not None else row.id
        if username.casefold() in registrar.accounts and (
                row.metadata.get("registered") or row.metadata.get("sip_contacts")):
            # An old derived view cannot retain a departed Contact or lend its
            # learned capabilities to another registration of the same account.
            if endpoint is None:
                continue
            row = _endpoint_entry(endpoint)
        configured.append(row)
    entries = merge_registered_entries(configured, registered, runtime.endpoints)
    result = []
    for row in entries:
        endpoint = runtime.endpoints.get(row.metadata.get("endpoint_id", ""))
        if endpoint is not None and endpoint.endpoint_id in video_disabled:
            row = replace(row, metadata={**row.metadata, 'configured_video_enabled': False, 'sip_video_codec': ''})
        if endpoint is not None and endpoint.supports_ha_client:
            bindings = runtime.ha_bindings
            bound = endpoint.kind.is_softphone or bool(bindings and bindings.has_binding(endpoint.endpoint_id))
            row = replace(row, metadata={**row.metadata, "ha_client_supported": True, "ha_binding": bound,
                                        "local_ha": bound and not bool(row.metadata.get("sip_contacts"))})
        result.append(row)
    return result


def _endpoint_entry(endpoint):
    browser = endpoint.kind.is_softphone
    return RosterEntry(id=endpoint.username or endpoint.sip_uri_user, name=endpoint.name,
        extension=endpoint.extension, ha_bridge=True,
        enabled=endpoint.availability is not EndpointAvailability.UNAVAILABLE,
        metadata={"endpoint_id": endpoint.endpoint_id, "endpoint_kind": endpoint.kind.value,
                  "username": endpoint.username, "sip_uri_user": endpoint.sip_uri_user,
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
            identity = service_directory_reference(row)
        elif isinstance(endpoint, PhoneEndpoint):
            from .account_runtime import account_has_ha_binding
            from .phone_config import phone_subentry_by_endpoint_id

            kind, identity = endpoint.kind.value, endpoint.endpoint_id
            # Publish the literal configured address. Consumers must not infer
            # a SIP login from a display label or an ASCII-only identifier.
            metadata.update(account_id="ha", availability=endpoint.availability.value,
                            username=endpoint.username, sip_uri_user=endpoint.sip_uri_user)
            account = phone_subentry_by_endpoint_id(entry, identity) if entry is not None else None
            if account is not None and account.data.get('video_enabled') is False:
                metadata.update(configured_video_enabled=False, sip_video_codec='')
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


async def async_refresh_directory(hass, *, peers=None, registered=None, prepare_peers=None):
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
    if peers is None:
        peers = await async_build_peer_snapshot(hass)
    if (runtime_data(hass) is not runtime or runtime.directory is not directory
            or not directory.owns_refresh(generation)):
        return None
    if prepare_peers is not None:
        peers = prepare_peers(peers)
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


def directory_user(directory, row):
    """Return the public SIP identity exposed to directory clients."""
    metadata = row.metadata
    if (metadata.get("endpoint_kind") in {"sip_account", "esphome", "browser", "ha_softphone"}
            and not metadata.get("virtual_endpoint") and not metadata.get("group_type")):
        return row.id
    return next(directory.dial_users(row), "")


def directory_uri(hass, row):
    """Return a canonical PBX address while retaining the row's call policy."""
    from .core.sip import SipUri
    from .phone_directory import contact_key_reference

    runtime = require_runtime_data(hass)
    host = runtime.transport_config.get("advertise_host") or getattr(runtime.directory, "advertise_host", "")
    if not host:
        return ""
    if contact_key_reference(row) or row.metadata.get("virtual_endpoint") or row.metadata.get("group_type"):
        user = directory_user(runtime.directory, row)
    else:
        user = row.id
    return str(SipUri(user, host, int(runtime.transport_config["sip_port"]))) if user else ""
