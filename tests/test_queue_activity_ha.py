"""Queue activity uses raw HA Recorder events and admission-based cohorts."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.core import Event, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util

from custom_components.voip_stack import queue_activity as activity

pytestmark = pytest.mark.ha
UTC = timezone.utc


def occurrence(kind, entry, minute, **extra):
    return {"event_type": kind, "entry_id": entry, "queue_id": "support",
            "timestamp": datetime(2026, 11, 11, 10, minute, tzinfo=UTC).isoformat(), **extra}


def test_metrics_cohort_deduplicates_answers_keeps_pending_and_separates_rejected():
    start = datetime(2026, 11, 11, 10, 10, tzinfo=UTC)
    end = start + timedelta(minutes=30)
    events = [occurrence("entered", "old", 9), occurrence("answered", "old", 12),
        occurrence("entered", "a", 10), occurrence("offer_started", "a", 11),
        occurrence("offer_started", "a", 12), occurrence("answered", "a", 13, wait_seconds=17),
        occurrence("answered", "a", 14, wait_seconds=99), occurrence("exited", "a", 20, outcome="completed"),
        occurrence("entered", "b", 11), occurrence("exited", "b", 12, outcome="abandoned", wait_seconds=3),
        occurrence("entered", "pending", 12), occurrence("exited", "overflow", 13, outcome="overflow"),
        occurrence("entered", "late-answer", 39), occurrence("answered", "late-answer", 41)]
    report = activity.reduce_queue_metrics(events, "support", start, end, end)
    assert report["admitted"] == 4 and report["rejected"] == 1 and report["pending"] == 2
    assert report["outcomes"] == {"answered": 1, "abandoned": 1, "overflow": 1}
    assert report["completed_waits"] == 2 and report["average_wait_seconds"] == 10
    assert report["maximum_wait_seconds"] == 17 and report["as_of"] == end.isoformat()


def test_metrics_uses_distinct_admissions_when_the_same_call_moves_between_queues():
    start = datetime(2026, 11, 11, 10, 0, tzinfo=UTC)
    end = start + timedelta(hours=1)
    events = [occurrence("entered", "admission-A", 1, call_id="same", generation=1),
        occurrence("exited", "admission-A", 2, outcome="transferred"),
        occurrence("entered", "admission-B", 2, call_id="same", generation=1, queue_id="sales"),
        occurrence("answered", "admission-B", 3, queue_id="sales")]
    assert activity.reduce_queue_metrics(events, "support", start, end, end)["outcomes"] == {"transferred": 1}
    assert activity.reduce_queue_metrics(events, "sales", start, end, end)["outcomes"] == {"answered": 1}


def test_outcome_without_its_admission_is_not_invented_as_a_new_cohort_entry():
    start = datetime(2026, 11, 11, 10, 10, tzinfo=UTC)
    end = start + timedelta(minutes=30)
    # The admission predates the query/retention window, so Recorder's selected
    # rows contain its later answer/exit only. Neither creates a new admission.
    events = [occurrence("answered", "older-admission", 11),
              occurrence("exited", "older-admission", 20, outcome="completed", talk_seconds=9)]
    report = activity.reduce_queue_metrics(events, "support", start, end, end)
    assert report["admitted"] == report["rejected"] == report["pending"] == 0
    assert report["outcomes"] == {}


def test_p95_and_operator_workload_are_admission_based_as_of_until():
    start = datetime(2026, 11, 11, 10, 0, tzinfo=UTC)
    end = start + timedelta(hours=1)
    events = []
    for index in range(20):
        events += [occurrence("entered", str(index), index),
                   occurrence("answered", str(index), index + 1, endpoint_id="operator-a", wait_seconds=index)]
    events += [occurrence("exited", "0", 30, outcome="completed", talk_seconds=11),
               {**occurrence("exited", "1", 30, outcome="completed", talk_seconds=99),
                "timestamp": (end + timedelta(seconds=1)).isoformat()}]
    report = activity.reduce_queue_metrics(events, "support", start, end, end)
    assert report["admitted"] == 20 and report["outcomes"] == {"answered": 20}
    assert report["p95_wait_seconds"] == 18 and report["average_wait_seconds"] == 9.5
    assert report["operators"] == {"operator-a": {"answered": 20, "completed": 1, "talk_seconds": 11}}


async def test_raw_bus_events_record_and_paginate_without_event_entity_double_count(recorder_mock, hass):
    received = []
    remove = hass.bus.async_listen(activity.QUEUE_EVENT, lambda event: received.append(dict(event.data)))
    try:
        for kind in ("entered", "offer_started", "answered"):
            activity.emit_queue_event(hass, {"event_type": kind, "entry_id": "one", "queue_id": "support",
                "config_entry_id": "integration-entry", "call_id": "call", "generation": 1,
                "password": "never-record-this", "wait_seconds": 7})
        activity.emit_queue_event(hass, {"event_type": "entered", "entry_id": "other", "queue_id": "sales"})
        hass.states.async_set("event.support_activity", "2026-11-11T10:00:00Z", {
            "event_type": "answered", "entry_id": "one", "queue_id": "support"})
        await hass.async_block_till_done()
        await recorder_mock.async_block_till_done()
        end = dt_util.utcnow() + timedelta(seconds=1)
        start = end - timedelta(hours=1)
        first = await activity.async_get_queue_activity(hass, "support", since=start, until=end, limit=2)
        assert len(first["events"]) == 2 and first["next_cursor"]
        second = await activity.async_get_queue_activity(hass, "support", limit=2, cursor=first["next_cursor"])
        assert len(second["events"]) == 1 and second["next_cursor"] is None
        assert len({event["record_id"] for event in first["events"] + second["events"]}) == 3
        assert all("password" not in event for event in received)
        assert all(event["schema_version"] == 1 for event in received)
        assert {event["event_type"] for event in first["events"] + second["events"]} == {"entered", "offer_started", "answered"}
        metrics = await activity.async_get_queue_metrics(hass, "support", since=start, until=end)
        assert metrics["admitted"] == 1 and metrics["outcomes"] == {"answered": 1}
        assert metrics["completed_waits"] == 1 and metrics["average_wait_seconds"] == 7
        assert metrics["as_of"] == end.isoformat()
        with pytest.raises(ServiceValidationError, match="cursor"):
            await activity.async_get_queue_activity(hass, "sales", cursor=first["next_cursor"])
    finally:
        remove()


async def test_event_entity_is_only_a_projection_of_the_raw_semantic_event(hass):
    entity = activity.QueueActivityEvent("support", "Support")
    entity.hass = hass
    entity.async_write_ha_state = Mock()
    data = {"schema_version": 1, "queue_id": "support", "entry_id": "one", "event_type": "entered"}
    entity._receive(Event(activity.QUEUE_EVENT, data))
    assert entity.state_attributes["event_type"] == "entered"
    entity.async_write_ha_state.assert_called_once()
    entity._receive(Event(activity.QUEUE_EVENT, {**data, "queue_id": "other"}))
    entity.async_write_ha_state.assert_called_once()


async def test_pagination_keeps_equal_submicrosecond_recorder_timestamps(recorder_mock, hass):
    timestamp = dt_util.utcnow().replace(microsecond=123456).timestamp() + .0000002
    for index in range(3):
        hass.bus.async_fire(activity.QUEUE_EVENT, {"schema_version": 1, "event_type": "entered",
            "entry_id": f"same-time-{index}", "queue_id": "support"}, time_fired=timestamp)
    await hass.async_block_till_done()
    await recorder_mock.async_block_till_done()
    first = await activity.async_get_queue_activity(hass, "support",
        since=dt_util.utc_from_timestamp(timestamp - 1), until=dt_util.utc_from_timestamp(timestamp + 1), limit=2)
    second = await activity.async_get_queue_activity(hass, "support", cursor=first["next_cursor"], limit=2)
    assert len(first["events"]) == 2 and len(second["events"]) == 1
    assert len({event["record_id"] for event in first["events"] + second["events"]}) == 3


@pytest.mark.parametrize("values", [{"limit": 0}, {"limit": 1001}, {"limit": True}, {"cursor": "bad"},
    {"since": "2026-11-10T00:00:00"},
    {"since": "2026-01-01T00:00:00Z", "until": "2026-02-02T00:00:00Z"},
    {"since": "2026-01-02T00:00:00Z", "until": "2026-01-01T00:00:00Z"}])
async def test_invalid_query_is_rejected_before_opening_recorder(hass, monkeypatch, values):
    query = Mock(side_effect=AssertionError("Recorder must not be opened"))
    monkeypatch.setattr(activity, "_recorder", query)
    with pytest.raises(ServiceValidationError):
        await activity.async_get_queue_activity(hass, "support", **values)
    query.assert_not_called()


async def test_native_service_registration_queries_on_recorder_executor(recorder_mock, hass, monkeypatch):
    import threading
    from custom_components.voip_stack import services

    thread = threading.get_ident()
    observed = []
    original = activity._history
    def history(*args):
        observed.append(threading.get_ident())
        return original(*args)
    monkeypatch.setattr(activity, "_history", history)
    await services.async_register_services(hass, {
        "get_queue_activity": activity.async_get_queue_activity_service,
        "get_queue_metrics": activity.async_get_queue_metrics_service})
    activity.emit_queue_event(hass, {"event_type": "entered", "entry_id": "service-entry", "queue_id": "support"})
    await hass.async_block_till_done()
    await recorder_mock.async_block_till_done()
    response = await hass.services.async_call("voip_stack", "get_queue_activity", {"queue": "support"},
        blocking=True, return_response=True)
    assert len(response["events"]) == 1 and response["events"][0]["entry_id"] == "service-entry"
    assert observed and all(identity != thread for identity in observed)
    metrics = await hass.services.async_call("voip_stack", "get_queue_metrics", {"queue": "support"},
        blocking=True, return_response=True)
    assert metrics["admitted"] == metrics["pending"] == 1 and not metrics["outcomes"]


async def test_history_without_recorder_explains_missing_owner(hass):
    with pytest.raises(ServiceValidationError, match="Recorder"):
        await activity.async_get_queue_activity(hass, "support")


async def test_queries_require_admin_before_resolving_history(hass, monkeypatch):
    from custom_components.voip_stack import authorization
    denied = AsyncMock(side_effect=ServiceValidationError("admin required"))
    monkeypatch.setattr(authorization, "async_require_service_admin", denied)
    query = AsyncMock()
    monkeypatch.setattr(activity, "async_get_queue_activity", query)
    with pytest.raises(ServiceValidationError, match="admin"):
        await activity.async_get_queue_activity_service(ServiceCall(hass, "voip_stack", "get_queue_activity", {"queue": "support"}))
    query.assert_not_called()


async def test_emitter_never_places_queries_or_invalid_observations_on_call_path(hass):
    assert activity.emit_queue_event(hass, {"event_type": "answered", "queue_id": "support"}) is None
    assert activity.emit_queue_event(hass, {"event_type": "anything", "queue_id": "support", "entry_id": "one"}) is None
    fired = activity.emit_queue_event(hass, {"event_type": "entered", "queue_id": "support", "entry_id": "one",
        "wait_seconds": float("nan"), "talk_seconds": -1, "authorization": "never-record"})
    assert "wait_seconds" not in fired and "talk_seconds" not in fired and "authorization" not in fired
