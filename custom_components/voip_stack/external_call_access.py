"""Administrator-controlled, config-entry-backed trunk access for each phone."""

from homeassistant.core import ServiceCall
from homeassistant.exceptions import ServiceValidationError

from .authorization import async_require_service_admin
from .runtime_data import require_runtime_data
from .store import config_entry
from .trunk_policy import EXTERNAL_CALL_BLOCKED_ENDPOINTS


async def async_set_external_call_access(call: ServiceCall) -> dict:
    await async_require_service_admin(call.hass, call)
    runtime = require_runtime_data(call.hass)
    endpoint = runtime.endpoints.by_device_id(str(call.data["device_id"]))
    if endpoint is None:
        raise ServiceValidationError("Select a VoIP Stack phone, SIP account or discovered ESPHome phone")
    entry = config_entry(call.hass)
    if entry is None:
        raise ServiceValidationError("VoIP Stack is not configured")
    data = dict(entry.data)
    blocked = set(data.get(EXTERNAL_CALL_BLOCKED_ENDPOINTS, ()))
    allowed = bool(call.data["allowed"])
    if allowed:
        blocked.discard(endpoint.endpoint_id)
    else:
        blocked.add(endpoint.endpoint_id)
    if blocked:
        data[EXTERNAL_CALL_BLOCKED_ENDPOINTS] = sorted(blocked)
    else:
        data.pop(EXTERNAL_CALL_BLOCKED_ENDPOINTS, None)
    call.hass.config_entries.async_update_entry(entry, data=data)
    # All SIP route runtimes share this projection. Do not reload transports or
    # interrupt established calls merely because permission for new legs changes.
    runtime.transport_config[EXTERNAL_CALL_BLOCKED_ENDPOINTS] = tuple(sorted(blocked))
    return {"device_id": endpoint.device_id, "endpoint_id": endpoint.endpoint_id, "allowed": allowed}
