"""Home Assistant boundary for persistent phone routing defaults."""

from dataclasses import replace

from homeassistant.core import ServiceCall
from homeassistant.exceptions import ServiceValidationError

from .phone_config import update_phone_policy
from .phone_policy import DestinationKind, PhoneDestination
from .runtime_data import endpoint_directory, runtime_data
from .service_endpoints import async_require_phone_service_control, resolve_public_phone, resolve_service_phone
from .store import config_entry


async def async_set_phone_settings(call: ServiceCall) -> None:
    """Change future-call defaults without taking control of an active call."""
    registry = endpoint_directory(call.hass)
    directory = getattr(runtime_data(call.hass), "directory", None)
    endpoint = resolve_service_phone(registry, call.data["device_id"], hass=call.hass)
    if endpoint is None:
        raise ServiceValidationError("Select an existing VoIP phone")
    await async_require_phone_service_control(call.hass, call, endpoint=endpoint)
    entry = config_entry(call.hass)
    if entry is None:
        raise ServiceValidationError("VoIP Stack configuration is unavailable")
    updates = {}
    if "outbound_trunk" in call.data:
        from .trunk_selection import resolve_trunk_selector

        updates["outbound_trunk_id"] = resolve_trunk_selector(call.hass, call.data["outbound_trunk"])
    for field, slot in (
        ("forward_to", "always"),
        ("busy_to", "busy"),
        ("unavailable_to", "unavailable"),
    ):
        if field not in call.data:
            continue
        target = str(call.data[field]).strip()
        destination = resolve_public_phone(registry, target, directory=directory) if target else None
        if destination is not None:
            if destination.endpoint_id == endpoint.endpoint_id:
                raise ServiceValidationError("A phone cannot forward to itself")
            target = destination.endpoint_id
        updates[slot] = PhoneDestination(DestinationKind.CONTACT, target) if target else None
    if "ringing_timeout" in call.data:
        timeout = float(call.data["ringing_timeout"])
        updates["ringing_timeout"] = timeout or None
    if call.data.get("ringing_timeout") == 0:
        updates["no_answer"] = None
    elif "no_answer_action" in call.data:
        if call.data["no_answer_action"] == "hangup":
            updates["no_answer"] = PhoneDestination(DestinationKind.HANGUP)
        elif call.data["no_answer_action"] == "voicemail":
            from .mailbox import mailbox_settings
            target = str(call.data.get("no_answer_to") or "").strip()
            destination = resolve_public_phone(registry, target, directory=directory) if target else endpoint
            if destination is None or not mailbox_settings(call.hass, destination.endpoint_id).get("enabled"):
                raise ServiceValidationError("Enable voicemail for the destination phone first")
            updates["no_answer"] = PhoneDestination(DestinationKind.VOICEMAIL, destination.endpoint_id)
        else:
            target = str(call.data.get("no_answer_to") or "").strip()
            if not target:
                raise ServiceValidationError("Select a no-answer destination")
            destination = resolve_public_phone(registry, target, directory=directory)
            if destination is not None:
                if destination.endpoint_id == endpoint.endpoint_id:
                    raise ServiceValidationError("A phone cannot forward to itself")
                target = destination.endpoint_id
            updates["no_answer"] = PhoneDestination(DestinationKind.CONTACT, target)
    elif "no_answer_to" in call.data:
        raise ServiceValidationError("Select the forward or voicemail no-answer action with its destination")
    try:
        policy = replace(endpoint.policy, **updates)
    except ValueError as err:
        raise ServiceValidationError(str(err)) from err
    visited = {endpoint.endpoint_id}
    current = policy.always
    while current is not None and current.kind is DestinationKind.CONTACT:
        target = registry.resolve(current.target, namespace="endpoint_id")
        if target is None:
            target = resolve_public_phone(registry, current.target, directory=directory)
        if target is None:
            break
        if target.endpoint_id in visited:
            raise ServiceValidationError("The unconditional forwarding rules form a loop")
        visited.add(target.endpoint_id)
        current = target.policy.always
    update_phone_policy(call.hass, entry, endpoint, policy)
    # Use the established ESPHome API delivery path, including remote devices.
    from . import _refresh_directory

    await _refresh_directory(call.hass)
