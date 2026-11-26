"""Trunk availability, inbound source trust and per-phone outbound access."""

from __future__ import annotations

import ipaddress


def authenticated_inbound_trunk(configs, username: str) -> str:
    """Resolve an existing account's explicit peer binding after Digest succeeds."""
    principal = str(username or "").casefold()
    if not principal:
        return ""
    matches = [(identity, config) for identity, config in configs.items()
               if str(config.get("trunk_inbound_account") or "").strip().casefold() == principal]
    if not matches:
        return ""
    enabled = [identity for identity, config in matches if config.get("trunk_enabled", False)]
    if len(enabled) != 1:
        raise ValueError("The authenticated SIP account needs one enabled incoming trunk")
    return enabled[0]


def select_trunk_id(configs, default_id: str, *, requested_id: str = "", phone_trunk_id: str = "") -> str:
    """Resolve an explicit choice, phone preference or default without failover."""
    selected = str(requested_id or phone_trunk_id or default_id or "").strip()
    if not selected:
        if configs:
            raise ValueError("Select a default trunk or choose a trunk for this call")
        return ""
    if selected not in configs:
        raise ValueError("The selected trunk no longer exists")
    if not configs[selected].get("trunk_enabled", False):
        raise ValueError("The selected trunk is disabled")
    return selected


def validate_static_trunk_sources(configs) -> None:
    """Reject overlapping source rules on the same signaling transport."""
    seen = []
    for identity, config in configs.items():
        if (not config.get("trunk_enabled", False) or config.get("trunk_register", True)
                or config.get("trunk_inbound_account")):
            continue
        transport = str(config.get("trunk_transport") or "udp").casefold()
        for network in trusted_networks(config.get("trunk_allowed_ips")):
            for previous_id, previous_transport, previous in seen:
                if (previous_id != identity and previous_transport == transport
                        and previous.version == network.version and previous.overlaps(network)):
                    raise ValueError("Static trunks on the same transport need non-overlapping incoming networks")
            seen.append((identity, transport, network))


def identify_inbound_trunk(clients, host: str, port: int, transport: str, *, flow_trunk_id: str = "", contact_line: str = "") -> str:
    """Bind an owned flow, or require one unambiguous trusted source match."""
    if flow_trunk_id:
        client = clients.get(flow_trunk_id)
        return flow_trunk_id if client is not None and trunk_inbound_source_available(client) else ""
    matches = [identity for identity, client in clients.items()
               if trunk_inbound_source_available(client) and client.accepts_inbound_source(host, port, transport)]
    if contact_line and matches:
        matches = [identity for identity in matches if getattr(clients[identity], "contact_line", "") == contact_line]
        if not matches:
            raise ValueError("Incoming SIP Contact identifies an inactive trunk registration")
    if len(matches) > 1:
        raise ValueError("Incoming SIP source matches multiple trunks")
    return matches[0] if matches else ""


def trunk_inbound_source_available(trunk) -> bool:
    """Keep source authentication separate from the outbound reachability gate."""
    return bool(getattr(trunk, "inbound_source_ready", trunk_available(trunk)))


def trusted_networks(values):
    """Validate literal IPs/CIDRs; never infer inbound trust from SIP headers."""
    if isinstance(values, str):
        values = [values]
    networks = tuple(ipaddress.ip_network(str(value).strip(), strict=False) for value in (values or ()))
    if not networks or any(network.prefixlen == 0 for network in networks):
        raise ValueError("Specify at least one IP address or restricted network")
    return networks


def trunk_available(trunk) -> bool:
    """Keep registration status distinct from static trunk availability."""
    return bool(getattr(trunk, "ready", getattr(trunk, "registered", False)))


EXTERNAL_CALL_BLOCKED_ENDPOINTS = "external_call_blocked_endpoints"


def external_call_denied(config, *endpoint_ids, session=None) -> str:
    """Return a restricted stable phone identity, never match caller display text."""
    metadata = getattr(session, "metadata", {}) or {}
    if caller := metadata.get("registration_only_caller"):
        return str(caller)
    blocked = frozenset(config.get(EXTERNAL_CALL_BLOCKED_ENDPOINTS, ()))
    if not blocked:
        return ""
    candidates = (*endpoint_ids, *(metadata.get("policy_endpoint_ids") or ()), *(metadata.get(key, "") for key in (
        "source_endpoint_id", "dest_endpoint_id", "endpoint_id",
    )))
    return next((str(value) for value in candidates if value and value in blocked), "")


def call_external_call_denied(runtime, call_id: str, *endpoint_ids) -> str:
    """Preserve the restriction through existing call/group ownership links."""
    config = getattr(runtime, "transport_config", {}) or {}
    return registry_external_call_denied(config, getattr(runtime, "sip", None), call_id, *endpoint_ids)


def registry_external_call_denied(config, calls, call_id, *endpoint_ids):
    """Follow the existing source call to keep its original outbound policy."""
    if denied := external_call_denied(config, *endpoint_ids):
        return denied
    seen = set()
    while calls is not None and call_id and call_id not in seen:
        seen.add(call_id)
        session = calls.get_session(call_id)
        if session is None:
            break
        if denied := external_call_denied(config, session=session):
            return denied
        call_id = str((session.metadata or {}).get("source_call_id") or "")
    return ""


def is_any_trunk_uri(runtime, uri) -> bool:
    """Access restrictions cover every configured provider, including non-default lines."""
    configs = getattr(runtime, "trunk_configs", {}) or {"": runtime.trunk_config}
    calls = getattr(runtime, "sip", None)
    for identity, config in configs.items():
        client = calls.component(f"trunk:{identity}" if identity else "trunk") if calls is not None else None
        if is_trunk_uri(uri, config, getattr(client, "active_registrar_target", None)):
            return True
    return False


def is_trunk_uri(uri, trunk_config, active_target=None) -> bool:
    """Recognize configured trunk routes without DNS work or trusting SIP From."""
    if not uri or not trunk_config.get("trunk_server"):
        return False
    from .core.sip import parse_sip_uri

    def normalize_host(host):
        host = str(host).strip("[]").casefold().rstrip(".")
        try:
            return str(ipaddress.ip_address(host))
        except ValueError:
            return host

    def address(value, default_port=None):
        raw = str(value or "").strip()
        if not raw:
            return None
        if not raw.lower().startswith(("sip:", "sips:")):
            raw = "sip:" + raw
        try:
            parsed = parse_sip_uri(raw)
        except (ValueError, TypeError):
            return None
        port = parsed.port or default_port or (5061 if raw.lower().startswith("sips:") else 5060)
        return normalize_host(parsed.host), int(port)

    requested = address(uri)
    targets = {
        address(trunk_config.get("trunk_server"), trunk_config.get("trunk_port")),
        address(trunk_config.get("trunk_outbound_proxy")),
    }
    if active_target:
        targets.add((normalize_host(active_target[0]), int(active_target[1])))
    return requested is not None and requested in targets
