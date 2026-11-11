"""Queue activity on HA's native event bus and Recorder, without another store."""

from __future__ import annotations

import base64
from contextlib import closing
from datetime import datetime, timedelta
import json
import logging
import math
from uuid import uuid4

from homeassistant.components.event import EventEntity
from homeassistant.core import callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.util import dt as dt_util

from .const import DOMAIN, QUEUE_EVENT

QUEUE_EVENT_VERSION = 1
QUEUE_EVENT_TYPES = ("entered", "offer_started", "offer_ended", "answered", "exited",
                     "agent_state_changed", "policy_changed")
_MAX_WINDOW = timedelta(days=31)
_LOGGER = logging.getLogger(__name__)
_TEXT_FIELDS = ("config_entry_id", "entry_id", "queue_id", "queue_name", "call_id", "endpoint_id", "disposition", "destination",
                "attempt_id", "reason", "outcome", "state", "previous_state", "caller_number", "called_number")


@callback
def emit_queue_event(hass, payload):
    """Publish one committed semantic occurrence; Recorder owns persistence."""
    kind = str(payload.get("event_type") or "")
    queue_id = str(payload.get("queue_id") or "")
    if kind not in QUEUE_EVENT_TYPES or not queue_id or (
        kind not in {"agent_state_changed", "policy_changed"} and not payload.get("entry_id")
    ):
        _LOGGER.warning("Queue activity omitted because its semantic identity is incomplete")
        return None
    event = {"schema_version": QUEUE_EVENT_VERSION, "event_type": kind, "event_id": uuid4().hex}
    for key in _TEXT_FIELDS:
        value = payload.get(key)
        if value is not None:
            event[key] = str(value)[:512]
    generation = payload.get("generation")
    if isinstance(generation, int) and not isinstance(generation, bool) and generation >= 0:
        event["generation"] = generation
    for key in ("wait_seconds", "talk_seconds"):
        value = payload.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
            event[key] = float(value)
    hass.bus.async_fire(QUEUE_EVENT, event)
    return event


def _datetime(value, fallback):
    if value is None:
        return fallback
    parsed = dt_util.parse_datetime(value) if isinstance(value, str) else value
    if not isinstance(parsed, datetime) or parsed.tzinfo is None:
        raise ServiceValidationError("Use an ISO timestamp with a timezone")
    return dt_util.as_utc(parsed)


def _window(since=None, until=None):
    end = _datetime(until, dt_util.utcnow())
    start = _datetime(since, end - timedelta(hours=24))
    if start >= end or end - start > _MAX_WINDOW:
        raise ServiceValidationError("Choose a positive activity window of at most 31 days")
    return start, end


def _encode_cursor(queue_id, start, end, last):
    value = {"version": 1, "queue_id": queue_id, "since": start.isoformat(), "until": end.isoformat(), "last": last}
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode()


def _decode_cursor(cursor, queue_id, since, until):
    try:
        if not isinstance(cursor, str) or len(cursor) > 2048:
            raise ValueError
        value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if value["version"] != 1 or value["queue_id"] != queue_id:
            raise ValueError
        start, end = _window(value["since"], value["until"])
        if since is not None and _datetime(since, start) != start:
            raise ValueError
        if until is not None and _datetime(until, end) != end:
            raise ValueError
        timestamp, identity = value["last"]
        if (isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp)
                or not start.timestamp() <= timestamp < end.timestamp()
                or isinstance(identity, bool) or not isinstance(identity, int) or identity < 1):
            raise ValueError
        return start, end, (timestamp, identity)
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
        raise ServiceValidationError("The activity cursor does not match this queue and window") from error


def _recorder(hass):
    from homeassistant.helpers.recorder import get_instance
    try:
        return get_instance(hass)
    except KeyError as error:
        raise ServiceValidationError("Queue history requires Home Assistant Recorder") from error


def _rows(hass, start, end, *, before=None, ascending=False):
    """Stream the indexed raw event type in Recorder's own executor/session."""
    from homeassistant.components.recorder.db_schema import EventData, Events, EventTypes
    from homeassistant.helpers.recorder import session_scope
    from sqlalchemy import and_, or_, select

    statement = (select(Events.event_id, Events.time_fired_ts, EventData.shared_data)
        .join(EventTypes, Events.event_type_id == EventTypes.event_type_id)
        .join(EventData, Events.data_id == EventData.data_id)
        .where(EventTypes.event_type == QUEUE_EVENT,
               Events.time_fired_ts >= start.timestamp(), Events.time_fired_ts < end.timestamp()))
    if before is not None:
        timestamp, identity = before
        statement = statement.where(or_(Events.time_fired_ts < timestamp,
            and_(Events.time_fired_ts == timestamp, Events.event_id < identity)))
    order = (Events.time_fired_ts, Events.event_id)
    statement = statement.order_by(*(field.asc() if ascending else field.desc() for field in order))
    with session_scope(hass=hass, read_only=True) as session:
        for identity, timestamp, raw in session.execute(statement.execution_options(yield_per=256)):
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict) or payload.get("schema_version") != QUEUE_EVENT_VERSION or payload.get("event_type") not in QUEUE_EVENT_TYPES:
                continue
            yield {**payload, "record_id": identity, "record_timestamp": timestamp,
                   "timestamp": dt_util.utc_from_timestamp(timestamp).isoformat()}


def _history(hass, queue_id, start, end, limit, before):
    events = []
    with closing(_rows(hass, start, end, before=before)) as rows:
        for event in rows:
            if event.get("queue_id") == queue_id:
                events.append(event)
                if len(events) > limit:
                    break
    more = len(events) > limit
    events = events[:limit]
    last = events[-1] if more else None
    return {"schema_version": 1, "queue_id": queue_id, "since": start.isoformat(), "until": end.isoformat(),
            "events": events, "next_cursor": _encode_cursor(queue_id, start, end,
                [last["record_timestamp"], last["record_id"]]) if last else None,
            "source": "recorder", "retention": "recorder"}


async def async_get_queue_activity(hass, queue_id, *, since=None, until=None, limit=100, cursor=None):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ServiceValidationError("Activity limit must be between 1 and 1000")
    if cursor:
        start, end, before = _decode_cursor(cursor, queue_id, since, until)
    else:
        start, end = _window(since, until)
        before = None
    return await _recorder(hass).async_add_executor_job(_history, hass, queue_id, start, end, limit, before)


def reduce_queue_metrics(events, queue_id, start, end, as_of):
    """Count admissions once; attempts and terminal retransmissions are not calls."""
    entries = {}
    for event in events:
        if event.get("queue_id") != queue_id or not event.get("entry_id"):
            continue
        occurred = dt_util.parse_datetime(event.get("timestamp", ""))
        if occurred is None or occurred >= as_of:
            continue
        row = entries.setdefault(event["entry_id"], {"entered": None, "answered": None, "exited": None})
        kind = event.get("event_type")
        if kind in row and row[kind] is None:
            row[kind] = event
    outcomes = {}
    waits = []
    operators = {}
    admitted = pending = rejected = unanchored = 0
    for row in entries.values():
        first = row["entered"] or row["exited"]
        if first is None:
            continue
        entered_at = dt_util.parse_datetime(first["timestamp"])
        if not start <= entered_at < end:
            continue
        if row["entered"] is None:
            outcome = str(first.get("outcome") or first.get("reason") or "rejected")
            # A later completion whose admission is outside this query (or
            # Recorder retention) cannot become a new rejected admission.
            if row["answered"] is not None or (
                    first.get("disposition") != "rejected" and outcome not in {"overflow", "rejected"}):
                unanchored += 1
                continue
            rejected += 1
        else:
            admitted += 1
            if row["answered"] is not None:
                outcome = "answered"
                endpoint_id = str(row["answered"].get("endpoint_id") or "")
                if endpoint_id:
                    operator = operators.setdefault(endpoint_id, {"answered": 0, "completed": 0, "talk_seconds": 0.0})
                    operator["answered"] += 1
                    completed = row["exited"]
                    if completed is not None and completed.get("outcome") == "completed":
                        operator["completed"] += 1
                        talk = completed.get("talk_seconds")
                        if isinstance(talk, (int, float)) and not isinstance(talk, bool) and math.isfinite(talk) and talk >= 0:
                            operator["talk_seconds"] += float(talk)
            elif row["exited"] is not None:
                outcome = str(row["exited"].get("outcome") or row["exited"].get("reason") or "exited")
            else:
                pending += 1
                continue
            terminal = row["answered"] or row["exited"]
            wait = terminal.get("wait_seconds")
            if wait is None:
                wait = (dt_util.parse_datetime(terminal["timestamp"]) - entered_at).total_seconds()
            if isinstance(wait, (int, float)) and math.isfinite(wait) and wait >= 0:
                waits.append(float(wait))
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    return {"schema_version": 1, "queue_id": queue_id, "cohort_since": start.isoformat(),
            "cohort_until": end.isoformat(), "as_of": as_of.isoformat(), "admitted": admitted,
            "rejected": rejected, "pending": pending, "unanchored": unanchored, "outcomes": outcomes,
            "completed_waits": len(waits), "average_wait_seconds": sum(waits) / len(waits) if waits else None,
            "maximum_wait_seconds": max(waits) if waits else None,
            "p95_wait_seconds": sorted(waits)[math.ceil(.95 * len(waits)) - 1] if waits else None,
            "wait_percentile_method": "nearest_rank", "operators": operators,
            "source": "recorder", "retention": "recorder"}


def _metrics(hass, queue_id, start, end, as_of):
    return reduce_queue_metrics(_rows(hass, start, as_of, ascending=True), queue_id, start, end, as_of)


async def async_get_queue_metrics(hass, queue_id, *, since=None, until=None):
    start, end = _window(since, until)
    as_of = end
    return await _recorder(hass).async_add_executor_job(_metrics, hass, queue_id, start, end, as_of)


async def _service(call, query):
    from .authorization import async_require_service_admin
    from .call_queues import get_queue_manager

    await async_require_service_admin(call.hass, call)
    data = dict(call.data)
    ref = str(data.pop("queue")).strip()
    manager = get_queue_manager(call.hass)
    queue = manager.resolve(ref) if manager is not None else None
    identity = queue.queue_id if queue is not None else ref.removeprefix("service:queue:").removeprefix("queue:")
    if not identity:
        raise ServiceValidationError("Select a queue")
    return await query(call.hass, identity, **data)


async def async_get_queue_activity_service(call):
    return await _service(call, async_get_queue_activity)


async def async_get_queue_metrics_service(call):
    return await _service(call, async_get_queue_metrics)


class QueueActivityEvent(EventEntity):
    """UI projection of the same raw occurrences recorded by Recorder."""

    _attr_event_types = QUEUE_EVENT_TYPES
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Activity"
    _attr_icon = "mdi:account-clock"

    def __init__(self, queue_id, name):
        self.queue_id = queue_id
        self._attr_unique_id = f"queue_{queue_id}_activity"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, f"group:{queue_id}")}, name=name)

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(self.hass.bus.async_listen(QUEUE_EVENT, self._receive))

    @callback
    def _receive(self, event):
        if event.data.get("queue_id") == self.queue_id and event.data.get("schema_version") == QUEUE_EVENT_VERSION:
            self.async_set_context(event.context)
            self._trigger_event(event.data["event_type"], dict(event.data))
            self.async_write_ha_state()


@callback
def async_setup_queue_activity(hass, entry, async_add_entities):
    """Follow the existing queue definitions and HA entity lifecycle."""
    entities = {}

    @callback
    def reconcile(_event=None):
        manager = entry.runtime_data.queues
        definitions = manager.definitions if manager is not None else {}
        for identity in entities.keys() - definitions.keys():
            entity = entities.pop(identity)
            if entity.hass is not None:
                hass.async_create_task(entity.async_remove())
        added = []
        for identity, queue in definitions.items():
            if identity not in entities:
                entities[identity] = QueueActivityEvent(identity, queue.name)
                subentry_id = next((item.subentry_id for item in entry.subentries.values()
                    if item.subentry_type == "group" and item.data.get("group_id") == identity), None)
                added.append((entities[identity], subentry_id))
        for entity, subentry_id in added:
            async_add_entities([entity], config_subentry_id=subentry_id)

    entry.async_on_unload(hass.bus.async_listen("voip_stack.queue_changed", reconcile))
    reconcile()
