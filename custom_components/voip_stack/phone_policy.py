"""Immutable, persisted phone policies, independent of call execution.

Policies describe defaults for future calls. They do not own timers, media or
automation controllers; those remain resources of the existing call session.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
import math


class DestinationKind(StrEnum):
    CONTACT = "contact"
    VOICEMAIL = "voicemail"
    HANGUP = "hangup"


@dataclass(frozen=True, slots=True)
class PhoneDestination:
    """A resolver target, a stable mailbox endpoint, or explicit termination."""

    kind: DestinationKind
    target: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", DestinationKind(self.kind))
        if not isinstance(self.target, str):
            raise ValueError("destination target must be text")
        object.__setattr__(self, "target", self.target.strip())
        if self.kind is DestinationKind.HANGUP:
            if self.target:
                raise ValueError("hangup cannot have a destination")
        elif not self.target:
            raise ValueError("a destination is required")

    @classmethod
    def from_data(cls, data: Mapping) -> PhoneDestination:
        if not isinstance(data, Mapping) or set(data) - {"kind", "target"}:
            raise ValueError("invalid phone destination")
        return cls(kind=data.get("kind", "contact"), target=data.get("target", ""))

    def as_data(self) -> dict:
        return {"kind": self.kind.value, "target": self.target}


@dataclass(frozen=True, slots=True)
class PhonePolicy:
    """Missing limits inherit; zero is unlimited; fallbacks are independent."""

    always: PhoneDestination | None = None
    busy: PhoneDestination | None = None
    unavailable: PhoneDestination | None = None
    no_answer: PhoneDestination | None = None
    ringing_timeout: float | None = None
    outbound_trunk_id: str = ""
    outbound_caller_id: str = ""
    outbound_caller_name: str = ""

    def __post_init__(self) -> None:
        for name in ("always", "busy", "unavailable", "no_answer"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, PhoneDestination):
                raise ValueError(f"{name} must be a phone destination")
        timeout = self.ringing_timeout
        if timeout is not None:
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
                raise ValueError("ringing_timeout must be seconds or null")
            if not math.isfinite(timeout) or not 0 <= timeout <= 3600:
                raise ValueError("ringing_timeout must be between 0 and 3600 seconds")
        if not isinstance(self.outbound_trunk_id, str):
            raise ValueError("outbound_trunk_id must be text")
        object.__setattr__(self, "outbound_trunk_id", self.outbound_trunk_id.strip())
        from .outbound_identity import identity_text

        object.__setattr__(self, "outbound_caller_id", identity_text(
            self.outbound_caller_id, "outbound caller ID", user=True))
        object.__setattr__(self, "outbound_caller_name", identity_text(
            self.outbound_caller_name, "outbound caller name"))

    @classmethod
    def from_data(cls, data: Mapping | None) -> PhonePolicy:
        if data is None:
            return cls()
        allowed = {"always", "busy", "unavailable", "no_answer", "ringing_timeout", "outbound_trunk_id",
                   "outbound_caller_id", "outbound_caller_name"}
        if not isinstance(data, Mapping) or set(data) - allowed:
            raise ValueError("invalid phone policy fields")
        values = dict(data)
        for name in ("always", "busy", "unavailable", "no_answer"):
            if values.get(name) is not None:
                values[name] = PhoneDestination.from_data(values[name])
        return cls(**values)

    def as_data(self) -> dict:
        values = {
            name: value.as_data()
            for name in ("always", "busy", "unavailable", "no_answer")
            if (value := getattr(self, name)) is not None
        }
        if self.ringing_timeout is not None:
            values["ringing_timeout"] = self.ringing_timeout
        if self.outbound_trunk_id:
            values["outbound_trunk_id"] = self.outbound_trunk_id
        for key in ("outbound_caller_id", "outbound_caller_name"):
            if value := getattr(self, key):
                values[key] = value
        return values

    @property
    def requires_ha_routing(self) -> bool:
        return any((self.always, self.busy, self.unavailable, self.no_answer))

    def before_ringing(
        self, *, busy: bool, unavailable: bool, group_member: bool = False
    ) -> PhoneDestination | None:
        """Select one default; automation and authorization precede this step.

        Group owners select their own fallback. A member's mailbox must not
        answer automatically while other members are still ringing.
        """
        if group_member:
            return None
        if self.always is not None:
            return self.always
        if busy:
            return self.busy
        if unavailable:
            return self.unavailable
        return None
