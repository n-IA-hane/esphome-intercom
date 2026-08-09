from __future__ import annotations

from qualification.dialplan_matrix import USE_CASES, validate_use_cases


def test_dialplan_use_case_matrix_is_coherent() -> None:
    assert validate_use_cases() == []


def test_common_presence_and_forward_scenarios_are_qualified() -> None:
    cases = {case.id: case for case in USE_CASES}
    assert cases["known-caller-by-presence"].qualification == (
        "conditional-route-real-ha"
    )
    assert cases["no-answer-next-room"].qualification == (
        "ringing-forward-real-ha"
    )


def test_every_case_has_a_negative_or_fallback_outcome() -> None:
    assert all(case.false_outcome for case in USE_CASES)
