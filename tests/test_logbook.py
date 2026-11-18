"""Tests for concise human-readable VoIP Logbook entries."""

from __future__ import annotations

import importlib
from pathlib import Path
import sys
from types import SimpleNamespace
import types


logbook_component = sys.modules.setdefault(
    "homeassistant.components.logbook",
    types.ModuleType("homeassistant.components.logbook"),
)
logbook_component.LOGBOOK_ENTRY_ENTITY_ID = "entity_id"
logbook_component.LOGBOOK_ENTRY_CONTEXT_ID = "context_id"
logbook_component.LOGBOOK_ENTRY_ICON = "icon"
logbook_component.LOGBOOK_ENTRY_MESSAGE = "message"
logbook_component.LOGBOOK_ENTRY_NAME = "name"
core = sys.modules.setdefault("homeassistant.core", types.ModuleType("homeassistant.core"))


class _Event:
    def __class_getitem__(cls, _item):
        return cls


core.Event = _Event
core.HomeAssistant = object
core.callback = lambda function: function
sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
sys.modules.setdefault("homeassistant.components", types.ModuleType("homeassistant.components"))

PACKAGE_NAME = "voip_stack_logbook_test"
package = types.ModuleType(PACKAGE_NAME)
package.__path__ = [
    str(Path(__file__).resolve().parents[1] / "custom_components" / "voip_stack")
]
sys.modules.setdefault(PACKAGE_NAME, package)

logbook = importlib.import_module(f"{PACKAGE_NAME}.logbook")
const = importlib.import_module(f"{PACKAGE_NAME}.const")
automation_routing = importlib.import_module(
    f"{PACKAGE_NAME}.automation_routing"
)
DOMAIN = const.DOMAIN
SIP_CALL_ENDED_EVENT = const.SIP_CALL_ENDED_EVENT


def test_registers_only_terminal_call_events(monkeypatch) -> None:
    monkeypatch.setattr(logbook, "shared_entity_ids", lambda hass: {"call_event": "event.renamed_calls"})
    registrations = []

    logbook.async_describe_events(
        SimpleNamespace(),
        lambda domain, event_type, describe: registrations.append(
            (domain, event_type, describe)
        ),
    )

    assert len(registrations) == 2
    domain, event_type, describe = registrations[0]
    assert domain == DOMAIN
    assert event_type == SIP_CALL_ENDED_EVENT
    assert registrations[1][:2] == (DOMAIN, const.QUEUE_EVENT)
    assert describe(
        SimpleNamespace(
            data={
                "type": "ended",
                "caller": "Cucina",
                "callee": "Portone",
                "duration_seconds": 45,
            }
        )
    ) == {
        "name": "VoIP Stack",
        "message": "Cucina called Portone · 45 s",
        "entity_id": "event.renamed_calls",
        "icon": "mdi:phone-log",
    }


def phone_call(state="early", *, leg="first", direction="initiator", call_id="call-one", generation=1):
    return {"call_id": call_id, "generation": generation, "state": state, "direction": direction,
            "leg_id": leg, "peer_name": "Mobile", "peer_user": "3511234567"}


def test_account_activity_logs_one_calling_phase_for_multiple_contacts():
    phone = {"name": "Desk", "calls": [phone_call(), phone_call(leg="second")]}
    event = {"endpoint_id": "sip:desk", "previous": {"name": "Desk", "calls": []}, "phone": phone,
             "call": {"call_id": "call-one", "generation": 1, "dialed_target": "3511234567"}}
    entries = logbook._account_activity(event)
    assert len(entries) == 1 and entries[0]["name"] == "Desk"
    assert entries[0]["message"] == "Calling Mobile"
    event["previous"] = {"name": "Desk", "calls": [phone_call()]}
    assert logbook._account_activity(event) == []


def test_account_answer_has_the_committed_winner_not_the_first_contact():
    event = {"endpoint_id": "sip:desk", "previous": {"name": "Desk", "calls": [phone_call()]},
             "phone": {"name": "Desk", "calls": [phone_call("confirmed", leg="winner")]},
             "call": {"call_id": "call-one", "generation": 1, "answered_by": "Living room phone"}}
    assert logbook._account_activity(event)[0]["message"] == "Outgoing call to Living room phone answered"


def test_account_incoming_activity_identifies_the_caller_not_its_own_account():
    event = {"phone": {"name": "Desk", "calls": [phone_call(direction="recipient")]},
             "previous": {"calls": []}, "call": {"call_id": "call-one", "generation": 1,
                 "caller": "3511234567", "callee": "Desk"}}
    assert logbook._account_activity(event)[0]["message"] == "Incoming call from 3511234567"


def test_account_failure_uses_only_the_same_call_status_and_reason():
    event = {"endpoint_id": "sip:desk", "previous": {"name": "Desk", "calls": [phone_call()]},
             "phone": {"name": "Desk", "calls": []}, "call": {
                 "call_id": "call-one", "generation": 1, "type": "failed", "state": "failed",
                 "reason": "busy", "sip_status_code": 486, "pbx_phase": "terminated"}}
    assert logbook._account_activity(event)[0]["message"] == "Outgoing call to Mobile failed · busy · SIP 486"
    event["call"]["call_id"] = "another-call"
    assert logbook._account_activity(event) == []
    event["call"].update(call_id="call-one", generation=2)
    assert logbook._account_activity(event) == []


def test_account_noncall_changes_and_handoff_do_not_create_terminal_failures():
    assert logbook._account_activity({"phone": {"name": "Desk", "dnd": True, "calls": []}, "previous": {"calls": []}}) == []
    event = {"previous": {"name": "Desk", "calls": [phone_call("confirmed")]},
             "phone": {"name": "Desk", "calls": []},
             "call": {"call_id": "call-one", "generation": 1, "state": "in_call", "reason": "forwarded"}}
    assert logbook._account_activity(event) == []


def test_account_terminal_names_the_peer_and_direction_without_inventing_a_hangup_actor():
    occurrence = {"call_id": "test-to-p4", "generation": 5, "type": "ended", "state": "idle",
        "source_endpoint_id": "sip:test", "dest_endpoint_id": "esp:p4", "direction": "outgoing",
        "caller": "Test", "callee": "430", "connected_party": "Waveshare P4 Touch"}
    assert logbook._account_terminal_message(occurrence, "sip:test") == "Outgoing call to Waveshare P4 Touch ended"
    assert logbook._account_terminal_message(occurrence, "esp:p4") == "Incoming call from Test ended"
    occurrence["terminal_reason"] = "remote_hangup"
    message = logbook._account_terminal_message(occurrence, "sip:test")
    assert message == "Outgoing call to Waveshare P4 Touch ended · remote hangup"
    assert "ended by" not in message and "hung up" not in message


def test_account_terminal_uses_connected_party_after_queue_or_transfer():
    occurrence = {"type": "ended", "state": "idle", "direction": "outgoing",
        "source_endpoint_id": "sip:test", "dest_endpoint_id": "sip:desk",
        "caller": "Test", "callee": "Support queue", "dialed_target": "Support queue",
        "connected_party": "Desk", "reason": "local_hangup"}
    assert logbook._account_terminal_message(occurrence, "sip:test") == "Outgoing call to Desk ended · local hangup"
    assert logbook._account_terminal_message(occurrence, "sip:desk") == "Incoming call from Test ended · local hangup"


def test_global_terminal_prefers_committed_peer_over_later_unsuccessful_route():
    entry = logbook._describe_call({"type": "ended", "direction": "incoming", "state": "idle",
        "caller": "Courier", "callee": "Support queue", "connected_party": "Desk",
        "duration_seconds": 12, "reason": "remote_hangup", "call_id": "routed-call", "generation": 4,
        "route_history": [{"action": "forward", "destination": "Unanswered transfer target"}]})
    assert entry["message"] == "Courier called Desk · 12 s · remote hangup"
    assert (entry["call_id"], entry["generation"]) == ("routed-call", 4)


def test_missed_call_prefers_the_answered_endpoint_name() -> None:
    entry = logbook._describe_call(
        {
            "type": "missed",
            "caller": "428",
            "callee": "RG Casa",
            "answered_by": "Test",
        }
    )

    assert entry["message"] == "Missed call from 428 to Test"


def test_incoming_caller_cancel_is_presented_as_missed() -> None:
    entry = logbook._describe_call(
        {
            "type": "ended",
            "direction": "incoming",
            "state": "cancelled",
            "terminal_reason": "cancelled",
            "caller": "Courier",
            "local_name": "Door",
        }
    )

    assert entry["message"] == "Missed call from Courier to Door · cancelled"


def test_incoming_busy_without_answer_is_presented_as_missed() -> None:
    entry = logbook._describe_call(
        {
            "type": "ended",
            "direction": "incoming",
            "state": "busy",
            "terminal_reason": "busy",
            "caller": "Courier",
            "local_name": "Door",
        }
    )

    assert entry["message"] == "Missed call from Courier to Door · busy"


def test_explicit_incoming_decline_is_not_presented_as_missed() -> None:
    entry = logbook._describe_call(
        {
            "type": "ended",
            "direction": "incoming",
            "state": "declined",
            "terminal_reason": "declined",
            "caller": "Courier",
            "local_name": "Door",
        }
    )

    assert entry["message"] == "Declined call from Courier to Door"


def test_zero_second_connected_incoming_call_is_not_presented_as_missed() -> None:
    entry = logbook._describe_call(
        {
            "type": "ended",
            "direction": "incoming",
            "state": "idle",
            "terminal_reason": "remote_hangup",
            "caller": "Courier",
            "local_name": "Door",
            "duration_seconds": 0,
        }
    )

    assert entry["message"] == "Courier called Door · 0 s · remote hangup"


def test_failure_is_readable_and_sip_uri_is_compact() -> None:
    entry = logbook._describe_call(
        {
            "type": "failed",
            "direction": "incoming",
            "caller": "sip:428@example.test;transport=tcp",
            "callee": "Casa",
            "terminal_reason": "media_incompatible",
        }
    )

    assert entry["message"] == "Call from 428 to Casa failed · media incompatible"


def test_sips_party_is_reduced_to_its_human_identity() -> None:
    assert logbook._party(
        {"caller": "sips:daniele@pbx.example;transport=tls"}, "caller"
    ) == "daniele"


def test_incoming_call_prefers_local_phone_name_over_dialed_extension() -> None:
    entry = logbook._describe_call(
        {
            "type": "ended",
            "direction": "incoming",
            "caller": "426",
            "callee": "427",
            "local_name": "Casa",
            "duration_seconds": 2,
        }
    )

    assert entry["message"] == "426 called Casa · 2 s"


def test_incoming_call_prefers_selected_pbx_destination() -> None:
    entry = logbook._describe_call(
        {
            "type": "ended",
            "direction": "incoming",
            "caller": "426",
            "callee": "427",
            "local_name": "Casa",
            "route_history": [
                {"action": "forward", "destination": "RG Casa"},
                {"action": "decline", "destination": ""},
            ],
            "duration_seconds": 2,
        }
    )

    assert entry["message"] == "426 called RG Casa · 2 s"


def test_duration_format_is_bounded_and_human_readable() -> None:
    assert logbook._format_duration(0) == "0 s"
    assert logbook._format_duration(125) == "2 min 05 s"
    assert logbook._format_duration(3725) == "1 h 02 min 05 s"
    assert logbook._format_duration(-1) == ""
    assert logbook._format_duration("not-a-number") == ""


def test_only_logical_terminal_occurrences_are_logbook_summaries() -> None:
    common = {"call_id": "call-1", "type": "ended", "scope": "session"}

    assert not automation_routing.is_logbook_call_summary(
        {
            **common,
            "state": "cancelled",
            "owner": "router",
            "pbx_phase": "ringing",
        }
    )
    assert automation_routing.is_logbook_call_summary(
        {
            **common,
            "state": "idle",
            "owner": "bridge",
            "pbx_phase": "terminating",
        }
    )
    assert automation_routing.is_logbook_call_summary(
        {
            **common,
            "state": "idle",
            "duration_seconds": 0,
        }
    )
    assert not automation_routing.is_logbook_call_summary(
        {
            **common,
            "scope": "esp",
            "state": "idle",
            "duration_seconds": 12,
        }
    )
