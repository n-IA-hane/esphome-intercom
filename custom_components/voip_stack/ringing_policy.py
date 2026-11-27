"""Resolve configured ringing budgets without owning calls or protocol timers."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping


CONF_DEFAULT_RINGING_TIMEOUT = "default_ringing_timeout"
# Validation metadata is shared by policy readers and recovery guidance.
# These are bounds, never configured durations or runtime fallbacks.
RINGING_TIMEOUT_LIMITS = (0, 3600)
RING_STEP_TIMEOUT_MINIMUM = 0


def validate_ringing_timeout(value, *, inherit: bool = False) -> float | None:
    """Preserve the difference between inheritance, unlimited and finite values."""
    if value is None and inherit:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Ringing timeout must be a number of seconds")
    minimum, maximum = RINGING_TIMEOUT_LIMITS
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"Ringing timeout must be between {minimum} and {maximum} seconds; 0 is unlimited")
    return float(value)


def resolve_ringing_timeout(config: Mapping, *, account=None, override=None) -> float | None:
    """Resolve one attempt, returning None only for an unlimited deadline.

    None at a configuration boundary inherits; explicit zero overrides a
    positive parent. The runtime never invents a positive configured value.
    """
    value = override
    if value is None:
        value = getattr(getattr(account, "policy", None), "ringing_timeout", None)
    if value is None:
        value = config.get(CONF_DEFAULT_RINGING_TIMEOUT)
    if value is None:
        return None
    value = validate_ringing_timeout(value)
    return value if value > 0 else None


def cap_ringing_timeout(timeout: float | None, *caps: float | None) -> float | None:
    """Apply enclosing finite budgets without treating unlimited as zero."""
    finite = [value for value in (timeout, *caps) if value is not None]
    return min(finite) if finite else None


async def wait_for_ringing_result(client, timeout: float | None) -> str:
    """Bound application ringing while the existing SIP task owns cleanup."""
    if client.dialog is not None:
        return "in_call"
    try:
        async with asyncio.timeout(timeout):
            return await client.wait_for_final()
    except TimeoutError:
        # wait_for_final requests CANCEL when its waiter is cancelled. A final
        # accepted response wins even if its task has not resumed this waiter.
        if client.dialog is not None:
            return "in_call"
        client.request_cancel()
        return "no_answer"
