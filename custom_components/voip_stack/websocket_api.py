"""Authenticated WebSocket transport for shared VoIP phone state."""

from __future__ import annotations

import logging
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
)

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import (
    HomeAssistant,
    callback,
)
from homeassistant.exceptions import Unauthorized

from .authorization import (
    media_controller_status,
    require_websocket_endpoint_read,
    require_websocket_endpoint_control,
    require_websocket_read,
    websocket_can_control_endpoint,
)
from .const import DOMAIN
from .endpoint_lifecycle import call_registry
from .runtime_data import (
    require_runtime_data,
    runtime_data,
)
from .websocket_owner import media_websocket_owner_status
from .const import (
    CALL_EVENT as CALL_EVENT,
    HA_SOFTPHONE_STATE_EVENT as HA_SOFTPHONE_STATE_EVENT,
    SIP_CALL_STATE_EVENT as SIP_CALL_STATE_EVENT,
    SIP_DTMF_EVENT as SIP_DTMF_EVENT,
    SIP_INCOMING_CALL_EVENT as SIP_INCOMING_CALL_EVENT,
    SIP_ROUTE_REQUEST_EVENT as SIP_ROUTE_REQUEST_EVENT,
)
from .call_events import (
    fire_call_event as _fire_call_event,  # noqa: F401
)
from .browser_phone_data import (
    _clean_group_token as _clean_group_token,
    _clean_group_name as _clean_group_name,
    _clean_endpoint_field as _clean_endpoint_field,
    _endpoint_store_id as _endpoint_store_id,
    _ha_softphone_store as _ha_softphone_store,
    _ha_softphone_stores as _ha_softphone_stores,
    _endpoint_registry as _endpoint_registry,
    _browser_endpoint as _browser_endpoint,
    _ha_softphone_groups as _ha_softphone_groups,
    _ha_softphone_extension as _ha_softphone_extension,
    _ha_softphone_auto_answer as _ha_softphone_auto_answer,
    _ha_softphone_send_video as _ha_softphone_send_video,
    _ha_softphone_dnd as _ha_softphone_dnd,
    _endpoint_id_from_selector as _endpoint_id_from_selector,
    _MEDIA_COUNTER_KEYS as _MEDIA_COUNTER_KEYS,
    _CALL_SCOPED_SOFTPHONE_FIELDS as _CALL_SCOPED_SOFTPHONE_FIELDS,
)
from .browser_phone_projection import (
    _runtime_counter as _runtime_counter,
    _sip_runtime_snapshot as _sip_runtime_snapshot,
    _account_browser_media as _account_browser_media,
    _account_shared_projection as _account_shared_projection,
    _ha_softphone_state as _ha_softphone_state,
    _ha_softphone_device as _ha_softphone_device,
    _ha_softphone_devices as _ha_softphone_devices,
)
from .browser_phone_runtime import (
    _update_browser_presence as _update_browser_presence,
    _release_ha_softphone_claim as _release_ha_softphone_claim,
    async_set_ha_softphone_settings as async_set_ha_softphone_settings,
    _async_shutdown_all as _async_shutdown_all,
    _async_load_ha_softphone_store as _async_load_ha_softphone_store,
    _async_save_ha_softphone_store as _async_save_ha_softphone_store,
    _publish_ha_softphone_state as _publish_ha_softphone_state,
    _set_ha_softphone_call_state as _set_ha_softphone_call_state,
    _reset_ha_softphone_store as _reset_ha_softphone_store,
    _set_sip_bridge_call_state as _set_sip_bridge_call_state,
)
from .esphome_phone_inventory import _get_voip_devices as _get_voip_devices


if TYPE_CHECKING:
    from homeassistant.core import Event

_LOGGER = logging.getLogger(__name__)

WS_TYPE_LIST = f"{DOMAIN}/list_devices"

WS_TYPE_RESOLVE_DEVICE = f"{DOMAIN}/resolve_device"

WS_TYPE_HA_SOFTPHONE_STATE = f"{DOMAIN}/ha_softphone_state"

WS_TYPE_SUBSCRIBE_CALL_EVENTS = f"{DOMAIN}/subscribe_call_events"

WS_TYPE_SUBSCRIBE_HA_SOFTPHONE = f"{DOMAIN}/subscribe_ha_softphone_state"

WS_TYPE_SUBSCRIBE_PHONE_STATES = f"{DOMAIN}/subscribe_phone_states"


def async_register_websocket_api(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, websocket_subscribe_phone_states)
    websocket_api.async_register_command(hass, websocket_subscribe_call_events)
    websocket_api.async_register_command(hass, websocket_subscribe_ha_softphone_state)
    websocket_api.async_register_command(hass, websocket_ha_softphone_state)
    websocket_api.async_register_command(hass, websocket_list_devices)
    websocket_api.async_register_command(hass, websocket_resolve_device)
    websocket_api.async_register_command(hass, websocket_ha_popup_binding)
    websocket_api.async_register_command(hass, websocket_set_ha_popup_binding)


@websocket_api.websocket_command({vol.Required("type"): WS_TYPE_SUBSCRIBE_PHONE_STATES})
@callback
def websocket_subscribe_phone_states(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: Dict[str, Any],
) -> None:
    """Observe readable phones without publishing browser availability."""
    from .phone_monitor import PhoneStateSubscription

    PhoneStateSubscription(hass, connection, msg["id"]).start()


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_SUBSCRIBE_CALL_EVENTS,
        vol.Optional("include_activity", default=False): bool,
        vol.Optional("include_runtime_ready", default=False): bool,
        vol.Optional("endpoint_id", default=""): str,
        vol.Optional("device_id", default=""): str,
    }
)
@callback
def websocket_subscribe_call_events(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: Dict[str, Any],
) -> None:
    def user_active():
        user = connection.user
        return bool(user is not None and getattr(user, "id", "") and getattr(user, "is_active", True))

    def readable_calls():
        if not user_active():
            return False
        try:
            require_websocket_read(hass, connection)
        except Unauthorized:
            return False
        return True

    # Readiness has no phone/call payload. Endpoint-only readers may use this
    # same transport without gaining global call or activity permissions.
    if not msg.get("include_runtime_ready", False):
        require_websocket_read(hass, connection)
    elif not user_active():
        raise Unauthorized(user_id=getattr(connection.user, "id", None))
    msg_id = msg["id"]
    requested_endpoint = str(msg.get("endpoint_id") or "").strip()
    if not requested_endpoint and msg.get("device_id"):
        try:
            requested_endpoint = _endpoint_id_from_selector(
                hass, device_id=msg.get("device_id")
            )
        except ValueError as err:
            connection.send_error(msg_id, "unknown_endpoint", str(err))
            return
    elif requested_endpoint:
        try:
            requested_endpoint = _endpoint_id_from_selector(
                hass, endpoint_id=requested_endpoint
            )
        except ValueError as err:
            connection.send_error(msg_id, "unknown_endpoint", str(err))
            return
    selected_endpoint = _browser_endpoint(hass, requested_endpoint)
    if selected_endpoint is not None:
        require_websocket_endpoint_read(hass, connection, selected_endpoint)

    @callback
    def forward_call_event(event) -> None:
        if not readable_calls():
            return
        event_endpoint = str(event.data.get("endpoint_id") or "").strip()
        if requested_endpoint:
            if event_endpoint and event_endpoint != requested_endpoint:
                return
            if not event_endpoint:
                return
        connection.send_event(msg_id, {"event_type": CALL_EVENT, "data": event.data})

    from .phone_state import phone_state_observer

    @callback
    def forward_activity(activity):
        if readable_calls():
            connection.send_event(msg_id, {"event_type": f"{DOMAIN}.activity", "data": activity})

    listeners = [hass.bus.async_listen(CALL_EVENT, forward_call_event)]
    activity_unsubscribe = None

    @callback
    def attach_activity():
        nonlocal activity_unsubscribe
        if activity_unsubscribe is not None:
            activity_unsubscribe()
            activity_unsubscribe = None
        runtime = runtime_data(hass)
        if (not msg.get("include_activity", False) or not readable_calls()
                or runtime is None or runtime.sip is None or not runtime.sip.active):
            return
        observer = phone_state_observer(hass)
        activity_unsubscribe = observer.subscribe(forward_activity, kind="activity")
        forward_activity(observer.activity)

    @callback
    def forward_runtime_ready(_event):
        if not user_active():
            return
        connection.send_event(msg_id, {"event_type": f"{DOMAIN}.runtime_ready", "data": {}})
        attach_activity()

    if msg.get("include_runtime_ready", False):
        listeners.append(hass.bus.async_listen(f"{DOMAIN}.runtime_ready", forward_runtime_ready))

    @callback
    def unsubscribe():
        if activity_unsubscribe is not None:
            activity_unsubscribe()
        for listener in listeners:
            listener()

    connection.subscriptions[msg_id] = unsubscribe
    connection.send_result(msg_id)
    attach_activity()


def _client_phone_state(hass, connection, endpoint_id, state=None):
    """Project per-user controls without storing permissions in account state."""
    return {**(_ha_softphone_state(hass, endpoint_id) if state is None else state),
            "can_control": websocket_can_control_endpoint(hass, connection, _browser_endpoint(hass, endpoint_id))}


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_SUBSCRIBE_HA_SOFTPHONE,
        vol.Optional("endpoint_id", default=""): str,
        vol.Optional("device_id", default=""): str,
    }
)
@callback
def websocket_subscribe_ha_softphone_state(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: Dict[str, Any],
) -> None:
    """Stream authoritative HA softphone snapshots."""
    require_websocket_read(hass, connection)
    msg_id = msg["id"]
    try:
        requested_endpoint = _endpoint_id_from_selector(
            hass,
            endpoint_id=msg.get("endpoint_id"),
            device_id=msg.get("device_id"),
        )
    except ValueError as err:
        connection.send_error(msg_id, "unknown_endpoint", str(err))
        return
    selected_endpoint = _browser_endpoint(hass, requested_endpoint)
    if selected_endpoint is not None:
        require_websocket_endpoint_read(hass, connection, selected_endpoint)
    from .phone_monitor import PhoneSubscriptionAccess
    from .phone_state import phone_state_observer
    from homeassistant.exceptions import ServiceValidationError

    access = PhoneSubscriptionAccess(hass, connection)
    unsubscribers = []
    released = False
    publishes_presence = False

    @callback
    def unsubscribe() -> None:
        nonlocal released, publishes_presence
        if released:
            return
        released = True
        for detach in unsubscribers:
            detach()
        unsubscribers.clear()
        if publishes_presence:
            publishes_presence = False
            _update_browser_presence(hass, requested_endpoint, -1)

    @callback
    def close(reason):
        if released:
            return
        try:
            connection.send_event(msg_id, {
                "kind": "closed", "reason": reason,
                "endpoint_id": requested_endpoint,
                "device_id": getattr(selected_endpoint, "device_id", ""),
            })
        finally:
            unsubscribe()

    def readable():
        if released:
            return False
        # Preserve both gates used when opening this account stream. The
        # passive multi-phone monitor intentionally needs only per-phone read.
        try:
            require_websocket_read(hass, connection)
        except Unauthorized:
            close("permissions_revoked")
            return False
        endpoint = _browser_endpoint(hass, requested_endpoint)
        if selected_endpoint is None and endpoint is None and access.active:
            return True  # Older singleton uses the integration boundary.
        if access.readable(endpoint):
            return True
        close("permissions_revoked")
        return False

    @callback
    def emit(state=None):
        nonlocal publishes_presence
        if not readable():
            return
        payload = _client_phone_state(hass, connection, requested_endpoint, state)
        presence = bool(payload["can_control"])
        if presence != publishes_presence:
            publishes_presence = presence
            _update_browser_presence(hass, requested_endpoint, 1 if presence else -1, publish=False)
        connection.send_event(msg_id, payload)

    @callback
    def forward_state(event: Event) -> None:
        if str(event.data.get("endpoint_id") or "").strip() == requested_endpoint:
            emit(event.data)

    @callback
    def forward_access(update):
        if released:
            return
        terminal = access.terminal_reason(update)
        if terminal:
            close(terminal)
        elif readable() and update.access_event is not None:
            emit()

    @callback
    def forward_projection(change, _context=None):
        if change.get("endpoint_id") == requested_endpoint and readable():
            state = _ha_softphone_state(hass, requested_endpoint)
            previous_calls = (change.get("previous") or {}).get("calls", ())
            occurrence = change.get("call") or {}
            if not (change.get("phone") or {}).get("calls") and state["state"] in {"idle", "unavailable"}:
                ended = next((call for call in previous_calls if call["call_id"] == occurrence.get("call_id")),
                             previous_calls[0] if len(previous_calls) == 1 else None)
                if ended is not None:
                    # A native SIP participant has no browser runtime cache.
                    # Scope its complete idle snapshot to the actual prior call
                    # so the frontend can retain its stale-terminal guards.
                    state["call_id"] = ended["call_id"]
                    state["generation"] = ended["generation"]
                    if occurrence.get("call_id") == ended["call_id"]:
                        state["sequence"] = occurrence.get("sequence", state["sequence"])
                        state["revision"] = occurrence.get("revision", state["revision"])
            emit(state)
    unsubscribers.append(hass.bus.async_listen(HA_SOFTPHONE_STATE_EVENT, forward_state))
    try:
        observer = phone_state_observer(hass)
        unsubscribers.append(observer.subscribe(forward_projection))
        unsubscribers.append(observer.subscribe(forward_access, kind="projection"))
    except ServiceValidationError:
        pass
    connection.subscriptions[msg_id] = unsubscribe
    try:
        connection.send_result(msg_id)
        emit()
    except Exception:
        unsubscribe()
        raise


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_HA_SOFTPHONE_STATE,
        vol.Optional("endpoint_id", default=""): str,
        vol.Optional("device_id", default=""): str,
        vol.Optional("media_client_id", default=""): vol.Any(
            "",
            vol.Match(r"^[A-Za-z0-9][A-Za-z0-9._~-]{15,127}$"),
        ),
    }
)
@websocket_api.async_response
async def websocket_ha_softphone_state(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: Dict[str, Any],
) -> None:
    require_websocket_read(hass, connection)
    try:
        endpoint_id = _endpoint_id_from_selector(
            hass,
            endpoint_id=msg.get("endpoint_id"),
            device_id=msg.get("device_id"),
        )
    except ValueError as err:
        connection.send_error(msg["id"], "unknown_endpoint", str(err))
        return
    selected_endpoint = _browser_endpoint(hass, endpoint_id)
    if selected_endpoint is not None:
        require_websocket_endpoint_read(hass, connection, selected_endpoint)
    state = _client_phone_state(hass, connection, endpoint_id)
    media_client_id = str(msg.get("media_client_id") or "")
    if media_client_id:
        owner_status = media_websocket_owner_status(
            require_runtime_data(hass).media,
            call_registry(hass),
            str(state.get("call_id") or ""),
            endpoint_id,
            media_client_id,
            local_bridge=require_runtime_data(hass).local_bridge,
        )
        controller_status = media_controller_status(
            call_registry(hass),
            str(state.get("call_id") or ""),
            endpoint_id,
            str(getattr(connection.user, "id", "") or ""),
        )
        state["media_owner"] = "other" if not state["can_attach_media"] or controller_status == "other" else owner_status
        state["owns_media"] = state["can_attach_media"] and state["media_owner"] == "self"
    connection.send_result(msg["id"], state)


@websocket_api.websocket_command({vol.Required("type"): WS_TYPE_LIST})
@websocket_api.async_response
async def websocket_list_devices(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: Dict[str, Any],
) -> None:
    from .entity_identity import shared_entity_ids

    require_websocket_read(hass, connection)
    devices = []
    for device in [*_ha_softphone_devices(hass), *(await _get_voip_devices(hass))]:
        endpoint = _endpoint_registry(hass).get(device.get("endpoint_id"))
        if endpoint is not None:
            try:
                require_websocket_endpoint_read(hass, connection, endpoint)
            except Unauthorized:
                continue
            device["can_control"] = websocket_can_control_endpoint(hass, connection, endpoint)
        devices.append(device)
    connection.send_result(
        msg["id"],
        {
            "devices": devices,
            "entity_ids": shared_entity_ids(hass),
        },
    )


@websocket_api.websocket_command(
    {vol.Required("type"): WS_TYPE_RESOLVE_DEVICE, vol.Required("device_id"): str}
)
@websocket_api.async_response
async def websocket_resolve_device(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: Dict[str, Any],
) -> None:
    require_websocket_read(hass, connection)
    device_id = str(msg.get("device_id") or "")
    registry = _endpoint_registry(hass)
    endpoint = registry.by_device_id(device_id) if registry is not None else None
    if endpoint is not None and endpoint.supports_ha_client:
        require_websocket_endpoint_read(hass, connection, endpoint)
        connection.send_result(
            msg["id"],
            {"device": _ha_softphone_device(hass, endpoint.endpoint_id)},
        )
        return
    for device in await _get_voip_devices(hass):
        if str(device.get("device_id") or "") == device_id:
            connection.send_result(msg["id"], {"device": device})
            return
    connection.send_result(msg["id"], {"device": None})


@websocket_api.websocket_command({vol.Required("type"): "voip_stack/ha_popup_binding",
    vol.Required("binding_id"): vol.Match(r"^[A-Za-z0-9._~-]{16,128}$")})
@callback
def websocket_ha_popup_binding(hass, connection, msg):
    require_websocket_read(hass, connection)
    owner = require_runtime_data(hass).ha_bindings
    connection.send_result(msg["id"], owner.popup_binding(connection.user.id, msg["binding_id"]) if owner is not None else None)


@websocket_api.websocket_command({vol.Required("type"): "voip_stack/set_ha_popup_binding",
    vol.Required("binding_id"): vol.Match(r"^[A-Za-z0-9._~-]{16,128}$"),
    vol.Required("endpoint_id"): str, vol.Required("enabled"): bool})
@websocket_api.async_response
async def websocket_set_ha_popup_binding(hass, connection, msg):
    require_websocket_read(hass, connection)
    owner = require_runtime_data(hass).ha_bindings
    if owner is None:
        connection.send_error(msg["id"], "not_ready", "HA client associations are unavailable")
        return
    endpoint = _endpoint_registry(hass).get(msg["endpoint_id"])
    if endpoint is not None:
        require_websocket_endpoint_control(hass, connection, endpoint)
    elif msg["enabled"]:
        connection.send_error(msg["id"], "unknown_endpoint", "Select an existing SIP account")
        return
    try:
        value = await owner.async_set_popup_binding(connection.user.id, msg["binding_id"], msg["endpoint_id"], msg["enabled"])
    except ValueError as error:
        connection.send_error(msg["id"], "invalid_format", str(error))
        return
    connection.send_result(msg["id"], value)
