"""Derived membership, persistent intent, FIFO and cross-queue admission."""

from types import SimpleNamespace

import pytest

from .test_groups import _load_module

queues = _load_module("call_queues")
phones = _load_module("phone_endpoint")
registry = _load_module("endpoint_registry")


@pytest.fixture
def manager():
    endpoints = registry.EndpointRegistry()
    for index in (1, 2):
        endpoints.register(phones.PhoneEndpoint(endpoint_id=f"sip:{index}", name=f"Phone {index}",
            username=str(index), kind=phones.EndpointKind.SIP_ACCOUNT,
            availability=phones.EndpointAvailability.AVAILABLE))
    now = [100.0]
    entry = SimpleNamespace(runtime_data=SimpleNamespace(endpoints=endpoints))
    manager = queues.QueueManager(None, entry, clock=lambda: now[0])
    manager.test_clock = now
    manager.reconcile([("sip:1", ("1002",)), ("sip:2", ("1002", "Support"))])
    return manager


def test_declarations_create_and_last_removal_deletes_without_creation_step(manager):
    assert manager.resolve("1002").members == ("sip:1", "sip:2")
    assert manager.resolve("service:queue:support").members == ("sip:2",)
    assert {row.extension for row in manager.roster_entries()} == {"1002", ""}
    manager.reconcile([("sip:2", ("1002",))])
    assert manager.resolve("Support") is None
    assert manager.resolve("1002").members == ("sip:2",)
    manager.reconcile([])
    assert not manager.definitions
    assert not manager.agent_states and not manager.policies
    with pytest.raises(ValueError, match="declare"):
        manager.configure("not-declared", {"max_wait": 30})


def test_logged_out_paused_and_offline_never_delete_membership(manager):
    manager.set_agent_state("1002", "sip:1", "logged_out")
    manager.set_agent_state("1002", "sip:2", "paused")
    manager.endpoints.update("sip:1", availability=phones.EndpointAvailability.OFFLINE)
    manager.reconcile([("sip:1", ("1002",)), ("sip:2", ("1002", "Support"))])
    assert manager.resolve("1002").members == ("sip:1", "sip:2")
    assert manager.eligible("1002") == ()
    assert manager.phone_summary("sip:1")[0]["state"] == "logged_out"
    assert manager.phone_summary("sip:2")[0]["logged_in"]
    assert manager.phone_summary("sip:2")[0]["paused"]


def test_new_pairs_start_logged_in_and_existing_intent_survives_republication(manager):
    manager.set_agent_state("1002", "sip:1", "logged_out")
    manager.reconcile([("sip:1", ("1002", "Support")), ("sip:2", ("1002", "Support"))])
    assert manager.agent_states["1002"]["sip:1"] == "logged_out"
    assert manager.agent_states["support"]["sip:1"] == "available"


def test_restart_restores_policy_and_agent_intent_without_creating_stale_queues(manager):
    manager.configure("1002", {"max_wait": 20, "wrap_up": 10})
    manager.set_agent_state("1002", "sip:1", "paused")
    saved = manager._snapshot()
    fresh = queues.QueueManager(None, manager.entry)
    fresh.policies = {key: queues.QueuePolicy.from_data(value) for key, value in saved["policies"].items()}
    fresh.agent_states = saved["agent_states"]
    fresh.reconcile([("sip:1", ("1002",)), ("sip:2", ("1002",))])
    assert fresh.policy("1002").max_wait == 20
    assert fresh.agent_states["1002"]["sip:1"] == "paused"
    assert fresh.resolve("Support") is None


def test_fifo_and_shared_agent_reservations(manager):
    first = manager.enqueue("1002", "call-1", 1)
    second = manager.enqueue("1002", "call-2", 2)
    other = manager.enqueue("Support", "call-3", 3)
    assert manager.claim_turn(second) == ()
    assert manager.claim_turn(first) == ("sip:1", "sip:2")
    assert manager.claim_turn(other) == ()
    manager.finish(first)
    assert manager.claim_turn(other) == ("sip:2",)
    assert manager.claim_turn(second) == ("sip:1",)
    manager.finish(second)
    manager.finish(other)
    assert not manager.waiting and not manager.reservations


def test_no_admission_after_deadline_and_capacity_is_bounded(manager):
    manager.configure("1002", {"max_waiting": 1, "max_wait": 20})
    ticket = manager.enqueue("1002", "call-1", 1)
    with pytest.raises(ValueError, match="full"):
        manager.enqueue("1002", "call-2", 2)
    manager.test_clock[0] += 20
    assert manager.claim_turn(ticket) == ()
    manager.finish(ticket)
    assert manager.enqueue("1002", "call-2", 2).deadline == 140


def test_removed_queue_wakes_waiters_and_releases_without_ending_connected_calls(manager):
    ticket = manager.enqueue("1002", "call-1", 1)
    assert manager.claim_turn(ticket)
    ticket.changed.clear()
    manager.reconcile([])
    assert ticket.changed.is_set()
    assert not manager.claim_turn(queues.QueueTicket("1002", "other", 2, 100, 120))
    manager.finish(ticket)
    manager.finish(ticket)
    assert not manager.reservations and not manager.waiting


@pytest.mark.parametrize("field,value", [
    ("max_waiting", 0), ("max_waiting", 65), ("max_waiting", True),
    ("max_wait", 0), ("ring_timeout", 121), ("retry_delay", 0),
    ("wrap_up", -1), ("enabled", "yes"), ("unknown", 1),
])
def test_invalid_policy_cannot_partially_change_queue(manager, field, value):
    previous = manager.policy("1002")
    with pytest.raises(ValueError):
        manager.configure("1002", {field: value})
    assert manager.policy("1002") == previous


def test_agent_cannot_be_added_through_login_service(manager):
    with pytest.raises(ValueError, match="does not declare"):
        manager.set_agent_state("Support", "sip:1", "available")
    assert manager.resolve("Support").members == ("sip:2",)
