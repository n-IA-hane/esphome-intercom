"""Dynamic entity manager shared by PhoneEndpoint platforms."""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import TYPE_CHECKING, Generic, TypeVar

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .endpoint_device import (
    async_ensure_endpoint_device,
    endpoint_config_subentry_id,
    is_managed_endpoint,
)

if TYPE_CHECKING:
    from .endpoint_registry import EndpointRegistry, EndpointRegistryEvent
    from .phone_endpoint import PhoneEndpoint

_LOGGER = logging.getLogger(__name__)

_EntityT = TypeVar("_EntityT", bound=Entity)


@callback
def register_endpoint_entity_manager(
    entry: ConfigEntry,
    bucket: dict,
    key: str,
    manager: "EndpointEntityManager",
) -> None:
    """Store a platform manager and remove it with a void unload callback."""
    bucket[key] = manager

    @callback
    def _remove_manager() -> None:
        bucket.pop(key, None)

    entry.async_on_unload(_remove_manager)


class EndpointEntityManager(Generic[_EntityT]):
    """Add and update one entity per integration-owned phone endpoint."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        async_add_entities: AddConfigEntryEntitiesCallback,
        factory: Callable[[HomeAssistant, PhoneEndpoint, EndpointRegistry], _EntityT],
        *,
        predicate: Callable[[PhoneEndpoint], bool] | None = None,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.async_add_entities = async_add_entities
        self.factory = factory
        self.predicate = predicate
        self.entities: dict[str, _EntityT] = {}
        self._creating_endpoint_ids: set[str] = set()
        self.registry: EndpointRegistry | None = hass.data.get(DOMAIN, {}).get(
            "endpoint_registry"
        )

    @callback
    def async_setup(self) -> None:
        """Install the registry listener and add the initial endpoint set."""
        if self.registry is None:
            _LOGGER.warning(
                "Phone endpoint registry is unavailable while setting up entities"
            )
            return
        new_entities: dict[str | None, list[_EntityT]] = {}
        for endpoint in self.registry.endpoints:
            entity = self._create(endpoint)
            if entity is not None:
                subentry_id = self._subentry_id(endpoint.endpoint_id)
                new_entities.setdefault(subentry_id, []).append(entity)
        self.entry.async_on_unload(self.registry.subscribe(self._on_registry_event))
        for subentry_id, entities in new_entities.items():
            self.async_add_entities(entities, config_subentry_id=subentry_id)

    @callback
    def _create(self, endpoint: PhoneEndpoint) -> _EntityT | None:
        if not is_managed_endpoint(endpoint):
            return None
        if self.predicate is not None and not self.predicate(endpoint):
            return None
        if endpoint.endpoint_id in self.entities:
            return None
        # Creating the HA Device writes its Device Registry ID back to the
        # immutable endpoint and therefore emits a synchronous UPDATED event.
        # Guard that re-entrant listener call so a dynamically registered phone
        # cannot create and add the same entity twice.
        if endpoint.endpoint_id in self._creating_endpoint_ids:
            return None
        self._creating_endpoint_ids.add(endpoint.endpoint_id)
        try:
            async_ensure_endpoint_device(
                self.hass, self.entry, endpoint, self.registry
            )
            # Ensuring the device may replace the immutable endpoint with one
            # that contains its registry ID. Always consume the latest object.
            endpoint = self.registry.get(endpoint.endpoint_id)
            entity = self.factory(self.hass, endpoint, self.registry)
            self.entities[endpoint.endpoint_id] = entity
            return entity
        finally:
            self._creating_endpoint_ids.discard(endpoint.endpoint_id)

    @callback
    def _on_registry_event(self, event: EndpointRegistryEvent) -> None:
        endpoint_id = event.endpoint.endpoint_id
        event_type = str(getattr(event.event_type, "value", event.event_type))
        if event_type == "removed" or not is_managed_endpoint(event.endpoint):
            entity = self.entities.pop(endpoint_id, None)
            if entity is not None and entity.hass is not None:
                self.hass.async_create_task(self._async_remove_entity(entity))
            return
        entity = self.entities.get(endpoint_id)
        if entity is None:
            entity = self._create(event.endpoint)
            if entity is not None:
                self.async_add_entities(
                    [entity],
                    config_subentry_id=self._subentry_id(endpoint_id),
                )
            return
        # Re-submit DeviceInfo only when device-facing configuration changes.
        # Runtime availability and call claims must not churn the HA Device
        # Registry. Consume the latest immutable snapshot in case ensuring the
        # device had to write its Device Registry ID back into the endpoint.
        previous = event.previous
        if (
            previous is None
            or not event.endpoint.device_id
            or previous.name != event.endpoint.name
            or previous.kind != event.endpoint.kind
        ):
            async_ensure_endpoint_device(
                self.hass, self.entry, event.endpoint, self.registry
            )
        endpoint = self.registry.get(endpoint_id) or event.endpoint
        apply_endpoint = getattr(entity, "apply_endpoint", None)
        if callable(apply_endpoint):
            apply_endpoint(endpoint)

    def _subentry_id(self, endpoint_id: str) -> str | None:
        return endpoint_config_subentry_id(self.hass, endpoint_id)

    async def _async_remove_entity(self, entity: _EntityT) -> None:
        """Remove runtime state and its now-orphaned Entity Registry entry."""
        entity_id = entity.entity_id
        await entity.async_remove(force_remove=True)
        if entity_id and er.async_get(self.hass).async_get(entity_id) is not None:
            er.async_get(self.hass).async_remove(entity_id)


def event_matches_endpoint(
    payload: dict[str, object],
    endpoint: PhoneEndpoint,
    registry: EndpointRegistry | None = None,
    *,
    owner_scoped: bool = False,
) -> bool:
    """Return whether a canonical call event involves ``endpoint``."""
    # HA softphone session events are emitted once per logical browser leg.
    # When the publisher supplies that owner explicitly, state/event entities
    # must consume only their own projection; source/destination fields merely
    # describe the shared call and must not make both entities apply both
    # states (calling then ringing, or vice versa).
    if owner_scoped:
        owner_endpoint_id = str(payload.get("endpoint_id") or "").strip()
        if owner_endpoint_id:
            return owner_endpoint_id.casefold() == endpoint.endpoint_id.casefold()
        owner_device_id = str(payload.get("device_id") or "").strip()
        if owner_device_id:
            device_id = str(getattr(endpoint, "device_id", "") or "")
            return bool(device_id and owner_device_id == device_id)

    endpoint_ids = {
        str(payload.get(key) or "").strip()
        for key in (
            "endpoint_id",
            "source_endpoint_id",
            "dest_endpoint_id",
            "target_endpoint_id",
        )
    }
    participant_endpoint_ids = payload.get("participant_endpoint_ids")
    if isinstance(participant_endpoint_ids, (list, tuple, set, frozenset)):
        endpoint_ids.update(
            str(value or "").strip() for value in participant_endpoint_ids
        )
    if endpoint.endpoint_id in endpoint_ids:
        return True

    device_id = str(getattr(endpoint, "device_id", "") or "")
    device_ids = {
        str(payload.get(key) or "").strip()
        for key in (
            "device_id",
            "source_device_id",
            "dest_device_id",
            "target_device_id",
            "session_device_id",
        )
    }
    participant_device_ids = payload.get("participant_device_ids")
    if isinstance(participant_device_ids, (list, tuple, set, frozenset)):
        device_ids.update(
            str(value or "").strip() for value in participant_device_ids
        )
    if device_id and device_id in device_ids:
        return True

    # Explicit endpoint/device metadata is authoritative. Do not let a caller
    # display name such as "Kitchen" make the Kitchen phone mirror a call that
    # explicitly belongs to another logical endpoint.
    if any(endpoint_ids) or any(device_ids):
        return False

    call_id = str(payload.get("call_id") or "").strip()
    dest_call_id = str(payload.get("dest_call_id") or "").strip()
    if endpoint.active_call_id and endpoint.active_call_id in {call_id, dest_call_id}:
        return True

    # Caller, callee and display-name fields are untrusted SIP text. They may
    # help users read a global event, but can never select a per-phone entity:
    # an external caller named "Kitchen" must not impersonate that endpoint.
    return False


def event_projects_endpoint_state(
    payload: dict[str, object],
    endpoint: PhoneEndpoint,
    registry: EndpointRegistry | None = None,
) -> bool:
    """Return whether ``payload`` is the authoritative state of one phone.

    Bridge, DTMF and routing events describe the call as a whole.  They may
    involve several endpoints, but must never become the durable state of any
    one phone; only the endpoint-scoped session projection may do that.
    """
    return str(payload.get("scope") or "") == "session" and event_matches_endpoint(
        payload,
        endpoint,
        registry,
        owner_scoped=True,
    )
