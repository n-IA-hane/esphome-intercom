"""Producer API invariants that cannot be established by legacy report import."""
from __future__ import annotations

import time

import pytest

from tools.hil_measurement import HilMeasurement, MeasurementError, sensitive_native_line
from tools.p4_hil_measurement import P4HilMeasurement


def environment(tmp_path):
    return {"HIL_EVIDENCE_ROOT": str(tmp_path), "HIL_MEASUREMENT_RECEIPT": str(tmp_path / "current.json"),
            "HIL_PLAN_ID": "plan", "HIL_HEAD": "head", "HIL_CANDIDATE_ID": "candidate", "HIL_RUN_ID": "new-run",
            "HIL_SCENARIO_ID": "esp-to-ha-answer-hangup", "HIL_ACTOR_STARTED_MONOTONIC": str(time.monotonic() - 1),
            "HIL_ACTOR_STARTED_UNIX": str(time.time() - 1)}


def test_observations_freeze_nested_native_data_and_cannot_be_backdated(tmp_path):
    actor = HilMeasurement.from_environment(environment(tmp_path))
    snapshot = {"resource_counts": {"media_tasks": 0}}
    event = actor.observe("runtime", snapshot=snapshot)
    snapshot["resource_counts"]["media_tasks"] = 10
    assert event["snapshot"]["resource_counts"]["media_tasks"] == 0
    with pytest.raises(MeasurementError, match="cannot be backdated"):
        actor.observe("runtime", at=0)


def test_session_start_uses_its_actual_observation_clock(tmp_path):
    actor = HilMeasurement.from_environment(environment(tmp_path))
    call = actor.start_session(caller="a", callee="b", terminal="a", route_kind="actual")
    assert call.data["started_at"] == actor.events[-1]["at"]
    call.connected()
    with pytest.raises(MeasurementError, match="already recorded"):
        call.connected()
    with pytest.raises(MeasurementError, match="Unfinished"):
        actor.finish()
    assert not actor.output.exists()


def test_p4_missing_site_configuration_fails_without_starting_any_device_owner(tmp_path):
    with pytest.raises(MeasurementError, match="HIL_DEVICE_MAC"):
        P4HilMeasurement(environment(tmp_path))


@pytest.mark.parametrize("line", ["Authorization: Digest omitted", "PASSWORD=omitted", "nonce: omitted", "secret = omitted"])
def test_receipt_native_filter_omits_sensitive_fields(line):
    assert sensitive_native_line(line)


def test_receipt_native_filter_preserves_call_identity_and_negotiation():
    assert not sensitive_native_line("Incoming call (call_id=owned) SDP L16/16000 JPEG/90000")
