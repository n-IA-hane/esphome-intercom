#!/usr/bin/env python3
"""Generate and validate the SIP intercom scenario catalog.

This is a planning harness, not a SIP runner. It gives the concrete scenario
IDs that the automated runners must implement with unit tests, simulator
scenarios, SIPp, PJSUA/baresip/manual checks, Playwright, and live ESP devices.
Catalog validation proves only that intended axes were listed. It is never
runtime qualification evidence and must not authorize a release.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sys
from typing import Iterable


TRANSPORTS = ("sip_udp", "sip_tcp")
ROUTES = ("direct", "ha_bridge", "ha_softphone", "explicit_uri", "name_via_ha")
DIRECTIONS = ("esp_to_esp", "esp_to_ha", "ha_to_esp", "pc_to_esp", "esp_to_pc")
AUDIO_ROLES = ("full_duplex", "mic_only", "speaker_only", "muted_mic", "muted_speaker")
FORMATS = (
    "16000:s16le:1:16",
    "16000:s16le:1:32",
    "16000:s16le:1:20",
    "48000:s16le:1:10",
    "16000:s24le:1:20",
)
FAILURES = (
    ("timeout_no_provisional", 408, "timeout"),
    ("dnd_busy", 486, "busy"),
    ("decline", 603, "declined"),
    ("cancel_before_answer", 487, "cancelled"),
    ("bye_remote", 200, "remote_hangup"),
    ("media_incompatible", 488, "media_incompatible"),
    ("auth_401", 401, "auth_required_unsupported"),
    ("auth_407", 407, "proxy_auth_required_unsupported"),
    ("endpoint_unreachable", 0, "transport_unreachable"),
)
RACES = (
    "incoming_while_ringing",
    "incoming_while_in_call",
    "double_invite_same_call_id",
    "simultaneous_hangup",
    "late_final_after_cancel",
    "rtp_after_terminal",
)

USAGE_ORIGINS = ("trunk", "esp", "ha_softphone", "registered_sip", "unregistered_sip")
INTERNAL_DESTINATIONS = (
    "ha_softphone",
    "esp",
    "registered_sip",
    "ring_group",
    "conference",
    "assist",
)
DIRECT_ENDPOINTS = ("ha_softphone", "esp", "registered_sip")
DESTINATION_STATES = (
    "manual_answer",
    "auto_answer",
    "decline",
    "dnd",
    "busy",
    "offline",
    "disabled",
    "caller_cancel",
    "timeout",
    "caller_hangup",
    "callee_hangup",
)
AUTOMATION_ACTIONS = ("no_action", "forward", "decline", "busy", "cancel")
RING_GROUP_CASES = (
    "available_answer",
    "available_auto_answer",
    "member_declines_other_answers",
    "simultaneous_answers",
    "caller_cancel",
    "all_decline",
    "timeout",
    "ha_dnd_esp_answers",
    "esp_dnd_ha_answers",
    "ha_busy_esp_answers",
    "esp_busy_ha_answers",
    "offline_member_other_answers",
    "all_dnd",
    "all_busy",
    "all_offline",
    "origin_member_excluded",
    "multi_contact_q_tiers",
    "sequential_tiers",
)
DTMF_METHODS = ("rtp_event", "sip_info")
DTMF_BRIDGES = (
    "trunk_to_ha",
    "trunk_to_esp",
    "esp_to_registered_sip",
    "ha_to_external",
    "ring_group_winner",
)


@dataclass(frozen=True, slots=True)
class Scenario:
    id: str
    category: str
    direction: str
    route: str
    caller_transport: str
    callee_transport: str
    audio_role: str
    tx_format: str
    rx_format: str
    expected_status: int
    terminal_reason: str
    tools: tuple[str, ...]
    assertions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UsageScenario:
    """One legal user-visible PBX behavior, independent of codec permutations."""

    id: str
    family: str
    origin: str
    destination: str
    selection: str
    destination_state: str
    expected: str
    layers: tuple[str, ...]
    assertions: tuple[str, ...]


def _usage(
    scenario_id: str,
    family: str,
    origin: str,
    destination: str,
    selection: str,
    destination_state: str,
    expected: str,
    *assertions: str,
    home_live: bool = False,
) -> UsageScenario:
    layers = ("unit", "ha_lab", "home_live") if home_live else ("unit", "ha_lab")
    return UsageScenario(
        scenario_id,
        family,
        origin,
        destination,
        selection,
        destination_state,
        expected,
        layers,
        (
            "single_generation",
            "single_terminal_transition",
            "all_resources_released",
            *assertions,
        ),
    )


def generate_usage_matrix() -> list[UsageScenario]:
    """Generate the exhaustive supported-use matrix before transport/codec expansion."""

    scenarios: list[UsageScenario] = []

    # Direct endpoint behavior. Browser OFFLINE is intentionally ringable;
    # SIP/ESP OFFLINE has no reachable Contact and returns 480.
    for origin in ("trunk", "esp", "registered_sip", "unregistered_sip"):
        for destination in DIRECT_ENDPOINTS:
            for state in DESTINATION_STATES:
                if state in {"manual_answer", "auto_answer"}:
                    expected = "200_connected"
                elif state == "decline":
                    expected = "603_declined"
                elif state == "dnd":
                    expected = "486_dnd"
                elif state == "busy":
                    expected = "486_busy"
                elif state == "offline":
                    expected = (
                        "180_logical_ringing"
                        if destination == "ha_softphone"
                        else "480_target_unreachable"
                    )
                elif state == "disabled":
                    expected = "target_disabled"
                elif state == "caller_cancel":
                    expected = "487_cancelled"
                elif state == "timeout":
                    expected = "408_timeout"
                else:
                    expected = "200_then_bye"
                scenarios.append(
                    _usage(
                        f"direct-{origin}-{destination}-{state}",
                        "direct_call",
                        origin,
                        destination,
                        "canonical_dialplan",
                        state,
                        expected,
                        "origin_and_ingress_stable",
                        "endpoint_state_matches_sip",
                        home_live=origin in {"trunk", "esp"},
                    )
                )

    # Inbound selection priority. Explicit trunk DTMF wins over automation;
    # registered and unknown SIP peers use the central dial plan without an
    # automation wait, while a trusted ESP may offer the bounded override.
    for destination in INTERNAL_DESTINATIONS:
        for origin in ("trunk", "esp"):
            scenarios.append(
                _usage(
                    f"selection-{origin}-{destination}-automation-off",
                    "initial_routing",
                    origin,
                    destination,
                    "automation_off_fallback",
                    "available",
                    "canonical_destination",
                    "no_route_request",
                    home_live=True,
                )
            )
            for action in AUTOMATION_ACTIONS:
                expected = {
                    "no_action": "fallback_destination",
                    "forward": "canonical_override_destination",
                    "decline": "603_declined",
                    "busy": "486_busy",
                    "cancel": "487_cancelled",
                }[action]
                scenarios.append(
                    _usage(
                        f"selection-{origin}-{destination}-automation-{action}",
                        "initial_routing",
                        origin,
                        destination,
                        f"automation_{action}",
                        "available",
                        expected,
                        "bounded_route_request",
                        "fallback_preserved",
                        home_live=True,
                    )
                )
        for origin in ("registered_sip", "unregistered_sip"):
            scenarios.append(
                _usage(
                    f"selection-{origin}-{destination}-automation-bypass",
                    "initial_routing",
                    origin,
                    destination,
                    "automation_bypass",
                    "available",
                    "canonical_destination",
                    "no_route_request",
                )
            )

    for destination in ("ha_softphone", "esp", "registered_sip", "assist"):
        scenarios.append(
            _usage(
                f"dtmf-selection-trunk-{destination}-valid",
                "preanswer_dtmf",
                "trunk",
                destination,
                "valid_extension_automation_ignored",
                "available",
                "canonical_destination",
                "one_digit_event_per_key",
                "dtmf_precedes_automation",
                home_live=True,
            )
        )
    for destination in INTERNAL_DESTINATIONS:
        scenarios.append(
            _usage(
                f"dtmf-selection-trunk-{destination}-no-digits",
                "preanswer_dtmf",
                "trunk",
                destination,
                "no_digits_then_automation_or_fallback",
                "available",
                "selected_or_fallback_destination",
                "one_route_request",
                home_live=True,
            )
        )
    scenarios.append(
        _usage(
            "dtmf-selection-trunk-unknown-extension",
            "preanswer_dtmf",
            "trunk",
            "unknown",
            "unknown_extension",
            "available",
            "404_route_not_found",
            "preanswer_media_released",
            home_live=True,
        )
    )

    for origin in ("trunk", "esp", "ha_softphone", "registered_sip"):
        for case in RING_GROUP_CASES:
            expected = {
                "all_dnd": "486_dnd",
                "all_busy": "486_busy",
                "all_offline": "480_target_unreachable",
                "all_decline": "603_declined",
                "caller_cancel": "487_cancelled",
                "timeout": "408_timeout",
            }.get(case, "first_answer_wins")
            scenarios.append(
                _usage(
                    f"ring-group-{origin}-{case}",
                    "ring_group",
                    origin,
                    "ring_group",
                    "canonical_fork",
                    case,
                    expected,
                    "origin_member_never_rings",
                    "losers_cancelled",
                    home_live=origin in {"trunk", "esp", "ha_softphone"},
                )
            )

    for origin in ("ha_softphone", "esp", "registered_sip"):
        for trunk_state in ("registered", "unavailable"):
            scenarios.append(
                _usage(
                    f"external-{origin}-{trunk_state}",
                    "external_call",
                    origin,
                    "external",
                    "trunk",
                    trunk_state,
                    "trunk_dialog" if trunk_state == "registered" else "503_trunk_unavailable",
                    "public_number_preserved",
                    home_live=True,
                )
            )

    for bridge in DTMF_BRIDGES:
        for source_leg in ("caller", "callee"):
            for method in DTMF_METHODS:
                scenarios.append(
                    _usage(
                        f"in-call-dtmf-{bridge}-{source_leg}-{method}",
                        "in_call_dtmf",
                        bridge.split("_to_", 1)[0],
                        bridge.split("_to_", 1)[-1],
                        method,
                        source_leg,
                        "event_only_no_reroute",
                        "canonical_event_v2",
                        "digit_preserved",
                        home_live=bridge.startswith("trunk_") or bridge.startswith("ha_"),
                    )
                )

    return scenarios


def validate_usage_matrix(scenarios: Iterable[UsageScenario]) -> list[str]:
    scenarios = list(scenarios)
    errors: list[str] = []
    ids = [scenario.id for scenario in scenarios]
    if len(ids) != len(set(ids)):
        errors.append("duplicate usage scenario ids")

    def has(**wanted: str) -> bool:
        return any(
            all(getattr(scenario, key) == value for key, value in wanted.items())
            for scenario in scenarios
        )

    for origin in USAGE_ORIGINS:
        if not has(origin=origin):
            errors.append(f"missing usage origin {origin}")
    for destination in (*INTERNAL_DESTINATIONS, "external"):
        if not has(destination=destination):
            errors.append(f"missing usage destination {destination}")
    for state in DESTINATION_STATES:
        if not has(destination_state=state):
            errors.append(f"missing destination state {state}")
    for case in RING_GROUP_CASES:
        if not has(family="ring_group", destination_state=case):
            errors.append(f"missing ring group case {case}")
    for method in DTMF_METHODS:
        if not has(family="in_call_dtmf", selection=method):
            errors.append(f"missing in-call DTMF method {method}")
    required_ids = {
        "selection-trunk-ha_softphone-automation-off",
        "selection-trunk-ring_group-automation-forward",
        "dtmf-selection-trunk-unknown-extension",
        "ring-group-esp-ha_dnd_esp_answers",
        "ring-group-esp-all_dnd",
        "ring-group-trunk-origin_member_excluded",
        "external-ha_softphone-unavailable",
    }
    missing = sorted(required_ids.difference(ids))
    errors.extend(f"missing required usage scenario {item}" for item in missing)
    if any("ha_lab" not in scenario.layers for scenario in scenarios):
        errors.append("every usage scenario must run in the HA laboratory")
    if any("single_terminal_transition" not in scenario.assertions for scenario in scenarios):
        errors.append("every usage scenario must assert one terminal transition")
    return errors


def _slug(value: str) -> str:
    return value.replace(":", "_").replace(";", "_").replace("-", "_")


def _call_tools(direction: str, route: str) -> tuple[str, ...]:
    tools = ["pytest"]
    if direction in ("pc_to_esp", "esp_to_pc"):
        tools.append("external_softphone")
    if route in ("direct", "explicit_uri", "name_via_ha", "ha_bridge"):
        tools.append("sipp")
    if route in ("ha_softphone", "ha_bridge", "name_via_ha"):
        tools.append("ha_local")
    if direction in ("ha_to_esp", "esp_to_ha"):
        tools.append("playwright")
    return tuple(dict.fromkeys(tools))


def _base_assertions(route: str) -> tuple[str, ...]:
    assertions = [
        "SipPhoneState.state",
        "SipPhoneState.call_id",
        "SipPhoneState.caller",
        "SipPhoneState.callee",
        "SipPhoneState.sip_transport",
        "SipPhoneState.selected_tx_format",
        "SipPhoneState.selected_rx_format",
        "SipPhoneState.rtp_counters",
        "terminal_reason",
        "info_log_lifecycle",
        "debug_sip_trace",
    ]
    if route == "direct":
        assertions.append("ha_bridge_not_used")
    if route in ("ha_bridge", "name_via_ha"):
        assertions.append("ha_bridge_used")
    if route == "ha_softphone":
        assertions.append("ha_card_mirror")
    return tuple(assertions)


def generate_matrix() -> list[Scenario]:
    scenarios: list[Scenario] = []

    for direction in DIRECTIONS:
        for route in ROUTES:
            if direction == "esp_to_esp" and route == "ha_softphone":
                continue
            if direction in ("esp_to_ha", "ha_to_esp") and route == "direct":
                continue
            if direction in ("pc_to_esp", "esp_to_pc") and route == "ha_softphone":
                continue
            for caller_transport in TRANSPORTS:
                for callee_transport in TRANSPORTS:
                    for audio_role in AUDIO_ROLES:
                        for fmt in FORMATS:
                            scenario_id = "-".join(
                                (
                                    "ok",
                                    direction,
                                    route,
                                    caller_transport,
                                    callee_transport,
                                    audio_role,
                                    _slug(fmt),
                                )
                            )
                            scenarios.append(
                                Scenario(
                                    id=scenario_id,
                                    category="successful_call",
                                    direction=direction,
                                    route=route,
                                    caller_transport=caller_transport,
                                    callee_transport=callee_transport,
                                    audio_role=audio_role,
                                    tx_format=fmt,
                                    rx_format=fmt,
                                    expected_status=200,
                                    terminal_reason="",
                                    tools=_call_tools(direction, route),
                                    assertions=_base_assertions(route),
                                )
                            )

    for failure, status, reason in FAILURES:
        for caller_transport in TRANSPORTS:
            for callee_transport in TRANSPORTS:
                for route in ("direct", "ha_bridge", "ha_softphone", "name_via_ha"):
                    scenario_id = "-".join((failure, route, caller_transport, callee_transport))
                    scenarios.append(
                        Scenario(
                            id=scenario_id,
                            category="failure",
                            direction="esp_to_esp" if route != "ha_softphone" else "esp_to_ha",
                            route=route,
                            caller_transport=caller_transport,
                            callee_transport=callee_transport,
                            audio_role="full_duplex",
                            tx_format=FORMATS[0],
                            rx_format=FORMATS[0],
                            expected_status=status,
                            terminal_reason=reason,
                            tools=tuple(dict.fromkeys(("pytest", "sipp", "ha_local"))),
                            assertions=(
                                "SipPhoneState.terminal_reason",
                                "sip_status_code",
                                "caller_terminal_screen",
                                "callee_terminal_screen",
                                "info_log_lifecycle",
                                "debug_sip_trace",
                            ),
                        )
                    )

    for race in RACES:
        for route in ("direct", "ha_bridge", "ha_softphone"):
            scenarios.append(
                Scenario(
                    id=f"race-{race}-{route}",
                    category="race",
                    direction="esp_to_esp" if route != "ha_softphone" else "esp_to_ha",
                    route=route,
                    caller_transport="sip_udp",
                    callee_transport="sip_udp",
                    audio_role="full_duplex",
                    tx_format=FORMATS[0],
                    rx_format=FORMATS[0],
                    expected_status=0,
                    terminal_reason="protocol_error" if race == "double_invite_same_call_id" else "",
                    tools=tuple(dict.fromkeys(("pytest", "simulator", "sipp"))),
                    assertions=(
                        "single_terminal_transition",
                        "no_late_media_promotion",
                        "no_duplicate_dialog",
                        "debug_sip_trace",
                    ),
                )
            )

    return scenarios


def validate_matrix(scenarios: Iterable[Scenario]) -> list[str]:
    scenarios = list(scenarios)
    errors: list[str] = []
    ids = [scenario.id for scenario in scenarios]
    if len(ids) != len(set(ids)):
        errors.append("duplicate scenario ids")

    def has(**wanted: str) -> bool:
        return any(all(getattr(s, key) == value for key, value in wanted.items()) for s in scenarios)

    for caller in TRANSPORTS:
        for callee in TRANSPORTS:
            if not has(caller_transport=caller, callee_transport=callee):
                errors.append(f"missing transport pair {caller}->{callee}")
    for route in ROUTES:
        if not has(route=route):
            errors.append(f"missing route {route}")
    for direction in DIRECTIONS:
        if not has(direction=direction):
            errors.append(f"missing direction {direction}")
    for role in AUDIO_ROLES:
        if not has(audio_role=role):
            errors.append(f"missing audio role {role}")
    for fmt in FORMATS:
        if not has(tx_format=fmt):
            errors.append(f"missing format {fmt}")
    for failure, status, reason in FAILURES:
        if not any(s.id.startswith(failure) and s.expected_status == status and s.terminal_reason == reason for s in scenarios):
            errors.append(f"missing failure {failure}")
    for race in RACES:
        if not any(s.id.startswith(f"race-{race}-") for s in scenarios):
            errors.append(f"missing race {race}")
    if not any("playwright" in s.tools and "ha_card_mirror" in s.assertions for s in scenarios):
        errors.append("missing Playwright HA card coverage")
    if not any("external_softphone" in s.tools for s in scenarios):
        errors.append("missing external softphone media coverage")
    return errors


def summary(scenarios: list[Scenario]) -> dict[str, object]:
    by_category: dict[str, int] = {}
    by_tool: dict[str, int] = {}
    for scenario in scenarios:
        by_category[scenario.category] = by_category.get(scenario.category, 0) + 1
        for tool in scenario.tools:
            by_tool[tool] = by_tool.get(tool, 0) + 1
    usage = generate_usage_matrix()
    usage_families: dict[str, int] = {}
    usage_layers: dict[str, int] = {}
    for scenario in usage:
        usage_families[scenario.family] = usage_families.get(scenario.family, 0) + 1
        for layer in scenario.layers:
            usage_layers[layer] = usage_layers.get(layer, 0) + 1
    return {
        "count": len(scenarios),
        "categories": dict(sorted(by_category.items())),
        "tools": dict(sorted(by_tool.items())),
        "transports": TRANSPORTS,
        "routes": ROUTES,
        "directions": DIRECTIONS,
        "audio_roles": AUDIO_ROLES,
        "formats": FORMATS,
        "failures": [name for name, _status, _reason in FAILURES],
        "races": RACES,
        "usage": {
            "count": len(usage),
            "families": dict(sorted(usage_families.items())),
            "layers": dict(sorted(usage_layers.items())),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print full matrix as JSON")
    parser.add_argument("--summary", action="store_true", help="print matrix summary as JSON")
    parser.add_argument("--write-json", type=Path, help="write full matrix to this path")
    parser.add_argument("--validate", action="store_true", help="validate coverage and exit non-zero on gaps")
    args = parser.parse_args(argv)

    scenarios = generate_matrix()
    errors = validate_matrix(scenarios)
    errors.extend(validate_usage_matrix(generate_usage_matrix()))

    if args.write_json:
        args.write_json.parent.mkdir(parents=True, exist_ok=True)
        args.write_json.write_text(
            json.dumps([asdict(s) for s in scenarios], indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if args.json:
        print(json.dumps([asdict(s) for s in scenarios], indent=2, sort_keys=True))
    if args.summary or not args.json:
        print(json.dumps(summary(scenarios), indent=2, sort_keys=True))
    if args.validate and errors:
        for error in errors:
            print(f"matrix error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
