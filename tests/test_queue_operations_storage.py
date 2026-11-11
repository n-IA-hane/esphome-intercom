"""Malformed operation storage cannot become unsafe runtime statistics."""

from types import SimpleNamespace

import pytest

from .test_groups import _load_module

queues = _load_module("call_queues")
endpoints = _load_module("endpoint_registry")
phones = _load_module("phone_endpoint")


def manager():
    registry = endpoints.EndpointRegistry()
    registry.register(phones.PhoneEndpoint(endpoint_id="op", name="Operator", kind="sip_account", availability="available"))
    result = queues.QueueManager(None, SimpleNamespace(runtime_data=SimpleNamespace(endpoints=registry)))
    result.reconcile([("op", ["Support"])])
    return result


@pytest.mark.parametrize("stored", [None, [], "bad", {"support": None}, {"support": {"op": None}}])
def test_stats_repairs_nonmapping_storage_before_read_or_increment(stored):
    subject = manager()
    subject.agent_stats = stored
    row = subject._stats("support", "op")
    assert row == {"consecutive_misses": 0, "pause_reason": "", "last_answered": None, "last_ended": None}
    row["consecutive_misses"] += 1
    assert subject._stats("support", "op")["consecutive_misses"] == 1
    assert subject.get_state("Support")["members"][0]["phone"] == "op"


def test_stats_drops_unknown_fields_and_nonfinite_or_malformed_values():
    subject = manager()
    subject.agent_stats = {"support": {"op": {"consecutive_misses": "2", "pause_reason": None,
        "last_answered": float("inf"), "last_ended": float("nan"), "unexpected": "never expose", "phone": "forged"}}}
    row = subject._stats("support", "op")
    assert row == {"consecutive_misses": 0, "pause_reason": "", "last_answered": None, "last_ended": None}
    public = subject.get_state("Support")["members"][0]
    assert public["phone"] == "op" and "unexpected" not in public


def test_operations_restore_preserves_valid_legacy_fields_and_pause_intent():
    loaded = {"agent_states": {"support": {"op": "paused"}},
        "agent_stats": {"support": {"op": {"consecutive_misses": 3, "pause_reason": "break",
            "last_answered": 123.5, "last_ended": 130}}},
        "round_robin": {"support": "op"}, "last_ended": {"op": 130}}
    restored = queues.normalize_queue_operations(loaded)
    assert restored == loaded
    legacy = queues.normalize_queue_operations({"agent_states": {"support": {"op": "logged_out"}}})
    assert legacy["agent_states"] == {"support": {"op": "logged_out"}}
    assert legacy["agent_stats"] == legacy["round_robin"] == legacy["last_ended"] == {}


def test_operations_restore_rejects_invalid_types_and_never_restores_nan():
    restored = queues.normalize_queue_operations({
        "agent_states": {"support": {"op": "unknown", "other": "available"}},
        "agent_stats": {"support": {"op": {"consecutive_misses": -1, "pause_reason": "x" * 300,
            "last_answered": True, "last_ended": -5}}},
        "round_robin": {"support": [], "valid": "op"},
        "last_ended": {"op": float("nan"), "negative": -1, "bool": True, "valid": 130}})
    assert restored["agent_states"]["support"] == {"op": "logged_out", "other": "available"}
    assert restored["agent_stats"]["support"]["op"] == {"consecutive_misses": 0,
        "pause_reason": "x" * 256, "last_answered": None, "last_ended": None}
    assert restored["round_robin"] == {"valid": "op"}
    assert restored["last_ended"] == {"valid": 130}
    assert queues.normalize_queue_operations(None) == {"agent_states": {}, "agent_stats": {}, "round_robin": {}, "last_ended": {}}
