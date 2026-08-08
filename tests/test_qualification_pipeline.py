from __future__ import annotations

import hashlib
import json
from pathlib import Path

from qualification.registry import FIRMWARE_PROFILES
from scripts.candidate_lock import build_lock
from scripts.qualification_plan import build_plan
from scripts.record_qualification_result import build_result
from scripts.verify_qualification import verify


def _candidate(head: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "repositories": {
            "esphome-intercom": {"commit": head, "dirty": False},
            "esphome-voip-stack": {"commit": "voip", "dirty": False},
            "esphome-audio-stack": {"commit": "audio", "dirty": False},
            "esphome-runtime-controller": {"commit": "runtime", "dirty": False},
        },
        "toolchain": {},
    }


def test_docs_only_plan_selects_economic_gate() -> None:
    plan = build_plan(
        ["docs/qualification.md"],
        base="base",
        head="head",
        full=False,
        event="pull-request",
    )

    assert plan["areas"] == ["documentation"]
    assert plan["required_jobs"] == ["static"]
    assert plan["firmware_profiles"] == []


def test_lifecycle_change_requires_real_qualification() -> None:
    plan = build_plan(
        ["custom_components/voip_stack/endpoint_termination.py"],
        base="base",
        head="head",
        full=False,
        event="pull-request",
    )

    assert set(plan["required_jobs"]) >= {
        "software-full",
        "ha-runtime",
        "peer-live",
        "hil-s3",
    }
    assert any(
        scenario["id"] == "registered-sip-to-esp-bidirectional-hangup"
        for scenario in plan["scenarios"]
    )


def test_push_to_dev_selects_complete_firmware_matrix() -> None:
    plan = build_plan(
        ["README.md"],
        base="base",
        head="head",
        full=False,
        event="push-dev",
    )

    assert plan["full"] is True
    assert {profile["id"] for profile in plan["firmware_profiles"]} == {
        profile.id for profile in FIRMWARE_PROFILES
    }


def test_unknown_path_fails_closed_to_full_plan() -> None:
    plan = build_plan(
        ["new-subsystem/unknown.py"],
        base="base",
        head="head",
        full=False,
        event="pull-request",
    )

    assert plan["full"] is True
    assert plan["unknown_files"] == ["new-subsystem/unknown.py"]


def test_summary_rejects_missing_required_job(tmp_path: Path) -> None:
    plan = build_plan(
        ["docs/qualification.md"],
        base="base",
        head="head",
        full=False,
        event="pull-request",
    )

    errors, manifest = verify(plan, _candidate("head"), {"jobs": {}}, artifact_root=tmp_path)

    assert errors == ["required job is missing: static"]
    assert manifest["qualified"] is False


def test_summary_rejects_wrong_candidate_and_artifact(tmp_path: Path) -> None:
    plan = build_plan(
        ["docs/qualification.md"],
        base="base",
        head="head",
        full=False,
        event="pull-request",
    )
    evidence = tmp_path / "static.json"
    evidence.write_text("{}\n", encoding="utf-8")
    results = {
        "jobs": {
            "static": {
                "status": "success",
                "artifacts": [
                    {"path": "static.json", "sha256": hashlib.sha256(b"wrong").hexdigest()}
                ],
            }
        }
    }

    errors, manifest = verify(plan, _candidate("other"), results, artifact_root=tmp_path)

    assert "candidate intercom commit does not match plan head" in errors
    assert "job artifact hash mismatch: static/static.json" in errors
    assert manifest["qualified"] is False


def test_summary_accepts_exact_candidate_and_evidence(tmp_path: Path) -> None:
    plan = build_plan(
        ["docs/qualification.md"],
        base="base",
        head="head",
        full=False,
        event="pull-request",
    )
    evidence = tmp_path / "static.json"
    evidence.write_text(json.dumps({"passed": True}) + "\n", encoding="utf-8")
    results = {
        "jobs": {
            "static": {
                "status": "success",
                "artifacts": [
                    {
                        "path": "static.json",
                        "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
                    }
                ],
            }
        }
    }

    errors, manifest = verify(plan, _candidate("head"), results, artifact_root=tmp_path)

    assert errors == []
    assert manifest["qualified"] is True


def test_job_result_hashes_only_artifacts_below_root(tmp_path: Path) -> None:
    evidence = tmp_path / "nested" / "result.json"
    evidence.parent.mkdir()
    evidence.write_text("{}\n", encoding="utf-8")

    result = build_result("static", "success", [evidence], tmp_path)

    artifact = result["jobs"]["static"]["artifacts"][0]
    assert artifact["path"] == "nested/result.json"
    assert artifact["sha256"] == hashlib.sha256(evidence.read_bytes()).hexdigest()


def test_candidate_lock_has_stable_identity(monkeypatch, tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    monkeypatch.setattr(
        "scripts.candidate_lock._repository",
        lambda path: {"commit": f"sha-{path.name}", "dirty": False},
    )
    monkeypatch.setattr("scripts.candidate_lock._package_version", lambda name: f"version-{name}")
    monkeypatch.setattr("scripts.candidate_lock._run", lambda *command, cwd=None: "v24.0.0")
    config = {"repositories": {"repo": {"path": str(repository)}}}

    first = build_lock(config, allow_dirty=False)
    second = build_lock(config, allow_dirty=False)

    assert first == second
    assert len(first["candidate_id"]) == 64
