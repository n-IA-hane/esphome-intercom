"""Compose account transports using the existing PBX fork and call owner."""

from dataclasses import dataclass, replace

from .dial_plan import RingPolicy


@dataclass(frozen=True, slots=True)
class AccountCallPlan:
    """One logical account; its policy owns the business answer deadline."""

    endpoint_id: str
    route_kind: str = "direct"

    @property
    def ring_policy(self) -> RingPolicy:
        # SIP transaction timers remain in SipCallClient. A transport fork must
        # not substitute the ring group's deadline for the account's policy.
        return RingPolicy(overall_timeout=float("inf"), step_timeout=15.0)


def account_call_plan(entry):
    """Recover the operation-local account plan without creating a group."""
    identity = str((entry.metadata if entry else {}).get("account_fork") or "")
    return AccountCallPlan(identity) if identity else None

def account_has_ha_binding(hass, endpoint_id):
    """Load the HA adapter only for a managed account's candidate path."""
    from .account_runtime import account_has_ha_binding as configured_binding
    return configured_binding(hass, endpoint_id)


def account_uses_ha_only(hass, endpoint):
    """Whether a configured account currently has only the HA receive path."""
    if endpoint is None or not getattr(endpoint, "supports_ha_client", False):
        return False
    if endpoint.kind.is_softphone:
        return True
    if not account_has_ha_binding(hass, endpoint.endpoint_id):
        return False
    from .runtime_data import sip_registrar
    registrar = sip_registrar(hass)
    return not (registrar and registrar.registered_contacts(endpoint.username))


def account_fork_entry(hass, endpoint, entry, *, all_transports=False):
    """Describe one account's mixed offer without creating another endpoint.

    A saved HA association contributes one logical browser candidate. Physical
    SIP Contacts still expand through the ordinary contact planner. This value
    is local to the operation; it is never published as a new ring group.
    """
    if endpoint is None or entry is None:
        return None
    contacts = entry.metadata.get("sip_contacts") or ()
    browser = (getattr(endpoint, "supports_ha_client", False)
               and account_has_ha_binding(hass, endpoint.endpoint_id))
    if (not contacts and not (all_transports and browser)) or (
            len(contacts) == 1 and not browser and not all_transports):
        return None
    return replace(entry, metadata={
        **entry.metadata,
        "members": [entry.id],
        "group_type": "ring",
        "account_fork": endpoint.endpoint_id,
        "route_kind": "direct",
    })


def account_dial_decision(hass, decision, *, all_transports=False):
    """Select the existing multi-transport fork after account policy resolution."""
    row = decision.entry
    identity = str((row.metadata if row else {}).get("endpoint_id") or "")
    if not identity:
        return decision
    from .router import RouteAction
    from .runtime_data import endpoint_directory

    allowed = {RouteAction.DIRECT, RouteAction.FORWARD, RouteAction.BRIDGE}
    if all_transports:
        allowed.add(RouteAction.ANSWER_HA)
    if decision.action not in allowed:
        return decision
    endpoint = endpoint_directory(hass).get(identity)
    fork = account_fork_entry(hass, endpoint, row, all_transports=all_transports)
    return replace(decision, action=RouteAction.GROUP, entry=fork) if fork else decision
