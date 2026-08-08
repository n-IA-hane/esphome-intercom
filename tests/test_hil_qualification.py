from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pytest

from scripts.run_hil_qualification import HilError, _lab_lock, run_hil


def _plan(*jobs: str) -> dict[str, object]:
    return {
        "plan_id": "plan",
        "head": "head",
        "required_jobs": list(jobs),
        "scenarios": [
            {
                "id": "esp-to-ha-answer-hangup",
                "executors": ["ha-lab", "ws3"],
            }
        ],
    }


def _hardware(tmp_path: Path, scenario: Path) -> dict[str, object]:
    snapshot = tmp_path / "snapshot.py"
    snapshot.write_text(
        "import json\nprint(json.dumps({'state': 'idle', "
        "'active_dialogs': 0, 'call_scoped_quiescent': True, "
        "'resource_counts': {'tasks': 0}}))\n",
        encoding="utf-8",
    )
    return {
        "schema_version": 1,
        "lock_file": str(tmp_path / "lab.lock"),
        "snapshot": {
            "command": [sys.executable, str(snapshot)],
            "interval_seconds": 0.01,
            "timeout_seconds": 2,
        },
        "devices": {
            "ws3": {
                "enabled": True,
                "capabilities": ["hil-s3", "ws3", "audio"],
                "volume_percent": 1,
                "doctor": [sys.executable, "-c", "raise SystemExit(0)"],
                "scenarios": {
                    "esp-to-ha-answer-hangup": {
                        "command": [sys.executable, str(scenario)]
                    }
                },
            }
        },
    }


def test_plan_without_hardware_requirement_is_motivated_skip(tmp_path: Path) -> None:
    artifact = run_hil(
        _plan("static"),
        {"schema_version": 1},
        environment=dict(os.environ),
    )

    assert artifact["status"] == "skipped"
    assert artifact["skip_reason"] == "qualification plan does not require hardware"


def test_required_hardware_without_device_fails_closed(tmp_path: Path) -> None:
    hardware = {
        "schema_version": 1,
        "lock_file": str(tmp_path / "lab.lock"),
        "snapshot": {"command": ["true"]},
        "devices": {},
    }

    with pytest.raises(HilError, match="exactly one enabled device"):
        run_hil(_plan("hil-s3"), hardware, environment=dict(os.environ))


def test_lab_lock_rejects_parallel_owner(tmp_path: Path) -> None:
    lock = tmp_path / "lab.lock"

    with _lab_lock(lock), pytest.raises(HilError, match="already reserved"):
        with _lab_lock(lock):
            pass


def test_required_scenario_collects_pre_peak_post_evidence(tmp_path: Path) -> None:
    scenario = tmp_path / "scenario.py"
    scenario.write_text(
        "import os, time\n"
        "assert float(os.environ['VOIP_TEST_VOLUME_PERCENT']) <= 1\n"
        "print('private output is hashed, not copied')\n"
        "time.sleep(0.05)\n",
        encoding="utf-8",
    )

    artifact = run_hil(
        _plan("hil-s3"),
        _hardware(tmp_path, scenario),
        environment=dict(os.environ),
    )

    assert artifact["status"] == "passed"
    result = artifact["jobs"]["hil-s3"]["scenarios"][0]
    assert result["snapshots"]["pre"]["call_scoped_quiescent"] is True
    assert result["snapshots"]["peak"]
    assert result["snapshots"]["post"]["call_scoped_quiescent"] is True
    assert result["snapshot_samples"] >= 1
    assert "private output" not in json.dumps(artifact)


def test_selected_hardware_job_does_not_execute_other_required_jobs(
    tmp_path: Path,
) -> None:
    scenario = tmp_path / "scenario.py"
    scenario.write_text("pass\n", encoding="utf-8")

    artifact = run_hil(
        _plan("hil-s3", "hil-p4"),
        _hardware(tmp_path, scenario),
        environment=dict(os.environ),
        selected_job="hil-s3",
    )

    assert artifact["status"] == "passed"
    assert set(artifact["jobs"]) == {"hil-s3"}


def test_non_hardware_scenario_has_explicit_skip_reason(tmp_path: Path) -> None:
    scenario = tmp_path / "scenario.py"
    scenario.write_text("pass\n", encoding="utf-8")
    plan = _plan("hil-s3")
    plan["scenarios"].append({"id": "trunk-only", "executors": ["home-ha", "wildix"]})

    artifact = run_hil(
        plan,
        _hardware(tmp_path, scenario),
        environment=dict(os.environ),
    )

    skipped = artifact["jobs"]["hil-s3"]["skipped_scenarios"]
    assert skipped == {"trunk-only": "scenario does not require ws3 executor"}


def test_volume_above_one_percent_is_rejected(tmp_path: Path) -> None:
    scenario = tmp_path / "scenario.py"
    scenario.write_text("pass\n", encoding="utf-8")
    hardware = _hardware(tmp_path, scenario)
    hardware["devices"]["ws3"]["volume_percent"] = 2

    with pytest.raises(HilError, match="1 percent volume limit"):
        run_hil(_plan("hil-s3"), hardware, environment=dict(os.environ))


def test_missing_required_scenario_is_not_silently_skipped(tmp_path: Path) -> None:
    scenario = tmp_path / "scenario.py"
    scenario.write_text("pass\n", encoding="utf-8")
    hardware = _hardware(tmp_path, scenario)
    hardware["devices"]["ws3"]["scenarios"] = {}

    with pytest.raises(HilError, match="required scenario"):
        run_hil(_plan("hil-s3"), hardware, environment=dict(os.environ))


def test_failed_scenario_keeps_evidence_and_fails_job(tmp_path: Path) -> None:
    scenario = tmp_path / "scenario.py"
    scenario.write_text("raise SystemExit(7)\n", encoding="utf-8")

    artifact = run_hil(
        _plan("hil-s3"),
        _hardware(tmp_path, scenario),
        environment=dict(os.environ),
    )

    job = artifact["jobs"]["hil-s3"]
    assert artifact["status"] == "failed"
    assert job["status"] == "failed"
    assert job["scenarios"][0]["exit_code"] == 7
    assert job["scenarios"][0]["snapshots"]["post"]["call_scoped_quiescent"] is True


def test_scenario_timeout_terminates_process(tmp_path: Path) -> None:
    scenario = tmp_path / "scenario.py"
    scenario.write_text("import time\ntime.sleep(10)\n", encoding="utf-8")
    hardware = _hardware(tmp_path, scenario)
    hardware["devices"]["ws3"]["scenarios"]["esp-to-ha-answer-hangup"][
        "timeout_seconds"
    ] = 0.03

    artifact = run_hil(_plan("hil-s3"), hardware, environment=dict(os.environ))

    result = artifact["jobs"]["hil-s3"]["scenarios"][0]
    assert artifact["status"] == "failed"
    assert result["timed_out"] is True
