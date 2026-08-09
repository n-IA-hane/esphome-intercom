"""User-facing dial-plan use cases mapped to executable qualification classes."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DialplanUseCase:
    """One likely HA automation intent, expressed through a PBX primitive."""

    id: str
    intent: str
    hook: str
    conditions: tuple[str, ...]
    operation: str
    true_outcome: str
    false_outcome: str
    qualification: str


def _case(
    id: str,
    intent: str,
    hook: str,
    conditions: tuple[str, ...],
    operation: str,
    true_outcome: str,
    false_outcome: str,
    qualification: str,
) -> DialplanUseCase:
    return DialplanUseCase(
        id,
        intent,
        hook,
        conditions,
        operation,
        true_outcome,
        false_outcome,
        qualification,
    )


USE_CASES = (
    _case(
        "known-caller-by-presence",
        "Known door phone follows the resident",
        "route_requested",
        ("caller", "presence"),
        "select_inbound_destination",
        "ring nearby ESP phone",
        "ring original HA phone",
        "conditional-route-real-ha",
    ),
    _case(
        "office-hours-reception",
        "External calls use reception during opening hours",
        "route_requested",
        ("ingress", "time", "weekday"),
        "select_inbound_destination",
        "ring reception",
        "use configured fallback",
        "conditional-route-real-ha",
    ),
    _case(
        "holiday-assist",
        "Holiday calls go to Assist or another attendant",
        "route_requested",
        ("ingress", "calendar"),
        "select_inbound_destination",
        "route to Assist",
        "use normal schedule",
        "conditional-route-real-ha",
    ),
    _case(
        "alarm-reject",
        "Reject nonessential calls while the alarm is triggered",
        "route_requested",
        ("ingress", "alarm"),
        "route_decline",
        "603 decline",
        "use configured fallback",
        "route-terminal-real-ha",
    ),
    _case(
        "anonymous-caller-policy",
        "Unknown callers use a restricted destination",
        "route_requested",
        ("caller", "phonebook_match"),
        "select_inbound_destination",
        "ring screening destination",
        "ring normal destination",
        "caller-filter-real-ha",
    ),
    _case(
        "endpoint-availability-fallback",
        "Prefer one phone only while it is available",
        "route_requested",
        ("endpoint_connectivity",),
        "select_inbound_destination",
        "ring preferred phone",
        "ring fallback phone or group",
        "conditional-route-real-ha",
    ),
    _case(
        "quiet-hours-group",
        "Use a smaller ring group during quiet hours",
        "route_requested",
        ("time", "input_boolean"),
        "select_inbound_destination",
        "ring quiet group",
        "ring whole-house group",
        "conditional-route-real-ha",
    ),
    _case(
        "explicit-extension-wins",
        "A caller-entered extension bypasses broad automation",
        "preanswer_dtmf",
        ("entered_extension",),
        "canonical_dialplan",
        "route exact extension",
        "unknown extension fails",
        "trunk-dtmf-live",
    ),
    _case(
        "trunk-only-override",
        "A policy affects provider calls but not local extensions",
        "route_requested",
        ("ingress",),
        "select_inbound_destination",
        "override trunk route",
        "leave extension route unchanged",
        "ingress-filter-real-ha",
    ),
    _case(
        "concurrent-guarded-routing",
        "Several simultaneous calls cannot consume each other's decision",
        "route_requested",
        ("call_id", "state", "sequence"),
        "route",
        "apply current decision",
        "reject stale decision",
        "guarded-route-ha-runtime",
    ),
    _case(
        "no-answer-next-room",
        "An unanswered room phone falls through to another room",
        "phone_ringing_for",
        ("phone", "duration", "presence"),
        "forward_resume",
        "ring next room",
        "resume original phone",
        "ringing-forward-real-ha",
    ),
    _case(
        "no-answer-ring-group",
        "An unanswered phone falls through to the whole house",
        "phone_ringing_for",
        ("phone", "duration"),
        "forward_resume",
        "ring group",
        "resume original phone",
        "ringing-forward-real-ha",
    ),
    _case(
        "follow-me-mobile",
        "An unanswered local phone calls a public mobile number",
        "phone_ringing_for",
        ("phone", "duration", "trunk_registered"),
        "forward_resume",
        "dial mobile through trunk",
        "resume original phone",
        "ringing-forward-trunk-live",
    ),
    _case(
        "no-answer-assist",
        "An unanswered call falls through to voice Assist",
        "phone_ringing_for",
        ("phone", "duration"),
        "forward_resume",
        "start Assist leg",
        "resume original phone",
        "ringing-forward-assist-ha",
    ),
    _case(
        "failed-forward-busy",
        "A failed replacement returns busy instead of resuming",
        "phone_ringing_for",
        ("phone", "duration"),
        "forward_busy",
        "connect replacement",
        "486 busy",
        "forward-failure-ha-runtime",
    ),
    _case(
        "blind-transfer",
        "Move an established call to another extension",
        "connected",
        ("phone", "destination"),
        "transfer",
        "REFER accepted and NOTIFY succeeds",
        "original dialog follows failure policy",
        "refer-notify-real-sipp",
    ),
    _case(
        "attended-transfer",
        "Replace a consultation call with the original caller",
        "two_connected_calls",
        ("phone", "call_id", "replaces_call_id"),
        "transfer_replaces",
        "consulted peer receives original call",
        "both original dialogs remain consistent",
        "refer-replaces-ha-runtime",
    ),
    _case(
        "secure-transfer",
        "Transfer to a secure SIP identity",
        "connected",
        ("sips_uri",),
        "transfer",
        "preserve sips identity and TLS route",
        "reject unverified route",
        "sips-transfer-transport-lab",
    ),
    _case(
        "dnd-by-occupancy",
        "Disable ringing when nobody is home",
        "occupancy_changed",
        ("zone",),
        "set_dnd",
        "DND enabled",
        "DND disabled",
        "phone-policy-ha-runtime",
    ),
    _case(
        "scheduled-auto-answer",
        "Enable auto answer only for a controlled time window",
        "schedule_changed",
        ("schedule",),
        "set_auto_answer",
        "auto answer enabled",
        "manual answer retained",
        "phone-policy-ha-runtime",
    ),
    _case(
        "scheduled-video-call",
        "Start a video check from a selected phone",
        "time_or_event",
        ("destination_connectivity",),
        "call_send_video",
        "audio and video offered",
        "do not place call",
        "p4-video-hil",
    ),
    _case(
        "doorbell-actionable-notification",
        "Open the matching card or decline from a notification",
        "phone_ringing",
        ("caller", "receiving_phone"),
        "notify_answer_or_decline",
        "card claims media or call declines",
        "notification expires",
        "browser-notification-ha",
    ),
    _case(
        "missed-call-notification",
        "Notify only for one phone's missed call",
        "phone_missed",
        ("phone",),
        "notify",
        "send notification",
        "ignore other phones",
        "event-entity-ha-runtime",
    ),
    _case(
        "in-call-dtmf-gate",
        "A trusted caller opens a gate with a DTMF key",
        "dtmf",
        ("caller", "source_leg", "digit"),
        "ha_action",
        "press gate control",
        "ignore event",
        "established-dtmf-live",
    ),
    _case(
        "pause-media-during-call",
        "Pause entertainment while a selected phone is in call",
        "phone_call_state",
        ("phone", "state"),
        "ha_scene_or_media_action",
        "pause on connected",
        "restore on terminal event",
        "state-projection-ha-runtime",
    ),
    _case(
        "call-triggered-lighting",
        "Turn on an area light while a door call is active",
        "phone_call_state",
        ("caller", "phone", "state"),
        "ha_light_action",
        "activate call scene",
        "restore previous scene",
        "state-projection-ha-runtime",
    ),
)


ALLOWED_HOOKS = frozenset(case.hook for case in USE_CASES)
ALLOWED_OPERATIONS = frozenset(case.operation for case in USE_CASES)


def validate_use_cases() -> list[str]:
    """Return catalog errors. An empty list means the matrix is coherent."""

    errors: list[str] = []
    ids = [case.id for case in USE_CASES]
    if len(ids) != len(set(ids)):
        errors.append("duplicate dial-plan use-case ids")
    for case in USE_CASES:
        if not case.conditions:
            errors.append(f"{case.id}: no condition or selector")
        if not case.false_outcome:
            errors.append(f"{case.id}: no false/failure outcome")
        if not case.qualification:
            errors.append(f"{case.id}: no qualification class")
    required_operations = {
        "select_inbound_destination",
        "forward_resume",
        "route",
        "transfer",
        "transfer_replaces",
        "set_dnd",
        "set_auto_answer",
        "call_send_video",
        "ha_action",
    }
    for operation in sorted(required_operations - ALLOWED_OPERATIONS):
        errors.append(f"missing dial-plan operation {operation}")
    return errors
