"""Passive, endpoint-authorized WebSocket views of the shared phone projection."""

from homeassistant.auth import EVENT_USER_REMOVED, EVENT_USER_UPDATED
from homeassistant.core import callback
from homeassistant.exceptions import Unauthorized

from .authorization import require_websocket_endpoint_read
from .phone_state import phone_state_dto, phone_state_observer


class PhoneStateSubscription:
    """Stream a snapshot and changed DTOs, retaining no routing/media ownership.

    Events are ``{kind: 'snapshot', revision: 1, phones: [...]}``, followed by
    ``{kind: 'delta', revision: 2, phones: [...], removed: [...]}``. Revision is
    local to this subscription. Reconnecting starts with a new full snapshot.
    """

    def __init__(self, hass, connection, msg_id):
        self.hass = hass
        self.connection = connection
        self.msg_id = msg_id
        self.revision = 0
        self.sent = {}
        self.closed = False
        self.revoked = False
        self.observer = None
        self.unsubscribe = None

    def _user_active(self):
        user = self.connection.user
        return bool(
            not self.revoked and user is not None and getattr(user, "id", "")
            and getattr(user, "is_active", True)
        )

    def _readable(self, endpoint_id):
        if not self._user_active():
            return False
        endpoint = self.hass_runtime.endpoints.get(endpoint_id)
        if endpoint is None:
            return False
        try:
            require_websocket_endpoint_read(self.hass, self.connection, endpoint)
        except Unauthorized:
            return False
        return True

    @callback
    def start(self):
        from .runtime_data import require_runtime_data

        if not self._user_active():
            raise Unauthorized(permission="read", user_id=getattr(self.connection.user, "id", None))
        self.hass_runtime = require_runtime_data(self.hass)
        self.observer = phone_state_observer(self.hass)
        self.unsubscribe = self.observer.subscribe(self._changed, kind="projection")
        self.connection.subscriptions[self.msg_id] = self.close
        try:
            self.connection.send_result(self.msg_id)
            self._publish(snapshot=True)
        except Exception:
            self.close()
            raise

    @callback
    def _changed(self, update):
        if self.closed:
            return
        event = update.access_event
        if event is not None and event.event_type in (EVENT_USER_UPDATED, EVENT_USER_REMOVED):
            if event.data.get("user_id") != self.connection.user.id:
                return
            if event.event_type == EVENT_USER_REMOVED:
                self.revoked = True
        try:
            self._publish(update=update)
        except Exception:
            self.close()
            raise
        if update.closed or not self._user_active():
            self.close()

    def _publish(self, *, snapshot=False, update=None):
        # Reuse the single observer's cached state. Per-user filtering never
        # recomputes dialogs, opens a media owner or changes browser presence.
        permissions = {}

        def readable(identity):
            if identity not in permissions:
                permissions[identity] = self._readable(identity)
            return permissions[identity]

        if snapshot or update.access_event is not None or update.closed:
            current = {}
            changed_phones = self.observer.previous
        else:
            current = dict(self.sent)
            changed_phones = dict(update.phones)
            for identity in update.removed:
                current.pop(identity, None)
            # A deleted peer loses its endpoint link while its already visible
            # caller label remains useful on the surviving conversation.
            if update.removed:
                deleted = set(update.removed)
                changed_phones.update(
                    (identity, self.observer.previous[identity])
                    for identity, phone in self.sent.items()
                    if identity in self.observer.previous and any(
                        call["peer_endpoint_id"] in deleted for call in phone["calls"]
                    )
                )
        for identity, phone in changed_phones.items():
            if readable(identity):
                current[identity] = phone_state_dto(phone, peer_readable=readable)
            else:
                current.pop(identity, None)
        changed = [phone for identity, phone in current.items() if self.sent.get(identity) != phone]
        removed = sorted(self.sent.keys() - current.keys())
        if not snapshot and not (changed or removed):
            return
        self.revision += 1
        self.sent = current
        payload = {
            "kind": "snapshot" if snapshot else "delta",
            "revision": self.revision,
            "phones": list(current.values()) if snapshot else changed,
        }
        if not snapshot:
            payload["removed"] = removed
        self.connection.send_event(self.msg_id, payload)

    @callback
    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.unsubscribe is not None:
            self.unsubscribe()
            self.unsubscribe = None
        # HA owns this dictionary and may currently be iterating values() in
        # ActiveConnection.async_handle_close. Only release our own resources.
        self.sent.clear()
        self.observer = None
        self.hass_runtime = None
