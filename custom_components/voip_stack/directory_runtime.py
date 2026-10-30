"""Assemble the single LDAP directory consumed by HA and physical phones."""

from __future__ import annotations

from dataclasses import replace

from .phone_directory import PhoneDirectory
from .phone_endpoint import EndpointKind
from .roster import RosterEntry
from .runtime_data import require_runtime_data


async def async_refresh_directory(hass, *, peers=None, registered=None) -> tuple[RosterEntry, ...]:
    """Keep persistent destinations even when their current binding is offline."""
    from .endpoint_routing import assemble_directory_entries
    from .peer_snapshot import async_build_peer_snapshot
    from .phonebook_runtime import registered_roster_entries

    runtime = require_runtime_data(hass)
    if runtime.directory is None:
        runtime.directory = PhoneDirectory()
    if peers is None:
        peers = await async_build_peer_snapshot(hass)
    if registered is None:
        registered = registered_roster_entries(hass)
    normalized = []
    for peer in peers:
        # Discovery can precede endpoint-id migration. Resolve physical peer
        # identities before assembly; manual contacts and virtual destinations
        # never enter this normalization path.
        endpoint = runtime.endpoints.get(peer.endpoint_id) if peer.endpoint_id else None
        if endpoint is None and peer.device_id:
            endpoint = runtime.endpoints.by_device_id(peer.device_id)
        if endpoint is None and not peer.is_ha and peer.endpoint_kind in {"", "esphome", "sip_account"}:
            endpoint = runtime.endpoints.by_username(peer.sip_uri_user or peer.name)
        if endpoint is not None:
            peer = replace(peer, endpoint_id=endpoint.endpoint_id, endpoint_kind=endpoint.kind.value)
        normalized.append(peer)
    candidates = assemble_directory_entries(hass, normalized, registered)
    entries = {}
    for entry in candidates:
        identity = str(entry.metadata.get("endpoint_id") or entry.id)
        previous = entries.get(identity)
        if previous is not None:
            metadata = {**previous.metadata, **entry.metadata}
            entry = replace(entry, metadata=metadata)
        entries[identity] = entry
    for endpoint in runtime.endpoints.endpoints:
        if endpoint.endpoint_id in entries:
            continue
        # Configured phones are directory identities, not a snapshot of live
        # transports. Reachability remains a separate SIP/BLF concern.
        entries[endpoint.endpoint_id] = RosterEntry(
            id=endpoint.username or endpoint.endpoint_id,
            name=endpoint.name, extension=endpoint.extension,
            metadata={"endpoint_id": endpoint.endpoint_id,
                      "endpoint_kind": endpoint.kind.value,
                      "device_id": endpoint.device_id,
                      "local_ha": endpoint.kind is EndpointKind.BROWSER,
                      "capabilities": sorted(endpoint.capabilities)},
        )
    runtime.directory.replace(entries.values())
    return runtime.directory.snapshot()


def directory_uri(hass, entry: RosterEntry) -> str:
    """LDAP callers dial the PBX's canonical name, retaining its call policy."""
    from .config import transport_config
    from .core.sip import SipUri

    cfg = transport_config(hass)
    host = str(cfg.get("advertise_host") or "")
    if not host:
        provisioning = require_runtime_data(hass).phone_provisioning
        host = getattr(provisioning, "advertise_host", "")
    if not host:
        return entry.sip_uri
    endpoint_id = str(entry.metadata.get("endpoint_id") or "")
    endpoint = require_runtime_data(hass).endpoints.get(endpoint_id) if endpoint_id else None
    target = endpoint.username if endpoint is not None and endpoint.username else entry.id
    return str(SipUri(scheme="sip", user=target, host=host, port=int(cfg["sip_port"])))
