"""One availability rule for canonical accounts and their configured clients."""
from dataclasses import replace

from homeassistant.core import callback

from .phone_endpoint import EndpointAvailability
from .runtime_data import runtime_data


def account_has_ha_binding(hass, endpoint_id):
    """Read configured HA associations from their single entry-owned tracker."""
    runtime = runtime_data(hass)
    bindings = getattr(runtime, "ha_bindings", None) if runtime is not None else None
    return bool(bindings is not None and bindings.has_binding(str(endpoint_id)))


@callback
def recompute_account_availability(hass, endpoint_id=None):
    """Re-read current contacts and saved bindings; callbacks are only signals."""
    from .phone_config import phone_subentries
    runtime = runtime_data(hass)
    if runtime is None:
        return
    if endpoint_id is None:
        selected = None
    elif isinstance(endpoint_id, str):
        selected = {endpoint_id}
    else:
        selected = set(endpoint_id)
    entry = next((entry for entry in hass.config_entries.async_entries("voip_stack")
                  if getattr(entry, "runtime_data", None) is runtime), None)
    if entry is None:
        return
    configured = {str(sub.data.get("endpoint_id") or ""): sub.data for sub in phone_subentries(entry)}
    registrar = runtime.sip.component("registrar") if runtime.sip is not None else None
    for endpoint in runtime.endpoints.endpoints:
        if not endpoint.supports_ha_client or (selected is not None and endpoint.endpoint_id not in selected):
            continue
        data = configured.get(endpoint.endpoint_id)
        account = registrar.accounts.get(endpoint.username.casefold()) if registrar is not None else None
        disabled = ((data is not None and not bool(data.get("enabled", True)))
                    or (data is None and endpoint.availability is EndpointAvailability.UNAVAILABLE)
                    or (account is not None and not account.enabled))
        known = data is not None or account is not None
        if disabled:
            availability = EndpointAvailability.UNAVAILABLE
        elif known and ((registrar is not None and registrar.registered_contacts(endpoint.username))
                        or account_has_ha_binding(hass, endpoint.endpoint_id)):
            availability = EndpointAvailability.AVAILABLE
        else:
            availability = EndpointAvailability.OFFLINE
        if endpoint.availability is not availability:
            runtime.endpoints.upsert(replace(endpoint, availability=availability))
