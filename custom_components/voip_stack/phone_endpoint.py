"""Pure logical phone endpoint model shared by every VoIP transport."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


DEFAULT_ENDPOINT_ID = "default"
class EndpointKind(StrEnum):
    """Supported logical endpoint implementations."""

    BROWSER = "browser"
    SIP_ACCOUNT = "sip_account"
    ESPHOME = "esphome"


class EndpointAvailability(StrEnum):
    """Transport-independent reachability of one endpoint."""

    AVAILABLE = "available"
    OFFLINE = "offline"
    UNAVAILABLE = "unavailable"


class OfflinePolicy(StrEnum):
    """Behavior when a call targets an endpoint that is not available."""

    UNAVAILABLE = "unavailable"
    FORWARD = "forward"


class EndpointValidationError(ValueError):
    """Raised when an endpoint definition is structurally invalid."""


def _required_text(value: object, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise EndpointValidationError(f"{field_name} must not be empty")
    return text


def _optional_text(value: object) -> str:
    return str(value or "").strip()


def _boolean(value: object, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    raise EndpointValidationError(f"{field_name} must be a boolean")


def _token_set(values: object, field_name: str) -> frozenset[str]:
    if values is None:
        return frozenset()
    if isinstance(values, str):
        iterable = (values,)
    else:
        try:
            iterable = iter(values)
        except TypeError as err:
            raise EndpointValidationError(f"{field_name} must be iterable") from err
    tokens = frozenset(
        text
        for item in iterable
        if (text := str(item or "").strip().casefold())
    )
    return tokens


def _entity_id_set(values: object) -> frozenset[str]:
    if values is None:
        return frozenset()
    if isinstance(values, str):
        iterable = (values,)
    else:
        try:
            iterable = iter(values)
        except TypeError as err:
            raise EndpointValidationError("entity_ids must be iterable") from err
    return frozenset(
        text for item in iterable if (text := str(item or "").strip())
    )


@dataclass(frozen=True, slots=True)
class PhoneEndpoint:
    """One immutable snapshot of a routable logical phone.

    ``endpoint_id`` is the transport-independent identity. Runtime and
    configuration changes replace the snapshot through ``EndpointRegistry``;
    callers can never mutate its identity in place.
    """

    endpoint_id: str
    name: str
    kind: EndpointKind
    extension: str = ""
    username: str = ""
    device_id: str = ""
    entity_ids: frozenset[str] = field(default_factory=frozenset)
    availability: EndpointAvailability = EndpointAvailability.OFFLINE
    capabilities: frozenset[str] = field(default_factory=frozenset)
    dnd: bool = False
    auto_answer: bool = False
    send_video: bool = False
    offline_policy: OfflinePolicy = OfflinePolicy.UNAVAILABLE
    ring_group: str = ""
    conference_group: str = ""
    conference_ring: bool = False
    offline_forward_target: str = ""
    active_call_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "endpoint_id", _required_text(self.endpoint_id, "endpoint_id")
        )
        object.__setattr__(self, "name", _required_text(self.name, "name"))
        try:
            object.__setattr__(self, "kind", EndpointKind(self.kind))
        except ValueError as err:
            raise EndpointValidationError(f"unsupported endpoint kind: {self.kind}") from err
        try:
            object.__setattr__(
                self, "availability", EndpointAvailability(self.availability)
            )
        except ValueError as err:
            raise EndpointValidationError(
                f"unsupported endpoint availability: {self.availability}"
            ) from err
        try:
            object.__setattr__(self, "offline_policy", OfflinePolicy(self.offline_policy))
        except ValueError as err:
            raise EndpointValidationError(
                f"unsupported offline policy: {self.offline_policy}"
            ) from err

        for field_name in (
            "extension",
            "username",
            "device_id",
            "ring_group",
            "conference_group",
            "offline_forward_target",
            "active_call_id",
        ):
            object.__setattr__(self, field_name, _optional_text(getattr(self, field_name)))
        object.__setattr__(self, "entity_ids", _entity_id_set(self.entity_ids))
        object.__setattr__(
            self, "capabilities", _token_set(self.capabilities, "capabilities")
        )
        object.__setattr__(self, "dnd", _boolean(self.dnd, "dnd"))
        object.__setattr__(
            self, "auto_answer", _boolean(self.auto_answer, "auto_answer")
        )
        object.__setattr__(
            self, "send_video", _boolean(self.send_video, "send_video")
        )
        object.__setattr__(
            self,
            "conference_ring",
            _boolean(self.conference_ring, "conference_ring"),
        )

        if (
            self.offline_policy is OfflinePolicy.FORWARD
            and not self.offline_forward_target
        ):
            raise EndpointValidationError(
                "offline_forward_target is required for the forward offline policy"
            )
        if self.kind is EndpointKind.BROWSER and (
            self.offline_policy is not OfflinePolicy.UNAVAILABLE
            or self.offline_forward_target
        ):
            raise EndpointValidationError(
                "browser phones do not use card-presence routing policies"
            )

    @property
    def route_aliases(self) -> tuple[str, ...]:
        """Return configured human-facing routing aliases without duplicates."""
        aliases: list[str] = []
        seen: set[str] = set()
        for value in (self.name, self.extension, self.username):
            key = value.casefold()
            if value and key not in seen:
                seen.add(key)
                aliases.append(value)
        return tuple(aliases)

    @property
    def sip_uri_user(self) -> str:
        """Return the public SIP user without exposing the opaque endpoint id."""
        if self.kind is EndpointKind.SIP_ACCOUNT:
            return self.username or self.extension or self.name
        return self.extension or self.name

    @property
    def has_active_call(self) -> bool:
        """Return whether this endpoint currently owns a logical call."""
        return bool(self.active_call_id)

    @property
    def is_available(self) -> bool:
        """Return whether this endpoint is currently reachable."""
        return self.availability is EndpointAvailability.AVAILABLE

    def supports(self, capability: object) -> bool:
        """Return whether a normalized capability is advertised."""
        return str(capability or "").strip().casefold() in self.capabilities
