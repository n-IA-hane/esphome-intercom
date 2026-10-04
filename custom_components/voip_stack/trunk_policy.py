"""Trunk availability, inbound source trust and per-phone outbound access."""

from __future__ import annotations

import ipaddress


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
    blocked = frozenset(config.get(EXTERNAL_CALL_BLOCKED_ENDPOINTS, ()))
    if not blocked:
        return ""
    metadata = getattr(session, "metadata", {}) or {}
    candidates = (*endpoint_ids, *(metadata.get(key, "") for key in (
        "source_endpoint_id", "dest_endpoint_id", "endpoint_id",
    )))
    return next((str(value) for value in candidates if value and value in blocked), "")


def call_external_call_denied(runtime, call_id: str, *endpoint_ids) -> str:
    """Preserve the restriction through existing call/group ownership links."""
    config = getattr(runtime, "transport_config", {}) or {}
    if not config.get(EXTERNAL_CALL_BLOCKED_ENDPOINTS):
        return ""
    if denied := external_call_denied(config, *endpoint_ids):
        return denied
    calls = getattr(runtime, "sip", None)
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
