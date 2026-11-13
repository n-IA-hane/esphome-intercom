"""Compose account transports using the existing PBX fork and call owner."""

from dataclasses import replace

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


def account_fork_entry(hass, endpoint, entry):
    """Describe one account's mixed offer without creating another endpoint.

    A saved HA association contributes one logical browser candidate. Physical
    SIP Contacts still expand through the ordinary contact planner. This value
    is local to the operation; it is never published as a new ring group.
    """
    if (endpoint is None or entry is None or not getattr(endpoint, "supports_ha_client", False)
            or not account_has_ha_binding(hass, endpoint.endpoint_id)
            or not entry.metadata.get("sip_contacts")):
        return None
    return replace(entry, metadata={
        **entry.metadata,
        "members": [entry.id],
        "group_type": "ring",
        "account_fork": endpoint.endpoint_id,
        "route_kind": "direct",
    })


def account_dial_decision(hass, decision):
    """Select the existing multi-transport fork after account policy resolution."""
    row = decision.entry
    identity = str((row.metadata if row else {}).get("endpoint_id") or "")
    if not identity:
        return decision
    from .router import RouteAction
    from .runtime_data import endpoint_directory

    if decision.action not in {RouteAction.DIRECT, RouteAction.FORWARD, RouteAction.BRIDGE}:
        return decision
    endpoint = endpoint_directory(hass).get(identity)
    fork = account_fork_entry(hass, endpoint, row)
    return replace(decision, action=RouteAction.GROUP, entry=fork) if fork else decision
