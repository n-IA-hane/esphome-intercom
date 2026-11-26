"""Initial queue durations used only while creating or migrating configuration."""

from collections.abc import Mapping

from .queue_policy import QueuePolicy

INITIAL_QUEUE_DURATIONS = {
    "max_wait": 60,
    "no_agent_wait": 1,
    "retry_delay": 1,
    "wrap_up": 0,
}


def initial_queue_policy(data=None) -> dict:
    """Copy a policy, materialize absent durations, and preserve saved choices."""
    if data is not None and not isinstance(data, Mapping):
        raise ValueError("Invalid queue policy configuration")
    result = dict(data or {})
    for key, value in INITIAL_QUEUE_DURATIONS.items():
        result.setdefault(key, value)
    QueuePolicy.from_data(result)
    return result
