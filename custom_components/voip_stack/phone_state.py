"""Read-only phone projections shared by HA actions and state observers."""

from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from types import SimpleNamespace
import logging

from homeassistant.core import callback
from homeassistant.exceptions import ServiceValidationError

from .dialog_info import snapshot_all
from .endpoint_registry import EndpointRegistryEvent, EndpointRegistryEventType
from .runtime_data import require_runtime_data

_LOGGER = logging.getLogger(__name__)

PHONE_STATE_CHANGED = "voip_stack.phone_state_changed"


@dataclass(frozen=True, slots=True)
class PhoneProjectionUpdate:
    """One shared projection update, separate from call ownership."""

    phones: dict
    removed: tuple[str, ...] = ()
    access_event: object = None
    closed: bool = False


def phone_state_dto(phone, *, peer_readable):
    """Expose the monitor's UI contract from the existing phone projection.

    Legacy ``phone_states`` retains its richer service/BLF-compatible shape.
    This detached view contains canonical call identities and display fields.
    """
    calls = []
    for call in phone["calls"]:
        peer_id = str(call.get("peer_endpoint_id") or "")
        calls.append({
            "id": str(call.get("call_id") or ""),
            "generation": int(call.get("generation") or 0),
            "leg_id": str(call.get("leg_id") or ""),
            "direction": "outgoing" if call["direction"] == "initiator" else "incoming",
            "state": call["state"],
            "held": bool(call.get("held")),
            "peer_endpoint_id": peer_id if peer_id and peer_readable(peer_id) else "",
            "peer_name": str(call.get("peer_name") or ""),
            "pickup_available": bool(call.get("pickup_available")),
        })
    mailbox = phone["mailbox"]
    return {
        **{key: phone[key] for key in (
            "schema_version", "endpoint_id", "device_id", "name", "extension",
            "kind", "availability", "dnd", "state",
        )},
        "calls": calls,
        "mailbox": {key: mailbox[key] for key in ("enabled", "waiting", "new", "old")},
    }


def resolve_phone(hass, selector):
    directory = require_runtime_data(hass).endpoints
    endpoint = directory.by_device_id(selector) or directory.resolve(selector)
    if endpoint is None:
        raise ServiceValidationError("Select an existing phone")
    return endpoint


def mailbox_state(hass, endpoint_id, archive=None):
    from .mailbox import mailbox_settings

    enabled = bool(
        mailbox_settings(hass, endpoint_id).get("enabled")
    )
    runtime = require_runtime_data(hass)
    if archive is None and runtime.sip is not None:
        archive = runtime.sip.component("media_archive")
    new, old = (
        archive.counts(endpoint_id) if enabled and archive is not None else (0, 0)
    )
    return {"enabled": enabled, "waiting": bool(new), "new": new, "old": old}


def phone_states(hass, *, dialogs=None):
    """Build one detached projection, without changing call ownership."""
    runtime = require_runtime_data(hass)
    dialogs = snapshot_all(runtime) if dialogs is None else dialogs
    result = {}
    for endpoint in runtime.endpoints.endpoints:
        states = tuple(
            item
            for item in dialogs.get(endpoint.endpoint_id, ())
            if item.state != "terminated"
        )
        ringing = any(
            item.state == "early" and item.direction == "recipient" for item in states
        )
        confirmed = tuple(item for item in states if item.state == "confirmed")
        state = (
            "ringing"
            if ringing
            else "held"
            if confirmed and all(item.held for item in confirmed)
            else "in_call"
            if confirmed
            else "calling"
            if states
            else "idle"
            if endpoint.availability.value == "available"
            else "unavailable"
        )
        calls = []
        for item in states:
            value = asdict(item)
            value["sip_call_id"] = value.pop("call_id")
            value["call_id"] = value.pop("session_id")
            value["pickup_available"] = (
                item.state == "early" and item.direction == "recipient"
            )
            calls.append(value)
        result[endpoint.endpoint_id] = {
            "schema_version": 1,
            "endpoint_id": endpoint.endpoint_id,
            "device_id": endpoint.device_id,
            "name": endpoint.name,
            "extension": endpoint.extension,
            "kind": endpoint.kind.value,
            "availability": endpoint.availability.value,
            "dnd": endpoint.dnd,
            "state": state,
            "calls": calls,
            "mailbox": mailbox_state(hass, endpoint.endpoint_id),
        }
    return result


async def async_get_phone_state(call):
    from .authorization import async_require_service_endpoint_read

    endpoint = resolve_phone(call.hass, call.data["device_id"])
    await async_require_service_endpoint_read(call.hass, call, endpoint)
    return phone_states(call.hass)[endpoint.endpoint_id]


class PhoneStateObserver:
    """One runtime-owned projection feeds HA and SIP without per-card scans."""

    def __init__(self, hass):
        self.hass = hass
        self.listeners = set()
        self.activity_listeners = set()
        self.dialog_listeners = set()
        self.projection_listeners = set()
        self.access_unsubscribers = []
        self.unsubscribers = []
        self.previous = {}
        self.dialogs = {}
        self.activity = {"phones": [], "parked": []}

    def _start(self):
        from .mailbox import EVENT_VOICEMAIL_CHANGED
        from .parking import PARKING_CHANGED
        from .dialog_info import activity_snapshot

        if self.unsubscribers:
            return
        runtime = require_runtime_data(self.hass)
        self.dialogs = snapshot_all(runtime)
        self.previous = phone_states(self.hass, dialogs=self.dialogs)
        self.activity = activity_snapshot(runtime, dialogs=self.dialogs)
        self.unsubscribers = [
            self.hass.bus.async_listen(name, self.refresh)
            for name in (EVENT_VOICEMAIL_CHANGED, PARKING_CHANGED)
        ]
        self.unsubscribers.append(runtime.sip.subscribe_state(self._committed))
        self.unsubscribers.append(runtime.endpoints.subscribe(self._endpoint_changed))

    @callback
    def _committed(self, snapshot):
        runtime = require_runtime_data(self.hass)
        self.refresh(SimpleNamespace(
            event_type="call_commit",
            data=self._call_fields(runtime.sip, snapshot),
            context=runtime.sip.ha_context(snapshot.call_id),
        ))

    @staticmethod
    def _call_fields(registry, snapshot):
        return {"call_id": snapshot.call_id, "generation": snapshot.generation,
                "sequence": snapshot.sequence, "revision": snapshot.revision,
                "state": snapshot.state, "caller": snapshot.caller,
                "callee": snapshot.callee, "route_kind": snapshot.route_kind,
                "event_type": "state_changed", "scope": "call_commit",
                **registry.event_fields(snapshot.call_id, snapshot.state)}

    @callback
    def _endpoint_changed(self, event: EndpointRegistryEvent):
        previous = event.previous
        if (
            event.event_type is EndpointRegistryEventType.UPDATED
            and previous is not None
            and replace(event.endpoint, active_call_id=previous.active_call_id)
            == previous
        ):
            # Claims precede the committed call projection and its action context.
            # Call events publish those transitions; registry events cover identity
            # and availability changes independently of a call.
            return
        self.refresh(dialogs=self.dialogs)
        if previous is not None and (
            previous.entity_ids != event.endpoint.entity_ids
            or previous.device_id != event.endpoint.device_id
            or previous.kind != event.endpoint.kind
        ):
            self._access_changed(event)

    def subscribe(self, listener, *, kind="state"):
        self._start()
        listeners = {
            "state": self.listeners,
            "activity": self.activity_listeners,
            "dialog": self.dialog_listeners,
            "projection": self.projection_listeners,
        }[kind]
        listeners.add(listener)
        if kind == "projection" and not self.access_unsubscribers:
            from homeassistant.auth import EVENT_USER_UPDATED, EVENT_USER_REMOVED
            from homeassistant.helpers.entity_registry import EVENT_ENTITY_REGISTRY_UPDATED

            self.access_unsubscribers = [
                self.hass.bus.async_listen(name, self._access_changed)
                for name in (EVENT_USER_UPDATED, EVENT_USER_REMOVED, EVENT_ENTITY_REGISTRY_UPDATED)
            ]

        def unsubscribe():
            listeners.discard(listener)
            if not self.projection_listeners:
                self._stop_access_observation()
            if not (self.listeners or self.activity_listeners or self.dialog_listeners or self.projection_listeners):
                self._stop()

        return unsubscribe

    def _stop(self):
        self._stop_access_observation()
        for unsubscribe in self.unsubscribers:
            unsubscribe()
        self.unsubscribers.clear()
        self.previous = {}
        self.dialogs = {}
        self.activity = {"phones": [], "parked": []}

    def _stop_access_observation(self):
        for unsubscribe in self.access_unsubscribers:
            unsubscribe()
        self.access_unsubscribers.clear()

    @callback
    def _access_changed(self, event):
        # Access changes reuse the cached projection and never scan call sessions.
        self._deliver(self.projection_listeners, PhoneProjectionUpdate({}, access_event=event))

    @staticmethod
    def _deliver(listeners, *args):
        for listener in tuple(listeners):
            try:
                listener(*args)
            except Exception:
                _LOGGER.exception("Phone state observer failed")

    @callback
    def refresh(self, event=None, *, dialogs=None):
        from .dialog_info import activity_snapshot
        from .websocket_api import CALL_EVENT
        from .parking import PARKING_CHANGED

        runtime = require_runtime_data(self.hass)
        if dialogs is None:
            dialogs = (
                snapshot_all(runtime)
                if event is None or event.event_type in {CALL_EVENT, PARKING_CHANGED, "call_commit"}
                else self.dialogs
            )
        current = phone_states(self.hass, dialogs=dialogs)
        activity = activity_snapshot(runtime, dialogs=dialogs)
        previous, self.previous = self.previous, current
        previous_dialogs, self.dialogs = self.dialogs, dialogs
        previous_activity, self.activity = self.activity, activity
        event_data = getattr(event, "data", {}) or {}
        context = getattr(event, "context", None)
        changed, removed = {}, []
        for identity in previous.keys() | current.keys():
            before, after = previous.get(identity), current.get(identity)
            if before == after:
                continue
            if after is None:
                removed.append(identity)
            else:
                changed[identity] = after
            payload = {
                "endpoint_id": identity,
                "previous": before,
                "phone": after,
                "removed": after is None,
            }
            phone_context = context
            if event_data.get("call_id"):
                candidates = (after or {}).get("calls", ()) or (before or {}).get("calls", ())
                selected = next((item for item in candidates
                                 if item["call_id"] == event_data["call_id"]),
                                candidates[0] if candidates else None)
                selected_id = selected["call_id"] if selected else event_data["call_id"]
                session = runtime.sip.get_session(selected_id)
                payload["call"] = (
                    self._call_fields(runtime.sip, session.committed_state)
                    if selected_id != event_data["call_id"] and session is not None
                    and session.committed_state is not None else dict(event_data)
                )
                phone_context = runtime.sip.ha_context(selected_id)
            self.hass.bus.async_fire(
                PHONE_STATE_CHANGED, deepcopy(payload), context=phone_context
            )
            self._deliver(self.listeners, payload, phone_context)
        if changed or removed:
            self._deliver(self.projection_listeners, PhoneProjectionUpdate(changed, tuple(removed)))
        if previous != current or previous_dialogs != dialogs:
            self._deliver(self.dialog_listeners, dialogs)
        if previous_activity != activity:
            self._deliver(self.activity_listeners, activity)

    async def close(self):
        removed = tuple(self.previous)
        self._stop()
        self._deliver(self.projection_listeners, PhoneProjectionUpdate({}, removed, closed=True))
        self.listeners.clear()
        self.activity_listeners.clear()
        self.dialog_listeners.clear()
        self.projection_listeners.clear()


def phone_state_observer(hass):
    runtime = require_runtime_data(hass)
    owner = runtime.sip
    if owner is None or not owner.active:
        raise ServiceValidationError("VoIP Stack is not ready")
    observer = owner.component("phone_state")
    if observer is None:
        observer = PhoneStateObserver(hass)
        owner.adopt_component("phone_state", observer, closer=observer.close)
    return observer
