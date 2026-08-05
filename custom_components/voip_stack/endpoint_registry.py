"""Authoritative in-memory registry for logical phone endpoints."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from enum import StrEnum
import logging

from .phone_endpoint import PhoneEndpoint
from .roster import normalize_roster_key


_LOGGER = logging.getLogger(__name__)


class EndpointRegistryError(ValueError):
    """Base class for endpoint registry failures."""


class EndpointNotFoundError(EndpointRegistryError):
    """Raised when a required endpoint does not exist."""

    def __init__(self, endpoint_id: object) -> None:
        self.endpoint_id = str(endpoint_id or "").strip()
        super().__init__(f"unknown endpoint: {self.endpoint_id or '<empty>'}")


class EndpointCollisionError(EndpointRegistryError):
    """Raised when a stable or routing identity is already owned."""

    def __init__(
        self,
        *,
        field: str,
        value: str,
        endpoint_id: str,
        conflicting_endpoint_id: str,
    ) -> None:
        self.field = field
        self.value = value
        self.endpoint_id = endpoint_id
        self.conflicting_endpoint_id = conflicting_endpoint_id
        super().__init__(
            f"endpoint {endpoint_id!r} {field} {value!r} conflicts with "
            f"endpoint {conflicting_endpoint_id!r}"
        )


class EndpointAmbiguousError(EndpointRegistryError):
    """Raised when an unqualified lookup matches different endpoints."""

    def __init__(self, value: str, endpoint_ids: tuple[str, ...]) -> None:
        self.value = value
        self.endpoint_ids = endpoint_ids
        super().__init__(
            f"ambiguous endpoint lookup {value!r}: {', '.join(endpoint_ids)}"
        )


class EndpointBusyError(EndpointRegistryError):
    """Raised when an endpoint already owns another active call."""

    def __init__(self, endpoint: PhoneEndpoint, requested_call_id: str) -> None:
        self.endpoint_id = endpoint.endpoint_id
        self.active_call_id = endpoint.active_call_id
        self.requested_call_id = requested_call_id
        super().__init__(
            f"endpoint {endpoint.endpoint_id!r} is busy with call "
            f"{endpoint.active_call_id!r}"
        )


class EndpointLookup(StrEnum):
    """Qualified endpoint lookup namespaces."""

    ENDPOINT_ID = "endpoint_id"
    DEVICE_ID = "device_id"
    ENTITY_ID = "entity_id"
    NAME = "name"
    EXTENSION = "extension"
    USERNAME = "username"


class EndpointRegistryEventType(StrEnum):
    """Mutations observable by HA entity and routing adapters."""

    REGISTERED = "registered"
    UPDATED = "updated"
    REMOVED = "removed"


@dataclass(frozen=True, slots=True)
class EndpointRegistryEvent:
    """One immutable endpoint registry notification."""

    event_type: EndpointRegistryEventType
    endpoint: PhoneEndpoint
    previous: PhoneEndpoint | None = None


EndpointRegistryListener = Callable[[EndpointRegistryEvent], None]


def _key(value: object) -> str:
    return str(value or "").strip().casefold()


class EndpointRegistry:
    """Validated lookup index for every logical phone implementation."""

    def __init__(self) -> None:
        self._endpoints: dict[str, PhoneEndpoint] = {}
        self._device_index: dict[str, str] = {}
        self._entity_index: dict[str, str] = {}
        self._name_index: dict[str, str] = {}
        self._extension_index: dict[str, str] = {}
        self._username_index: dict[str, str] = {}
        self._route_index: dict[str, str] = {}
        self._listeners: set[EndpointRegistryListener] = set()
        self.subentry_ids: dict[str, str] = {}
        self.pending_removals: set[str] = set()

    @property
    def endpoints(self) -> tuple[PhoneEndpoint, ...]:
        """Return a stable registration-order snapshot."""
        return tuple(self._endpoints.values())

    def __len__(self) -> int:
        return len(self._endpoints)

    def __iter__(self) -> Iterator[PhoneEndpoint]:
        return iter(self.endpoints)

    def subscribe(self, listener: EndpointRegistryListener) -> Callable[[], None]:
        """Subscribe to mutations and return an idempotent unsubscribe callback."""
        self._listeners.add(listener)

        def unsubscribe() -> None:
            self._listeners.discard(listener)

        return unsubscribe

    def close(self) -> None:
        """Release every listener owned by this registry."""
        self._listeners.clear()

    def register(self, endpoint: PhoneEndpoint) -> PhoneEndpoint:
        """Register a new endpoint, rejecting every identity collision."""
        endpoint_key = _key(endpoint.endpoint_id)
        existing = self._endpoints.get(endpoint_key)
        if existing is not None:
            raise EndpointCollisionError(
                field="endpoint_id",
                value=endpoint.endpoint_id,
                endpoint_id=endpoint.endpoint_id,
                conflicting_endpoint_id=existing.endpoint_id,
            )
        self.validate(endpoint)
        self._endpoints[endpoint_key] = endpoint
        self._rebuild_indexes()
        self._emit(
            EndpointRegistryEvent(
                EndpointRegistryEventType.REGISTERED,
                endpoint,
            )
        )
        return endpoint

    def upsert(self, endpoint: PhoneEndpoint) -> PhoneEndpoint:
        """Register or atomically replace one endpoint snapshot."""
        endpoint_key = _key(endpoint.endpoint_id)
        previous = self._endpoints.get(endpoint_key)
        if previous is not None and previous.endpoint_id != endpoint.endpoint_id:
            raise EndpointRegistryError("endpoint_id is immutable")
        if (
            previous is not None
            and previous.active_call_id
            and endpoint.active_call_id != previous.active_call_id
        ):
            raise EndpointBusyError(endpoint=previous, requested_call_id=endpoint.active_call_id)
        self.validate(
            endpoint,
            replacing_endpoint_id=previous.endpoint_id if previous is not None else None,
        )
        if previous == endpoint:
            return previous
        self._endpoints[endpoint_key] = endpoint
        self._rebuild_indexes()
        self._emit(
            EndpointRegistryEvent(
                EndpointRegistryEventType.UPDATED
                if previous is not None
                else EndpointRegistryEventType.REGISTERED,
                endpoint,
                previous,
            )
        )
        return endpoint

    def update(self, endpoint_id: object, /, **changes: object) -> PhoneEndpoint:
        """Replace selected fields while preserving the endpoint identity."""
        if "endpoint_id" in changes:
            raise EndpointRegistryError("endpoint_id is immutable")
        if "active_call_id" in changes:
            raise EndpointRegistryError(
                "active_call_id must be changed with claim_call or release_call"
            )
        previous = self.require(endpoint_id)
        return self.upsert(replace(previous, **changes))

    def remove(self, endpoint_id: object, *, force: bool = False) -> PhoneEndpoint:
        """Remove an idle endpoint and all of its lookup aliases."""
        endpoint = self.require(endpoint_id)
        if endpoint.active_call_id and not force:
            raise EndpointBusyError(endpoint, "")
        del self._endpoints[_key(endpoint.endpoint_id)]
        self._rebuild_indexes()
        self._emit(
            EndpointRegistryEvent(
                EndpointRegistryEventType.REMOVED,
                endpoint,
                endpoint,
            )
        )
        return endpoint

    def get(self, endpoint_id: object) -> PhoneEndpoint | None:
        """Return an endpoint by stable identity."""
        return self._endpoints.get(_key(endpoint_id))

    def require(self, endpoint_id: object) -> PhoneEndpoint:
        """Return an endpoint by identity or raise a typed error."""
        endpoint = self.get(endpoint_id)
        if endpoint is None:
            raise EndpointNotFoundError(endpoint_id)
        return endpoint

    def by_device_id(self, device_id: object) -> PhoneEndpoint | None:
        return self._from_index(self._device_index, device_id)

    def by_entity_id(self, entity_id: object) -> PhoneEndpoint | None:
        return self._from_index(self._entity_index, entity_id)

    def by_name(self, name: object) -> PhoneEndpoint | None:
        return self._from_index(self._name_index, name)

    def by_extension(self, extension: object) -> PhoneEndpoint | None:
        return self._from_index(self._extension_index, extension)

    def by_username(self, username: object) -> PhoneEndpoint | None:
        return self._from_index(self._username_index, username)

    def resolve(
        self,
        value: object,
        *,
        namespace: EndpointLookup | str | None = None,
    ) -> PhoneEndpoint | None:
        """Resolve one qualified identifier or an unqualified unique match."""
        if namespace is not None:
            try:
                lookup = EndpointLookup(namespace)
            except ValueError as err:
                raise EndpointRegistryError(
                    f"unsupported endpoint lookup namespace: {namespace}"
                ) from err
            return {
                EndpointLookup.ENDPOINT_ID: self.get,
                EndpointLookup.DEVICE_ID: self.by_device_id,
                EndpointLookup.ENTITY_ID: self.by_entity_id,
                EndpointLookup.NAME: self.by_name,
                EndpointLookup.EXTENSION: self.by_extension,
                EndpointLookup.USERNAME: self.by_username,
            }[lookup](value)

        matches = {
            endpoint.endpoint_id: endpoint
            for endpoint in (
                self.get(value),
                self.by_device_id(value),
                self.by_entity_id(value),
                self.by_name(value),
                self.by_extension(value),
                self.by_username(value),
            )
            if endpoint is not None
        }
        if len(matches) > 1:
            raise EndpointAmbiguousError(
                str(value or "").strip(), tuple(sorted(matches))
            )
        return next(iter(matches.values()), None)

    def claim_call(self, endpoint_id: object, call_id: object) -> PhoneEndpoint:
        """Atomically claim the endpoint for one call; repeated claims are safe."""
        requested_call_id = str(call_id or "").strip()
        if not requested_call_id:
            raise EndpointRegistryError("call_id must not be empty")
        endpoint = self.require(endpoint_id)
        if endpoint.active_call_id:
            if endpoint.active_call_id == requested_call_id:
                return endpoint
            raise EndpointBusyError(endpoint, requested_call_id)
        return self._replace_runtime(
            endpoint,
            active_call_id=requested_call_id,
        )

    def release_call(self, endpoint_id: object, call_id: object) -> bool:
        """Release only the matching call so stale callbacks cannot clear a claim."""
        requested_call_id = str(call_id or "").strip()
        endpoint = self.require(endpoint_id)
        if not endpoint.active_call_id or endpoint.active_call_id != requested_call_id:
            return False
        self._replace_runtime(endpoint, active_call_id="")
        return True

    def sync_transport_call(
        self,
        endpoint_id: object,
        *,
        active: bool,
        fallback_call_id: object = "",
    ) -> PhoneEndpoint:
        """Mirror an endpoint-owned call state without replacing HA ownership.

        Physical phones often expose only a call-state entity, not the SIP
        Call-ID.  If HA already owns the routed dialog, keep that exact claim;
        otherwise use the stable transport token supplied by the adapter.
        A terminal physical state is authoritative and releases whichever call
        the endpoint currently owns.
        """
        endpoint = self.require(endpoint_id)
        if active:
            call_id = endpoint.active_call_id or str(fallback_call_id or "").strip()
            if not call_id:
                raise EndpointRegistryError(
                    "fallback_call_id must not be empty for an unclaimed endpoint"
                )
            return self.claim_call(endpoint.endpoint_id, call_id)
        if endpoint.active_call_id:
            self.release_call(endpoint.endpoint_id, endpoint.active_call_id)
        return self.require(endpoint.endpoint_id)

    def adopt_transport_call(
        self,
        endpoint_id: object,
        call_id: object,
        *,
        transport_prefix: str = "physical:",
    ) -> PhoneEndpoint:
        """Replace only a provisional physical-state token with a SIP Call-ID."""
        requested_call_id = str(call_id or "").strip()
        if not requested_call_id:
            raise EndpointRegistryError("call_id must not be empty")
        endpoint = self.require(endpoint_id)
        if not endpoint.active_call_id:
            return self.claim_call(endpoint.endpoint_id, requested_call_id)
        if endpoint.active_call_id == requested_call_id:
            return endpoint
        if endpoint.active_call_id.startswith(str(transport_prefix or "physical:")):
            return self._replace_runtime(
                endpoint,
                active_call_id=requested_call_id,
            )
        raise EndpointBusyError(endpoint, requested_call_id)

    def validate(
        self,
        endpoint: PhoneEndpoint,
        *,
        replacing_endpoint_id: str | None = None,
    ) -> None:
        """Validate candidate identities without mutating the registry."""
        replacing_key = _key(replacing_endpoint_id)
        endpoint_key = _key(endpoint.endpoint_id)
        existing = self._endpoints.get(endpoint_key)
        if existing is not None and endpoint_key != replacing_key:
            self._raise_collision(endpoint, "endpoint_id", endpoint.endpoint_id, existing)

        self._validate_index_value(
            endpoint, "device_id", endpoint.device_id, self._device_index, replacing_key
        )
        for entity_id in endpoint.entity_ids:
            self._validate_index_value(
                endpoint, "entity_id", entity_id, self._entity_index, replacing_key
            )
        for field_name, value in (
            ("name", endpoint.name),
            ("extension", endpoint.extension),
            ("username", endpoint.username),
        ):
            value_key = normalize_roster_key(value)
            if not value_key:
                continue
            owner_key = self._route_index.get(value_key)
            if owner_key is not None and owner_key != replacing_key:
                self._raise_collision(
                    endpoint,
                    field_name,
                    value,
                    self._endpoints[owner_key],
                )

    def _validate_index_value(
        self,
        endpoint: PhoneEndpoint,
        field_name: str,
        value: str,
        index: dict[str, str],
        replacing_key: str,
    ) -> None:
        value_key = _key(value)
        if not value_key:
            return
        owner_key = index.get(value_key)
        if owner_key is None or owner_key == replacing_key:
            return
        owner = self._endpoints[owner_key]
        self._raise_collision(endpoint, field_name, value, owner)

    @staticmethod
    def _raise_collision(
        endpoint: PhoneEndpoint,
        field_name: str,
        value: str,
        owner: PhoneEndpoint,
    ) -> None:
        raise EndpointCollisionError(
            field=field_name,
            value=value,
            endpoint_id=endpoint.endpoint_id,
            conflicting_endpoint_id=owner.endpoint_id,
        )

    def _replace_runtime(
        self, endpoint: PhoneEndpoint, **changes: object
    ) -> PhoneEndpoint:
        updated = replace(endpoint, **changes)
        self._endpoints[_key(endpoint.endpoint_id)] = updated
        self._rebuild_indexes()
        self._emit(
            EndpointRegistryEvent(
                EndpointRegistryEventType.UPDATED,
                updated,
                endpoint,
            )
        )
        return updated

    def _from_index(
        self, index: dict[str, str], value: object
    ) -> PhoneEndpoint | None:
        endpoint_key = index.get(_key(value))
        return self._endpoints.get(endpoint_key) if endpoint_key is not None else None

    def _rebuild_indexes(self) -> None:
        self._device_index.clear()
        self._entity_index.clear()
        self._name_index.clear()
        self._extension_index.clear()
        self._username_index.clear()
        self._route_index.clear()
        for endpoint_key, endpoint in self._endpoints.items():
            self._add_index(self._device_index, endpoint.device_id, endpoint_key)
            for entity_id in endpoint.entity_ids:
                self._add_index(self._entity_index, entity_id, endpoint_key)
            self._add_index(self._name_index, endpoint.name, endpoint_key)
            self._add_index(self._extension_index, endpoint.extension, endpoint_key)
            self._add_index(self._username_index, endpoint.username, endpoint_key)
            for alias in endpoint.route_aliases:
                route_key = normalize_roster_key(alias)
                if route_key:
                    self._route_index[route_key] = endpoint_key

    @staticmethod
    def _add_index(index: dict[str, str], value: str, endpoint_key: str) -> None:
        value_key = _key(value)
        if value_key:
            index[value_key] = endpoint_key

    def _emit(self, event: EndpointRegistryEvent) -> None:
        for listener in tuple(self._listeners):
            try:
                listener(event)
            except Exception:  # pragma: no cover - defensive adapter isolation
                _LOGGER.exception("Phone endpoint registry listener failed")
