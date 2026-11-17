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


def test_prepared_queue_handoff_is_not_a_second_live_ticket(manager):
    original = manager.enqueue("1002", "move", 1)
    reserved = manager.claim_turn(original)
    prepared = manager.prepare_transfer(original, "Support")
    assert not prepared.admitted and prepared.entry_id != original.entry_id
    assert manager.claim_turn(prepared) == ()
    assert manager.waiting.get("support") is None
    manager.rollback_transfer(prepared)
    assert not original.closed and original.reserved == reserved
    assert set(manager.waiting) == {"1002"} and not manager.staged_admissions


def test_queue_handoff_commit_releases_only_old_ticket_and_starts_new_budget(manager):
    original = manager.enqueue("1002", "move", 1)
    manager.claim_turn(original)
    prepared = manager.prepare_transfer(original, "Support")
    manager.test_clock[0] += 7
    manager.commit_transfer(original, prepared)
    assert original.closed and prepared.admitted and not prepared.closed
    assert prepared.enqueued == 107 and prepared.deadline == 167
    assert not manager.reservations and not manager.staged_admissions
    assert list(manager.waiting["support"]) == [prepared]
    assert manager.claim_turn(prepared) == ("sip:2",)
    manager.finish(prepared)
    assert not manager.waiting and not manager.reservations


def test_queue_handoff_revalidates_destination_and_preserves_source_on_failure(manager):
    original = manager.enqueue("1002", "move", 1)
    prepared = manager.prepare_transfer(original, "Support")
    manager.definitions.pop("support")
    with pytest.raises(ValueError):
        manager.commit_transfer(original, prepared)
    assert not original.closed and list(manager.waiting["1002"]) == [original]
    manager.rollback_transfer(prepared)
    assert not manager.staged_admissions


def test_prepared_admissions_hold_capacity_and_rollback_releases_it(manager):
    manager.configure("Support", {"max_waiting": 1})
    first = manager.enqueue("1002", "first-move", 1)
    second = manager.enqueue("1002", "second-move", 2)
    staged = manager.prepare_transfer(first, "Support")
    with pytest.raises(ValueError, match="full"):
        manager.prepare_transfer(second, "Support")
    manager.rollback_transfer(staged)
    assert not first.closed and not second.closed
    assert manager.prepare_transfer(second, "Support").admitted is False


def test_sequential_strategies_reserve_only_actual_candidate_and_rotate_on_offer(manager):
    manager.configure('1002', {'strategy': 'round_robin'})
    ticket = manager.enqueue('1002', 'rotate', 1)
    assert manager.claim_turn(ticket) == ('sip:1',)
    assert set(manager.reservations) == {'sip:1'}
    manager.release_attempt(ticket)
    assert manager.claim_turn(ticket) == ('sip:1',)  # no INVITE was dispatched
    attempt = manager.begin_offer(ticket, 'sip:1')
    assert attempt
    manager.finish_offer(ticket, 'sip:1', 'timeout', True, attempt)
    manager.release_attempt(ticket)
    assert manager.claim_turn(ticket) == ('sip:2',)
    assert manager._snapshot()['round_robin']['1002'] == 'sip:1'


def test_ordered_escalation_and_idle_consider_completed_ordinary_calls(manager):
    manager.configure('1002', {'strategy': 'ordered', 'agent_order': ['sip:2', 'sip:1']})
    ticket = manager.enqueue('1002', 'ordered', 1)
    assert manager.claim_turn(ticket) == ('sip:2',)
    attempt = manager.begin_offer(ticket, 'sip:2')
    manager.finish_offer(ticket, 'sip:2', 'busy', False, attempt)
    manager.release_attempt(ticket)
    assert manager.claim_turn(ticket) == ('sip:1',)
    manager.finish(ticket)
    manager.configure('1002', {'strategy': 'longest_idle'})
    manager.endpoints.claim_call('sip:1', 'ordinary')
    manager.test_clock[0] += 10
    # Manager reads this endpoint's actual connected leg, not the answered caller.
    manager.entry.runtime_data.sip = SimpleNamespace(get_session=lambda _call: SimpleNamespace(
        legs={"ordinary": SimpleNamespace(endpoint_id="sip:1", state="in_call", phase="answered")}))
    active = manager.endpoints.get('sip:1')
    manager.endpoints.release_call('sip:1', 'ordinary')
    manager.endpoint_changed(registry.EndpointRegistryEvent(registry.EndpointRegistryEventType.UPDATED,
        manager.endpoints.get('sip:1'), active))
    assert manager.claim_turn(manager.enqueue('1002', 'idle', 2)) == ('sip:2',)


def test_auto_pause_counts_only_actual_ringing_timeouts_and_manual_reset(manager):
    manager.configure('1002', {'auto_pause_after': 2, 'strategy': 'round_robin'})
    ticket = manager.enqueue('1002', 'misses', 1)
    def outcome(value, rang):
        manager.reservations['sip:1'] = ticket
        ticket.reserved = ('sip:1',)
        attempt = manager.begin_offer(ticket, 'sip:1')
        manager.finish_offer(ticket, 'sip:1', value, rang, attempt)
        manager.finish_offer(ticket, 'sip:1', value, rang, attempt)  # idempotent callback
        manager.release_attempt(ticket)
    outcome('timeout', False)
    outcome('busy', True)
    outcome('cancelled', True)
    assert manager.get_state('1002')['members'][0]['consecutive_misses'] == 0
    outcome('timeout', True)
    assert manager.agent_states['1002']['sip:1'] == 'available'
    outcome('timeout', True)
    state = manager.get_state('1002')['members'][0]
    assert state['state'] == 'paused' and state['pause_reason'] == 'no_answer'
    assert manager.agent_states['support']['sip:2'] == 'available'
    manager.set_agent_state('1002', 'sip:1', 'available')
    assert manager.get_state('1002')['members'][0]['consecutive_misses'] == 0


def test_distinct_exit_policy_and_single_projection_notification(manager):
    manager.configure('1002', {'fallback': 'default', 'fallback_full': 'overflow', 'fallback_timeout': 'assist'})
    policy = manager.policy('1002')
    assert policy.fallback_for('queue_full') == 'overflow'
    assert policy.fallback_for('queue_timeout') == 'assist'
    assert policy.fallback_for('queue_removed') == 'default'
    ticket = manager.enqueue('1002', 'notify', 1)
    manager.claim_turn(ticket)
    changes = []
    manager.subscribe(lambda: changes.append(1))
    manager.finish(ticket)
    assert len(changes) == 1


def test_conversation_wrap_starts_on_actual_end_not_answer(manager):
    manager.configure('1002', {'wrap_up': 12})
    ticket = manager.enqueue('1002', 'conversation', 1)
    manager.claim_turn(ticket)
    manager.connected(ticket, 'sip:1')
    assert ticket.closed and not manager.wrap_until
    manager.test_clock[0] += 30
    manager.complete_conversation(ticket, 'sip:1', 'normal')
    manager.complete_conversation(ticket, 'sip:1', 'normal')
    assert manager.wrap_until['sip:1'] == 142


def test_agent_leg_completion_ends_workload_before_remaining_caller_session(manager):
    manager.configure('1002', {'wrap_up': 10})
    ticket = manager.enqueue('1002', 'transferred-conversation', 1)
    manager.endpoints.claim_call('sip:1', ticket.call_id)
    manager.connected(ticket, 'sip:1')
    manager.test_clock[0] += 7
    active = manager.endpoints.get('sip:1')
    manager.endpoints.release_call('sip:1', ticket.call_id)
    manager.endpoint_changed(registry.EndpointRegistryEvent(registry.EndpointRegistryEventType.UPDATED,
        manager.endpoints.get('sip:1'), active))
    assert ticket.completed and not manager.conversations
    assert manager.wrap_until['sip:1'] == 117
    manager.test_clock[0] += 50
    manager.complete_conversation(ticket, 'sip:1', 'caller_later_ended')
    assert manager.wrap_until['sip:1'] == 117


def test_unanswered_candidate_does_not_reset_longest_idle_even_with_answered_caller(manager):
    manager.entry.runtime_data.sip = SimpleNamespace(get_session=lambda _call: SimpleNamespace(
        answer_committed=True, legs={"candidate": SimpleNamespace(endpoint_id="sip:1", state="remote_ringing", phase="closed")}))
    manager.endpoints.claim_call('sip:1', 'already-answered-caller')
    active = manager.endpoints.get('sip:1')
    manager.endpoints.release_call('sip:1', active.active_call_id)
    manager.endpoint_changed(registry.EndpointRegistryEvent(registry.EndpointRegistryEventType.UPDATED,
        manager.endpoints.get('sip:1'), active))
    assert 'sip:1' not in manager.last_ended


@pytest.mark.parametrize('state', ['paused', 'logged_out'])
def test_late_unanswered_offer_cannot_override_explicit_operator_state(manager, state):
    manager.configure('1002', {'auto_pause_after': 1})
    ticket = manager.enqueue('1002', 'late-timeout', 1)
    manager.claim_turn(ticket)
    attempt = manager.begin_offer(ticket, 'sip:1')
    manager.set_agent_state('1002', 'sip:1', state, reason='operator_choice')
    manager.finish_offer(ticket, 'sip:1', 'timeout', True, attempt)
    assert manager.agent_states['1002']['sip:1'] == state
    expected_reason = 'operator_choice' if state == 'paused' else ''
    assert manager.get_state('1002')['members'][0]['pause_reason'] == expected_reason


def test_sequential_queue_never_reselects_its_own_caller_ahead_of_idle_agent(manager):
    manager.configure('1002', {'strategy': 'round_robin'})
    manager.endpoints.claim_call('sip:1', 'caller-also-agent')
    ticket = manager.enqueue('1002', 'caller-also-agent', 1)
    assert manager.claim_turn(ticket) == ('sip:2',)
    assert 'sip:1' not in manager.reservations
