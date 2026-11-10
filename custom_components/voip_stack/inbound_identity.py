"""Explicit provider-neutral called-party policy for an identified SIP trunk.

RFC 3261 separates Request-URI routing from the logical To recipient. RFC
7315 P-Called-Party-ID can preserve the address before retargeting, while
RFC 7044 History-Info describes a history, not an unambiguous DID selector.
Only the configured source and explicit ordered fallbacks are consulted.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Mapping

from .core import sip


DID_SOURCES = ("request_uri", "to", "p_called_party_id", "custom_header", "fixed")
INBOUND_IDENTITY_KEYS = (
    "called_number", "called_number_raw", "called_number_source",
    "caller_number", "caller_name", "inbound_trunk_id",
)


def _text(value, *, empty=True):
    if not isinstance(value, str):
        raise ValueError("DID values must be strings")
    value = value.strip()
    if (not empty and not value) or len(value) > 256 or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError("Invalid DID value")
    return value


def normalized_did_config(data: Mapping) -> dict:
    """Validate persisted and form values without numeric or country coercion."""
    source = _text(data.get("trunk_did_source") or "request_uri").lower()
    if source not in DID_SOURCES:
        raise ValueError("Invalid DID source")
    fallbacks = data.get("trunk_did_fallbacks") or []
    if not isinstance(fallbacks, (tuple, list)) or any(not isinstance(item, str) for item in fallbacks):
        raise ValueError("DID fallbacks must be an ordered list")
    fallbacks = [item.strip().lower() for item in fallbacks]
    if any(item not in DID_SOURCES for item in fallbacks) or len(set([source, *fallbacks])) != 1 + len(fallbacks):
        raise ValueError("Invalid or duplicate DID fallback")
    header = _text(data.get("trunk_did_header") or "")
    if header and not sip.is_sip_token(header):
        raise ValueError("Invalid DID header name")
    # Authentication and message framing are never identity sources.
    if header.casefold() in {"authorization", "proxy-authorization", "www-authenticate", "proxy-authenticate", "content-length", "l"}:
        raise ValueError("Invalid DID identity header")
    fixed = _text(data.get("trunk_did_fixed") or "")
    selected = {source, *fallbacks}
    if "custom_header" in selected and not header:
        raise ValueError("A custom DID header is required")
    if "fixed" in selected and not fixed:
        raise ValueError("A fixed DID is required")
    rules = data.get("trunk_did_prefix_rules") or []
    if not isinstance(rules, (tuple, list)) or len(rules) > 64:
        raise ValueError("DID prefix rules must be a bounded ordered list")
    normalized = []
    prefixes = set()
    for rule in rules:
        if not isinstance(rule, Mapping) or set(rule) != {"prefix", "replacement"}:
            raise ValueError("DID rules require prefix and replacement")
        prefix = _text(rule["prefix"], empty=False)
        replacement = _text(rule["replacement"])
        if prefix in prefixes:
            raise ValueError("Duplicate DID prefix rule")
        prefixes.add(prefix)
        normalized.append({"prefix": prefix, "replacement": replacement})
    return {"trunk_did_source": source, "trunk_did_header": header,
            "trunk_did_fixed": fixed, "trunk_did_fallbacks": fallbacks,
            "trunk_did_prefix_rules": normalized}


def _number_from_source(raw: str, *, plain=False) -> str:
    """Read exactly one SIP/SIPS/tel address, or an explicit plain header value."""
    try:
        value = sip.name_addr_uri(raw)
        scheme, separator, rest = value.partition(":")
        if separator and scheme.casefold() in {"sip", "sips"}:
            # Reuse the URI parser and its percent-decoding and '+' semantics.
            return sip.parse_sip_uri(value).user
        if separator and scheme.casefold() == "tel":
            number = rest.split(";", 1)[0]
            return _text(number) if number and all(ch in "+0123456789-().*#" for ch in number) else ""
        if plain and value and all(ch in "+0123456789*#" for ch in value):
            return value
    except (TypeError, ValueError, sip.SipError):
        pass
    return ""


def apply_inbound_identity(invite, config: Mapping, request: sip.SipMessage):
    """Extract configured identity only after the existing trust classification."""
    if not invite.received_via_trunk:
        return invite
    policy = normalized_did_config(config)
    number = source_name = ""
    for source in (policy["trunk_did_source"], *policy["trunk_did_fallbacks"]):
        source_name = source
        if source == "fixed":
            number = policy["trunk_did_fixed"]
        elif source == "request_uri":
            number = invite.request_uri.user
        else:
            header = {"to": "To", "p_called_party_id": "P-Called-Party-ID"}.get(source, policy["trunk_did_header"])
            values = request.header_values(header)
            number = _number_from_source(values[0], plain=source == "custom_header") if len(values) == 1 else ""
            if source == "custom_header":
                source_name = f"header:{header}"
        if number:
            break
    raw = number
    for rule in policy["trunk_did_prefix_rules"]:
        if number.startswith(rule["prefix"]):
            number = rule["replacement"] + number[len(rule["prefix"]):]
            break
    return replace(invite, called_number=number, called_number_raw=raw,
                   called_number_source=source_name if raw else "",
                   caller_number=invite.caller_uri.user if invite.caller_uri else invite.caller_route,
                   caller_name=sip.name_addr_display_name(request.header("From")),
                   inbound_trunk_id=invite.trunk_id)


def identity_fields(invite) -> dict[str, str]:
    """Copy the small immutable ingress snapshot, never request headers."""
    return {key: str(getattr(invite, key, "") or "") for key in INBOUND_IDENTITY_KEYS}
