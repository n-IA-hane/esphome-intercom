from __future__ import annotations

from pathlib import Path

from qualification.dialplan_matrix import (
    QUALIFICATION_CLASSES,
    USE_CASES,
    validate_use_cases,
)


def test_dialplan_use_case_matrix_is_coherent() -> None:
    assert validate_use_cases() == []


def test_common_presence_and_forward_scenarios_are_qualified() -> None:
    cases = {case.id: case for case in USE_CASES}
    assert cases["known-caller-by-presence"].qualification == (
        "conditional-route-real-ha"
    )
    assert cases["no-answer-next-room"].qualification == ("ringing-forward-real-ha")


def test_every_case_has_a_negative_or_fallback_outcome() -> None:
    assert all(case.false_outcome for case in USE_CASES)


def test_every_qualification_class_names_an_existing_executor() -> None:
    root = Path(__file__).resolve().parents[1]
    assert {case.qualification for case in USE_CASES} == set(QUALIFICATION_CLASSES)
    assert all(
        (root / evidence.executor).is_file()
        for evidence in QUALIFICATION_CLASSES.values()
    )


def test_live_dialplan_classes_bind_exact_artifact_scenarios() -> None:
    assert QUALIFICATION_CLASSES["guarded-route-real-ha"].scenario_ids == (
        "stale_route_sequence_is_rejected",
        "concurrent_route_requests_remain_distinct",
    )
    assert QUALIFICATION_CLASSES["trunk-dtmf-live"].scenario_ids == (
        "dtmf_assist_extension_bypasses_automation",
        "dtmf_secondary_extension_bypasses_automation",
    )
    assert all(
        scenario.endswith("_ha_runtime")
        for scenario in QUALIFICATION_CLASSES[
            "phone-policy-ha-runtime"
        ].scenario_ids
    )
