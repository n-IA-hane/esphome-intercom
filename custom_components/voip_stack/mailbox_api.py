"""Mailbox metadata controls; audio belongs to the normal phone call."""

import asyncio

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.exceptions import ServiceValidationError

from .authorization import require_websocket_endpoint_read
from .core.media_archive import ArchiveError
from .mailbox import get_archive
from .runtime_data import require_runtime_data
from .endpoint_registry import EndpointLookup
from .service_endpoints import resolve_service_phone


def _endpoint(hass, selector):
    registry = require_runtime_data(hass).endpoints
    endpoint = resolve_service_phone(registry, selector, allow_internal=True,
                                     namespace=EndpointLookup.ENDPOINT_ID)
    if endpoint is None:
        raise ValueError("The selected phone is unavailable")
    return endpoint


def _metadata(entry):
    return {"id": entry["id"], "caller": entry["caller"], "created_at": entry["created_at"],
            "read": entry["read"], "partial": entry["status"] == "partial",
            "duration": max((part["start"] + part["frames"] / part["sample_rate"]
                             for part in entry["segments"]), default=0)}


async def _call_selections(hass, call_id, endpoint_id):
    """Read the numbering of this phone's already established mailbox call."""
    from .endpoint_lifecycle import call_registry
    from .voicemail_menu import VoicemailMenu

    application = call_registry(hass).resource_for(call_id, "voicemail")
    if (not isinstance(application, VoicemailMenu)
            or application.caller_endpoint_id != endpoint_id
            or application.mailbox_id != endpoint_id):
        raise ValueError("This call is not accessing the selected phone's own mailbox")
    async with asyncio.timeout(10):
        await application.ready.wait()
    application.require_current()
    return {identity: f"0{number}#" for number, identity in enumerate(application.message_ids, 1)}


@websocket_api.websocket_command({
    vol.Required("type"): "voip_stack/voicemail/list",
    vol.Required("endpoint_id"): str,
    vol.Optional("offset", default=0): vol.All(int, vol.Range(min=0)),
    vol.Optional("limit", default=50): vol.All(int, vol.Range(min=1, max=100)),
    vol.Optional("call_id"): str,
    vol.Optional("message_id"): str,
})
@websocket_api.async_response
async def websocket_list(hass, connection, msg):
    try:
        endpoint = _endpoint(hass, msg["endpoint_id"])
        require_websocket_endpoint_read(hass, connection, endpoint)
        archive = await get_archive(hass)
        selections = {}
        if call_id := msg.get("call_id"):
            selections = await _call_selections(hass, call_id, endpoint.endpoint_id)
        unread, read = archive.counts(endpoint.endpoint_id)
        messages = ([archive.message(msg["message_id"], endpoint.endpoint_id)] if msg.get("message_id")
                    else archive.list(endpoint.endpoint_id, offset=msg["offset"], limit=msg["limit"]))
        connection.send_result(msg["id"], {
            "unread": unread, "total": unread + read,
            "selections": selections,
            "messages": [_metadata(item) for item in messages],
        })
    except (ValueError, ArchiveError, TimeoutError, ServiceValidationError) as err:
        connection.send_error(msg["id"], "mailbox_unavailable", str(err))


def async_register_mailbox_api(hass):
    websocket_api.async_register_command(hass, websocket_list)
