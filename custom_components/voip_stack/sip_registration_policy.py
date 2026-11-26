"""Configured SIP registration admission and static peer qualification policy.

The metadata is the single source for persisted migration values, native HA
forms and validation. Runtime construction reads every configured value rather
than silently substituting a different policy for an incomplete saved record.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Any


@dataclass(frozen=True, slots=True)
class IntegerPolicyField:
    default: int
    minimum: int
    maximum: int

    def validate(self, value: Any) -> int:
        if isinstance(value, bool):
            raise ValueError("Expected an integer")
        try:
            result = int(value)
            exact = result == float(value)
        except (TypeError, ValueError, OverflowError) as err:
            raise ValueError("Expected an integer") from err
        if not exact or not self.minimum <= result <= self.maximum:
            raise ValueError(f"Expected an integer between {self.minimum} and {self.maximum}")
        return result


REGISTRAR_POLICY_FIELDS = {
    "registrar_auth_failure_threshold": IntegerPolicyField(10, 0, 100),
    "registrar_auth_failure_window": IntegerPolicyField(60, 1, 86400),
    "registrar_auth_block_duration": IntegerPolicyField(300, 0, 604800),
    "registrar_min_expires": IntegerPolicyField(60, 1, 86400),
    "registrar_max_expires": IntegerPolicyField(86400, 1, 86400),
    "registrar_contact_limit": IntegerPolicyField(16, 1, 1024),
}
TRUNK_OPTIONS_INTERVAL = "trunk_options_interval"
TRUNK_OPTIONS_FIELD = IntegerPolicyField(30, 0, 86400)
AUDIO_RTCP_MIN_INTERVAL = "audio_rtcp_min_interval"
AUDIO_RTCP_INTERVAL_FIELD = IntegerPolicyField(5, 5, 86400)


def registrar_policy_defaults() -> dict[str, int]:
    return {name: field.default for name, field in REGISTRAR_POLICY_FIELDS.items()}


def validate_registrar_policy(data: Mapping[str, Any]) -> dict[str, int]:
    values = {name: field.validate(data[name]) for name, field in REGISTRAR_POLICY_FIELDS.items()}
    if values["registrar_min_expires"] > values["registrar_max_expires"]:
        raise ValueError("The registration minimum must not exceed its maximum")
    return values


@dataclass(frozen=True, slots=True)
class RegistrarPolicy:
    auth_failure_threshold: int = REGISTRAR_POLICY_FIELDS["registrar_auth_failure_threshold"].default
    auth_failure_window: int = REGISTRAR_POLICY_FIELDS["registrar_auth_failure_window"].default
    auth_block_duration: int = REGISTRAR_POLICY_FIELDS["registrar_auth_block_duration"].default
    min_expires: int = REGISTRAR_POLICY_FIELDS["registrar_min_expires"].default
    max_expires: int = REGISTRAR_POLICY_FIELDS["registrar_max_expires"].default
    contact_limit: int = REGISTRAR_POLICY_FIELDS["registrar_contact_limit"].default

    def __post_init__(self):
        validate_registrar_policy({"registrar_" + key: getattr(self, key)
            for key in self.__dataclass_fields__})

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> RegistrarPolicy:
        return cls(**{key.removeprefix("registrar_"): value
            for key, value in validate_registrar_policy(data).items()})

    @property
    def auth_block_enabled(self) -> bool:
        return bool(self.auth_failure_threshold and self.auth_block_duration)

LOCAL_REDIRECT_MAX_HOPS = "local_redirect_max_hops"
LOCAL_REDIRECT_HOPS_FIELD = IntegerPolicyField(5, 1, 32)
