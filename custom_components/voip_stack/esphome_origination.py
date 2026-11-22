"""Native phone commands which leave SIP origination to the phone."""

from homeassistant.exceptions import ServiceValidationError

from .esphome_actions import async_call_action, has_action
from .runtime_data import require_runtime_data
from .trunk_selection import public_trunk_destination, select_call_trunk


async def originate_phone_call(call, phone, destination):
    """Send a dial command without inventing a session or its SIP Call-ID."""
    runtime = require_runtime_data(call.hass)
    endpoint = runtime.endpoints.get(phone.endpoint_id)
    if endpoint is not None and endpoint.active_call_id:
        raise ServiceValidationError("The selected phone is busy")
    device = phone.transport_data
    if not has_action(call.hass, device, "start_call_on_line"):
        raise ServiceValidationError("Rebuild this phone's firmware to use its SIP account")
    target = str(destination).strip()
    if not target or len(target.encode()) > 2048 or any(ord(char) < 32 for char in target):
        raise ServiceValidationError("Choose a valid SIP destination")
    if requested := str(call.data.get("trunk_id") or ""):
        selection = select_call_trunk(call.hass, requested=requested, endpoint_id=phone.endpoint_id)
        if not selection.available:
            raise selection.unavailable_error()
        target = public_trunk_destination(call.hass, selection.identity, target)
    send_video = bool(call.data.get("send_video", False))
    receive_video = bool(call.data.get("receive_video", send_video))
    await async_call_action(call.hass, device, "start_call_on_line",
                            {"dest": target, "line": "ha", "send_video": send_video, "receive_video": receive_video},
                            context=call.context)
    # The command was delivered. The actual INVITE will establish call state.
    return ""
