"""Correlate an internal SIP leg with the call and account that own it."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .endpoint_session import LegPhase

if TYPE_CHECKING:
    from .endpoint_registry import EndpointRegistry
    from .pbx_runtime import SipEndpointRuntime
    from .phone_endpoint import PhoneEndpoint
    from .sip_listener import SipInitialInvite


def owned_local_call_source(
    invite: SipInitialInvite,
    calls: SipEndpointRuntime,
    endpoints: EndpointRegistry,
    *,
    listener_hosts: set[str] | frozenset[str],
    listener_port: int,
) -> PhoneEndpoint | None:
    """Return the current HA source only when its actual SIP client owns the leg."""
    if invite.received_via_trunk:
        return None
    client = calls.sip_client_for(invite.call_id)
    session = calls.get_session(invite.call_id)
    if (client is None or session is None or not session.live
            or session.metadata.get("inbound_trunk_id")):
        return None
    identity = str(session.metadata.get("source_endpoint_id") or "")
    leg = session.legs.get(invite.call_id)
    source = endpoints.get(identity) if identity else None
    if (source is not None and source.supports_ha_client and source.active_call_id == session.call_id
            and session.endpoint_claims.get(identity) == "source"
            and leg is not None and leg.dialog is client and leg.role == "ha_softphone"
            and leg.phase not in {LegPhase.CLOSING, LegPhase.CLOSED}
            and calls.get_session(session.call_id, generation=session.generation) is session
            and client.matches_local_invite_source(
                invite, listener_hosts=listener_hosts, listener_port=listener_port,
            )):
        return source
    return None
