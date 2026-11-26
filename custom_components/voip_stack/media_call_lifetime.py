"""Resolve browser media eligibility from the softphone projection."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback

from .runtime_data import call_projection
from .const import CALL_EVENT
from .browser_phone_data import _ha_softphone_store

if TYPE_CHECKING:
    from .pbx_runtime import SipEndpointRuntime


_MEDIA_CALL_STATES = frozenset({"connecting", "in_call"})


@dataclass(frozen=True, slots=True)
class ActiveMediaCall:
    """One endpoint's projected media-bearing call and PBX registry."""

    call_id: str
    store: dict[str, Any]
    registry: SipEndpointRuntime


def selected_browser_dialog(registry, call_id):
    """Use the confirmed dialog or the selected provisional branch of this owner."""
    session = registry.get_session(registry.resolve_session_id(call_id))
    client = registry.sip_client_for(call_id)
    if session is None or not session.live or client is None:
        return None
    if client.dialog is not None:
        return client.dialog
    tag = session.metadata.get("browser_early_media_tag")
    return client.early_dialogs.get(tag) if tag else None


def _media_state_active(store):
    state = str(store.get("state") or "").strip().lower()
    return state in _MEDIA_CALL_STATES or (
        state in {"calling", "remote_ringing"} and store.get("early_media") is True
        and store.get("direction") == "outgoing"
    )


def active_media_call(
    hass: HomeAssistant,
    endpoint_id: str,
) -> ActiveMediaCall | None:
    """Resolve a media-bearing call without manufacturing missing runtime state."""

    store = _ha_softphone_store(hass, endpoint_id)
    call_id = str(store.get("call_id") or "").strip()
    if not call_id or not _media_state_active(store):
        return None
    registry = call_projection(hass)
    if registry is None or not hasattr(registry, "sessions"):
        return None
    return ActiveMediaCall(call_id, store, registry)


def listen_for_media_call_end(
    hass: HomeAssistant,
    call_id: str,
    endpoint_id: str,
) -> tuple[asyncio.Event, Any]:
    """Wake when one endpoint no longer projects the specified active call."""

    call_ended = asyncio.Event()

    @callback
    def on_call_event(event: Any) -> None:
        payload = event.data
        if str(payload.get("call_id") or "") != call_id:
            return
        event_endpoint = str(payload.get("endpoint_id") or "").strip()
        if event_endpoint and event_endpoint != endpoint_id:
            return
        if not event_endpoint and str(payload.get("state") or "").lower() in {
            "calling", "ringing", "remote_ringing"
        }:
            # Aggregate/group events do not describe this phone's media leg.
            current = _ha_softphone_store(hass, endpoint_id)
            if (
                str(current.get("call_id") or "") == call_id
                and _media_state_active(current)
            ):
                return
        current = _ha_softphone_store(hass, endpoint_id)
        terminal = str(payload.get("state") or "").lower() in {
            "idle", "unavailable", "terminating", "ended", "failed", "error", "busy", "cancelled"
        }
        if terminal or str(current.get("call_id") or "") != call_id or not _media_state_active(current):
            call_ended.set()

    remove_listener = hass.bus.async_listen(CALL_EVENT, on_call_event)
    store = _ha_softphone_store(hass, endpoint_id)
    if (
        str(store.get("call_id") or "") != call_id
        or not _media_state_active(store)
    ):
        call_ended.set()
    return call_ended, remove_listener
