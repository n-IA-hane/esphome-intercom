"""Registration policy and explicit inbound source trust for SIP trunks."""

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
