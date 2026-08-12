from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import sys

import pytest
import yaml

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
        "firmware_timeout_seconds": 30,
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
                "firmware": {
                    "profile": "waveshare-s3-full",
                    "mode": "install",
                    "command": [
                        sys.executable,
                        "-c",
                        "import os; assert os.path.isfile(os.environ['HIL_FIRMWARE_PATH'])",
                    ],
                },
                "scenarios": {
                    "esp-to-ha-answer-hangup": {
                        "command": [sys.executable, str(scenario)]
                    }
                },
            }
        },
    }


def _evidence(tmp_path: Path) -> dict[str, object]:
    candidate_bytes = b'{"candidate_id":"candidate"}'
    source_lock_sha256 = hashlib.sha256(candidate_bytes).hexdigest()
    firmware = tmp_path / "firmware/waveshare-s3-full/firmware.factory.bin"
    firmware.parent.mkdir(parents=True, exist_ok=True)
    firmware.write_bytes(b"candidate firmware")
    return {
        "candidate": {"candidate_id": "candidate"},
        "source_lock_sha256": source_lock_sha256,
        "firmware_manifest": {
            "schema_version": 2,
            "candidate_id": "candidate",
            "source_lock_sha256": source_lock_sha256,
            "firmware": [
                {
                    "profile": "waveshare-s3-full",
                    "artifact": {
                        "path": "firmware/waveshare-s3-full/firmware.factory.bin",
                        "sha256": hashlib.sha256(firmware.read_bytes()).hexdigest(),
                    },
                }
            ],
        },
        "firmware_root": tmp_path,
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
        run_hil(
            _plan("hil-s3"),
            hardware,
            environment=dict(os.environ),
            **_evidence(tmp_path),
        )


def test_non_positive_firmware_timeout_fails_closed(tmp_path: Path) -> None:
    scenario = tmp_path / "scenario.py"
    scenario.write_text("pass\n", encoding="utf-8")
    hardware = _hardware(tmp_path, scenario)
    hardware["firmware_timeout_seconds"] = 0

    with pytest.raises(HilError, match="snapshot timing must be positive"):
        run_hil(
            _plan("hil-s3"),
            hardware,
            environment=dict(os.environ),
            **_evidence(tmp_path),
        )


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
        **_evidence(tmp_path),
    )

    assert artifact["status"] == "passed"
    result = artifact["jobs"]["hil-s3"]["results"][0]
    assert result["snapshots"]["pre"]["call_scoped_quiescent"] is True
    assert result["snapshots"]["peak"]
    assert result["snapshots"]["post"]["call_scoped_quiescent"] is True
    assert result["snapshot_samples"] >= 1
    assert "private output" not in json.dumps(artifact)


def test_hardware_variant_can_install_attested_ota_artifact(tmp_path: Path) -> None:
    scenario = tmp_path / "scenario.py"
    scenario.write_text("pass\n", encoding="utf-8")
    hardware = _hardware(tmp_path, scenario)
    firmware = hardware["devices"]["ws3"]["firmware"]
    firmware["artifact_kind"] = "ota"
    firmware["command"] = [
        sys.executable,
        "-c",
        "import os; assert os.environ['HIL_FIRMWARE_PATH'].endswith('firmware.ota.bin')",
    ]
    evidence = _evidence(tmp_path)
    ota = tmp_path / "firmware/waveshare-s3-full/firmware.ota.bin"
    ota.write_bytes(b"candidate ota firmware")
    evidence["firmware_manifest"]["firmware"][0]["artifacts"] = [
        {
            "path": "firmware/waveshare-s3-full/firmware.ota.bin",
            "sha256": hashlib.sha256(ota.read_bytes()).hexdigest(),
        }
    ]

    artifact = run_hil(
        _plan("hil-s3"),
        hardware,
        environment=dict(os.environ),
        **evidence,
    )

    assert artifact["status"] == "passed"
    assert artifact["jobs"]["hil-s3"]["firmware"][0]["sha256"] == hashlib.sha256(
        ota.read_bytes()
    ).hexdigest()


def test_example_p4_variants_install_ota_artifacts() -> None:
    root = Path(__file__).parents[1]
    hardware = yaml.safe_load(
        (root / "qualification/hardware-map.example.yaml").read_text(
            encoding="utf-8"
        )
    )

    for variant in hardware["devices"]["p4"]["firmware_variants"]:
        assert variant["artifact_kind"] == "ota"
        assert variant["command"][:2] == [
            ".venv/bin/python",
            "scripts/install_esphome_ota.py",
        ]


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
        **_evidence(tmp_path),
    )

    assert artifact["status"] == "passed"
    assert set(artifact["jobs"]) == {"hil-s3"}


def test_one_device_runs_required_scenarios_on_sequential_firmware_variants(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("pass\n", encoding="utf-8")
    second.write_text("pass\n", encoding="utf-8")
    hardware = _hardware(tmp_path, first)
    device = hardware["devices"]["ws3"]
    install = device.pop("firmware")
    device.pop("scenarios")
    device["firmware_variants"] = [
        {
            **install,
            "scenarios": {
                "esp-to-ha-answer-hangup": {
                    "command": [sys.executable, str(first)]
                }
            },
        },
        {
            **install,
            "profile": "waveshare-s3-second",
            "scenarios": {"second-scenario": {"command": [sys.executable, str(second)]}},
        },
    ]
    evidence = _evidence(tmp_path)
    second_firmware = tmp_path / "firmware/waveshare-s3-second/firmware.factory.bin"
    second_firmware.parent.mkdir(parents=True)
    second_firmware.write_bytes(b"second candidate firmware")
    evidence["firmware_manifest"]["firmware"].append(
        {
            "profile": "waveshare-s3-second",
            "artifact": {
                "path": "firmware/waveshare-s3-second/firmware.factory.bin",
                "sha256": hashlib.sha256(second_firmware.read_bytes()).hexdigest(),
            },
        }
    )
    plan = _plan("hil-s3")
    plan["scenarios"].append({"id": "second-scenario", "executors": ["ws3"]})

    artifact = run_hil(
        plan,
        hardware,
        environment=dict(os.environ),
        **evidence,
    )

    job = artifact["jobs"]["hil-s3"]
    assert artifact["status"] == "passed"
    assert [item["profile"] for item in job["firmware"]] == [
        "waveshare-s3-full",
        "waveshare-s3-second",
    ]
    assert [item["firmware_profile"] for item in job["results"]] == [
        "waveshare-s3-full",
        "waveshare-s3-second",
    ]


def test_non_hardware_scenario_has_explicit_skip_reason(tmp_path: Path) -> None:
    scenario = tmp_path / "scenario.py"
    scenario.write_text("pass\n", encoding="utf-8")
    plan = _plan("hil-s3")
    plan["scenarios"].append({"id": "trunk-only", "executors": ["home-ha", "wildix"]})

    artifact = run_hil(
        plan,
        _hardware(tmp_path, scenario),
        environment=dict(os.environ),
        **_evidence(tmp_path),
    )

    skipped = artifact["jobs"]["hil-s3"]["skipped_scenarios"]
    assert skipped == {"trunk-only": "scenario does not require ws3 executor"}


def test_volume_above_one_percent_is_rejected(tmp_path: Path) -> None:
    scenario = tmp_path / "scenario.py"
    scenario.write_text("pass\n", encoding="utf-8")
    hardware = _hardware(tmp_path, scenario)
    hardware["devices"]["ws3"]["volume_percent"] = 2

    with pytest.raises(HilError, match="1 percent volume limit"):
        run_hil(
            _plan("hil-s3"),
            hardware,
            environment=dict(os.environ),
            **_evidence(tmp_path),
        )


def test_missing_required_scenario_is_not_silently_skipped(tmp_path: Path) -> None:
    scenario = tmp_path / "scenario.py"
    scenario.write_text("pass\n", encoding="utf-8")
    hardware = _hardware(tmp_path, scenario)
    hardware["devices"]["ws3"]["scenarios"] = {}

    with pytest.raises(HilError, match="required scenario"):
        run_hil(
            _plan("hil-s3"),
            hardware,
            environment=dict(os.environ),
            **_evidence(tmp_path),
        )


def test_failed_scenario_keeps_evidence_and_fails_job(tmp_path: Path) -> None:
    scenario = tmp_path / "scenario.py"
    scenario.write_text("raise SystemExit(7)\n", encoding="utf-8")

    artifact = run_hil(
        _plan("hil-s3"),
        _hardware(tmp_path, scenario),
        environment=dict(os.environ),
        **_evidence(tmp_path),
    )

    job = artifact["jobs"]["hil-s3"]
    assert artifact["status"] == "failed"
    assert job["status"] == "failed"
    assert job["results"][0]["exit_code"] == 7
    assert job["results"][0]["snapshots"]["post"]["call_scoped_quiescent"] is True


def test_scenario_timeout_terminates_process(tmp_path: Path) -> None:
    scenario = tmp_path / "scenario.py"
    scenario.write_text("import time\ntime.sleep(10)\n", encoding="utf-8")
    hardware = _hardware(tmp_path, scenario)
    hardware["devices"]["ws3"]["scenarios"]["esp-to-ha-answer-hangup"][
        "timeout_seconds"
    ] = 0.03

    artifact = run_hil(
        _plan("hil-s3"),
        hardware,
        environment=dict(os.environ),
        **_evidence(tmp_path),
    )

    result = artifact["jobs"]["hil-s3"]["results"][0]
    assert artifact["status"] == "failed"
    assert result["timed_out"] is True


def test_candidate_firmware_hash_mismatch_fails_before_scenario(tmp_path: Path) -> None:
    scenario = tmp_path / "scenario.py"
    scenario.write_text("raise AssertionError('must not run')\n", encoding="utf-8")
    evidence = _evidence(tmp_path)
    evidence["firmware_manifest"]["firmware"][0]["artifact"]["sha256"] = "0" * 64

    with pytest.raises(HilError, match="candidate firmware hash mismatch"):
        run_hil(
            _plan("hil-s3"),
            _hardware(tmp_path, scenario),
            environment=dict(os.environ),
            **evidence,
        )


def test_verify_mode_requires_exact_device_attestation(tmp_path: Path) -> None:
    scenario = tmp_path / "scenario.py"
    scenario.write_text("pass\n", encoding="utf-8")
    hardware = _hardware(tmp_path, scenario)
    hardware["devices"]["ws3"]["firmware"] = {
        "profile": "waveshare-s3-full",
        "mode": "verify",
        "command": [sys.executable, "-c", "print('{}')"],
    }

    with pytest.raises(HilError, match="firmware attestation mismatch"):
        run_hil(
            _plan("hil-s3"),
            hardware,
            environment=dict(os.environ),
            **_evidence(tmp_path),
        )
