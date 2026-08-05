"""Shared authoritative lifetime lookup for browser media sessions."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant

from .phone_endpoint import DEFAULT_ENDPOINT_ID
from .runtime_data import call_projection
from .websocket_api import CALL_EVENT, _ha_softphone_store

if TYPE_CHECKING:
    from .pbx_runtime import SipEndpointRuntime


_MEDIA_CALL_STATES = frozenset({"connecting", "in_call"})


@dataclass(frozen=True, slots=True)
class ActiveMediaCall:
    """One endpoint's current media-bearing call and authoritative registry."""

    call_id: str
    store: dict[str, Any]
    registry: SipEndpointRuntime


def active_media_call(
    hass: HomeAssistant,
    endpoint_id: str = DEFAULT_ENDPOINT_ID,
) -> ActiveMediaCall | None:
    """Resolve a media-bearing call without manufacturing missing runtime state."""

    store = _ha_softphone_store(hass, endpoint_id)
    call_id = str(store.get("call_id") or "").strip()
    state = str(store.get("state") or "").strip().lower()
    if not call_id or state not in _MEDIA_CALL_STATES:
        return None
    registry = call_projection(hass)
    if registry is None or not hasattr(registry, "sessions"):
        return None
    return ActiveMediaCall(call_id, store, registry)


def listen_for_media_call_end(
    hass: HomeAssistant,
    call_id: str,
    endpoint_id: str = DEFAULT_ENDPOINT_ID,
) -> tuple[asyncio.Event, Any]:
    """Wake when one endpoint no longer projects the specified active call."""

    call_ended = asyncio.Event()

    def on_call_event(event: Any) -> None:
        payload = event.data
        if str(payload.get("call_id") or "") != call_id:
            return
        if str(payload.get("state") or "").lower() not in _MEDIA_CALL_STATES:
            call_ended.set()

    remove_listener = hass.bus.async_listen(CALL_EVENT, on_call_event)
    store = _ha_softphone_store(hass, endpoint_id)
    if (
        str(store.get("call_id") or "") != call_id
        or str(store.get("state") or "").lower() not in _MEDIA_CALL_STATES
    ):
        call_ended.set()
    return call_ended, remove_listener
