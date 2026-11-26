"""Immutable queue policy shared by configuration and queue execution."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class QueuePolicy:
    """Queue behavior with durations supplied by its persisted configuration."""

    max_wait: float
    no_agent_wait: float
    retry_delay: float
    wrap_up: float
    enabled: bool = True
    max_waiting: int = 10
    ring_timeout: float | None = None
    fallback: str = ""
    hold_media: str = ""
    strategy: str = "ring_all"
    agent_order: tuple[str, ...] = ()
    auto_pause_after: int = 0
    fallback_full: str = ""
    fallback_timeout: str = ""
    fallback_no_agents: str = ""
    fallback_disabled: str = ""
    fallback_removed: str = ""

    def fallback_for(self, reason):
        return getattr(self, f"fallback_{reason.removeprefix('queue_')}", "") or self.fallback

    @classmethod
    def from_data(cls, data):
        values = dict(data or {})
        if set(values) - set(cls.__dataclass_fields__):
            raise ValueError("Unknown queue setting")
        required = {"max_wait", "no_agent_wait", "retry_delay", "wrap_up"}
        if missing := required - values.keys():
            raise ValueError(f"Missing configured queue durations: {', '.join(sorted(missing))}")
        order = values.get("agent_order", ())
        if not isinstance(order, (list, tuple)) or len(order) > 128 or any(
                not isinstance(item, str) or not item or len(item) > 256 for item in order):
            raise ValueError("agent_order must contain configured phone identities")
        values["agent_order"] = tuple(dict.fromkeys(order))
        policy = cls(**values)
        if policy.strategy not in {"ring_all", "round_robin", "longest_idle", "ordered"}:
            raise ValueError("Unknown queue strategy")
        if isinstance(policy.auto_pause_after, bool) or not isinstance(policy.auto_pause_after, int) or not 0 <= policy.auto_pause_after <= 100:
            raise ValueError("auto_pause_after must be 0 (disabled) or between 1 and 100")
        for name in ("fallback_full", "fallback_timeout", "fallback_no_agents", "fallback_disabled", "fallback_removed"):
            if not isinstance(getattr(policy, name), str) or len(getattr(policy, name)) > 256:
                raise ValueError(f"{name} must be a destination of at most 256 characters")
        if not isinstance(policy.enabled, bool):
            raise ValueError("enabled must be a boolean")
        if isinstance(policy.max_waiting, bool) or not isinstance(policy.max_waiting, int) or not 1 <= policy.max_waiting <= 64:
            raise ValueError("max_waiting must be between 1 and 64")
        for name, minimum, maximum in (
            ("max_wait", 1, 3600), ("ring_timeout", 0, 3600),
            ("no_agent_wait", 0, 120), ("retry_delay", 0.1, 120),
            ("wrap_up", 0, 3600),
        ):
            value = getattr(policy, name)
            if name == "ring_timeout" and value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= value <= maximum:
                raise ValueError(f"{name} must be between {minimum} and {maximum}")
        if not isinstance(policy.fallback, str) or len(policy.fallback) > 256:
            raise ValueError("fallback must be a destination of at most 256 characters")
        if not isinstance(policy.hold_media, str) or len(policy.hold_media) > 2048:
            raise ValueError("hold_media must be an audio URI of at most 2048 characters")
        return policy
