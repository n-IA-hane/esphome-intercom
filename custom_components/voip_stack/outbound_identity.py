"""Select one outbound caller independently of credentials, Contact and routing.

The selected identity is carried in the standard SIP From header. An admitted
trunk originator can be preserved, but arbitrary incoming identity headers are
never copied. Authentication remains the responsibility of the trunk client.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .core import sip


CALLER_ID_POLICIES = ("trunk", "caller", "account")


def identity_text(value: object, field: str, *, user: bool = False) -> str:
    """Validate a configured number/name without changing its numbering plan."""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    value = value.strip()
    if len(value) > 256 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"Invalid {field}")
    if user and any(char.isspace() or char in '<>@"\\' for char in value):
        raise ValueError(f"{field} must be a number or SIP username, not an address")
    return value


def normalized_outbound_identity_config(data: Mapping) -> dict:
    """Keep the former trunk identity policy until an administrator changes it."""
    policy = identity_text(data.get("trunk_caller_id_policy") or "trunk", "caller ID policy").lower()
    if policy not in CALLER_ID_POLICIES:
        raise ValueError("Invalid trunk caller ID policy")
    return {
        "trunk_caller_id_policy": policy,
        "trunk_caller_id": identity_text(data.get("trunk_caller_id", ""), "trunk caller ID", user=True),
        "trunk_caller_name": identity_text(data.get("trunk_caller_name", ""), "trunk caller name"),
    }


@dataclass(frozen=True, slots=True)
class CallerIdentity:
    """The logical identity for one SIP leg, with no resource ownership."""

    user: str
    name: str
    uri: str


def resolve_trunk_caller(
    config: Mapping,
    *,
    caller_user: str,
    caller_name: str,
    caller_uri: str = "",
    account_username: str = "",
    outbound_caller_id: str = "",
    outbound_caller_name: str = "",
) -> CallerIdentity:
    """Apply the trunk choice once; an account's public number is optional.

    ``caller_uri`` belongs only to a caller admitted as an incoming trunk.
    Local accounts are selected by their authenticated canonical username.
    The ``account`` mode deliberately ignores the account's public number so
    an inter-PBX extension remains callable in the private numbering plan.
    """
    policy = normalized_outbound_identity_config(config)
    mode = policy["trunk_caller_id_policy"]
    if mode == "trunk":
        user = str(policy["trunk_caller_id"] or config.get("trunk_username") or caller_user).strip()
        name = policy["trunk_caller_name"] or user
    elif caller_uri:
        uri = sip.parse_sip_uri(caller_uri)
        return CallerIdentity(uri.user, caller_name or uri.user, str(uri))
    else:
        user = account_username or caller_user
        name = caller_name or user
        if mode == "caller":
            user = outbound_caller_id or user
            name = outbound_caller_name or name
    domain = str(config.get("trunk_domain") or config.get("trunk_server") or "").strip()
    # The client's reachable Contact is the fallback only for incomplete legacy
    # static-line configurations. It is never built from a public caller number.
    uri = str(sip.SipUri(user, domain)) if user and domain else ""
    return CallerIdentity(user, name, uri)


def outbound_identity(
    hass,
    config: Mapping,
    *,
    invite=None,
    endpoint=None,
    caller_user: str = "",
    caller_name: str = "",
    source_call_id: str = "",
) -> CallerIdentity:
    """Project existing call/account owners into the pure caller policy."""
    from .runtime_data import runtime_data

    runtime = runtime_data(hass)
    caller_uri = ""
    if invite is not None:
        caller_user = invite.routing_caller or caller_user
        caller_name = invite.caller or caller_name
        if invite.received_via_trunk:
            # Its authenticated link account is a transport principal, not the
            # calling extension. Do not replace the admitted remote originator.
            caller_uri = str(invite.caller_uri or "")
            endpoint = None
        elif endpoint is None and runtime is not None:
            endpoint = runtime.endpoints.get(invite.identified_source_endpoint_id)
            if endpoint is None and invite.authenticated_username:
                endpoint = runtime.endpoints.by_username(invite.authenticated_username)
    if endpoint is None and not caller_uri and runtime is not None:
        if source_call_id and runtime.sip is not None:
            source = runtime.sip.get_session(source_call_id)
            if source is not None and not source.metadata.get("inbound_trunk_id"):
                endpoint = runtime.endpoints.get(source.metadata.get("source_endpoint_id"))
        endpoint = endpoint or runtime.endpoints.by_username(caller_user)
    policy = endpoint.policy if endpoint is not None else None
    return resolve_trunk_caller(
        config,
        caller_user=caller_user,
        caller_name=caller_name,
        caller_uri=caller_uri,
        account_username=(endpoint.username or endpoint.sip_uri_user) if endpoint is not None else "",
        outbound_caller_id=policy.outbound_caller_id if policy is not None else "",
        outbound_caller_name=policy.outbound_caller_name if policy is not None else "",
    )
