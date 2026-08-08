from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import yaml

from qualification.registry import FIRMWARE_PROFILES
from scripts.candidate_lock import build_lock, candidate_id
from scripts.install_ha_qualification_package import install
from scripts.qualification_plan import build_plan
from scripts.merge_qualification_results import merge_results
from scripts.record_qualification_result import build_result
from scripts.verify_qualification import verify


def _candidate(head: str) -> dict[str, object]:
    payload = {
        "schema_version": 1,
        "repositories": {
            "esphome-intercom": {"commit": head, "dirty": False},
            "esphome-voip-stack": {"commit": "voip", "dirty": False},
            "esphome-audio-stack": {"commit": "audio", "dirty": False},
            "esphome-runtime-controller": {"commit": "runtime", "dirty": False},
        },
        "toolchain": {},
    }
    payload["candidate_id"] = candidate_id(payload)
    return payload


def _results(plan: dict[str, object], jobs: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "plan_id": plan["plan_id"],
        "candidate_id": _candidate(str(plan["head"]))["candidate_id"],
        "head": plan["head"],
        "jobs": jobs,
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


def test_ha_service_surface_requires_real_ha_without_hardware() -> None:
    plan = build_plan(
        ["custom_components/voip_stack/services.yaml"],
        base="base",
        head="head",
        full=False,
        event="pull-request",
    )

    assert plan["unknown_files"] == []
    assert plan["areas"] == ["ha_surface"]
    assert plan["required_jobs"] == ["ha-runtime", "software-full", "static"]
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

    errors, manifest = verify(
        plan,
        _candidate("head"),
        _results(plan, {}),
        artifact_root=tmp_path,
    )

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
    results = _results(
        plan,
        {
            "static": {
                "status": "success",
                "artifacts": [
                    {
                        "path": "static.json",
                        "sha256": hashlib.sha256(b"wrong").hexdigest(),
                        "bytes": evidence.stat().st_size,
                    }
                ],
            }
        },
    )

    errors, manifest = verify(
        plan, _candidate("other"), results, artifact_root=tmp_path
    )

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
    results = _results(
        plan,
        {
            "static": {
                "status": "success",
                "artifacts": [
                    {
                        "path": "static.json",
                        "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
                        "bytes": evidence.stat().st_size,
                    }
                ],
            }
        },
    )

    errors, manifest = verify(plan, _candidate("head"), results, artifact_root=tmp_path)

    assert errors == []
    assert manifest["qualified"] is True


def test_job_result_hashes_only_artifacts_below_root(tmp_path: Path) -> None:
    evidence = tmp_path / "nested" / "result.json"
    evidence.parent.mkdir()
    evidence.write_text("{}\n", encoding="utf-8")

    result = build_result(
        "static",
        "success",
        [evidence],
        tmp_path,
        plan_id="plan",
        candidate_id="candidate",
        head="head",
    )

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
    monkeypatch.setattr(
        "scripts.candidate_lock._package_version", lambda name: f"version-{name}"
    )
    monkeypatch.setattr(
        "scripts.candidate_lock._run", lambda *command, cwd=None: "v24.0.0"
    )
    config = {"repositories": {"repo": {"path": str(repository)}}}

    first = build_lock(config, allow_dirty=False)
    second = build_lock(config, allow_dirty=False)

    assert first == second
    assert len(first["candidate_id"]) == 64


def test_result_merge_rejects_duplicate_job(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    payload = {
        "schema_version": 1,
        "plan_id": "plan",
        "candidate_id": "candidate",
        "head": "head",
        "jobs": {"static": {"status": "success", "artifacts": []}},
    }
    first.write_text(json.dumps(payload), encoding="utf-8")
    second.write_text(json.dumps(payload), encoding="utf-8")

    try:
        merge_results([first, second])
    except RuntimeError as error:
        assert str(error) == "duplicate qualification jobs: static"
    else:
        raise AssertionError("duplicate qualification job was accepted")


def test_successful_result_requires_evidence(tmp_path: Path) -> None:
    try:
        build_result(
            "static",
            "success",
            [],
            tmp_path,
            plan_id="plan",
            candidate_id="candidate",
            head="head",
        )
    except RuntimeError as error:
        assert str(error) == "successful qualification job has no evidence: static"
    else:
        raise AssertionError("successful job without evidence was accepted")


def test_summary_rejects_result_identity_mismatch(tmp_path: Path) -> None:
    plan = build_plan(
        ["docs/qualification.md"],
        base="base",
        head="head",
        full=False,
        event="pull-request",
    )
    results = _results(plan, {})
    results["plan_id"] = "wrong"

    errors, _manifest = verify(
        plan,
        _candidate("head"),
        results,
        artifact_root=tmp_path,
    )

    assert "qualification results do not match plan" in errors


def test_workflow_wires_fail_closed_qualification_chain() -> None:
    workflow = (
        Path(__file__).parents[1] / ".github/workflows/qualification.yml"
    ).read_text(encoding="utf-8")

    for command in (
        "scripts/qualification_plan.py",
        "scripts/candidate_lock.py",
        "scripts/run_qualification_command.py",
        "scripts/merge_qualification_results.py",
        "scripts/verify_qualification.py",
    ):
        assert command in workflow
    assert "qualification-summary:" in workflow
    assert "if: always()" in workflow
    assert "if-no-files-found: error" in workflow
    assert "yaml_paths.sh --local" in workflow
    assert 'build_root="$RUNNER_TEMP/candidate"' in workflow
    assert (
        "working-directory: workspace/esphome-intercom\n        run: |\n          ./scripts/yaml_paths.sh --local"
        not in workflow
    )


def test_qualification_command_runs_as_script(tmp_path: Path) -> None:
    plan = build_plan(
        ["docs/qualification.md"],
        base="base",
        head="head",
        full=False,
        event="pull-request",
    )
    plan_path = tmp_path / "plan.json"
    candidate_path = tmp_path / "candidate.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    candidate_path.write_text(json.dumps(_candidate("head")), encoding="utf-8")

    subprocess.run(
        [
            sys.executable,
            "scripts/run_qualification_command.py",
            "--job",
            "static",
            "--plan",
            str(plan_path),
            "--candidate",
            str(candidate_path),
            "--evidence-root",
            str(tmp_path),
            "--",
            sys.executable,
            "-c",
            "print('qualified')",
        ],
        cwd=Path(__file__).parents[1],
        check=True,
    )

    result = json.loads((tmp_path / "result-static.json").read_text())
    assert result["jobs"]["static"]["status"] == "success"


def test_real_ha_automation_package_covers_route_decisions() -> None:
    package_path = (
        Path(__file__).parents[1]
        / "qualification/home_assistant/voip_qualification.yaml"
    )
    package = yaml.safe_load(package_path.read_text(encoding="utf-8"))

    options = package["input_select"]["voip_qualification_route_action"]["options"]
    assert set(options) == {
        "no_action",
        "default",
        "answer_ha",
        "decline",
        "busy",
        "cancel",
        "forward",
        "bridge",
    }
    automation = package["automation"][0]
    assert automation["triggers"] == [
        {
            "trigger": "state",
            "entity_id": "event.voip_stack_call",
        }
    ]
    route_action = next(
        action
        for action in automation["actions"]
        if action.get("action") == "voip_stack.route"
    )
    assert route_action["data"]["expected_sequence"] == "{{ selected_sequence }}"


def test_ha_package_installer_preserves_one_canonical_source(tmp_path: Path) -> None:
    config = tmp_path / "configuration.yaml"
    config.write_text(
        "homeassistant:\n  packages: !include_dir_named packages\n",
        encoding="utf-8",
    )

    target, digest = install(tmp_path, check=False)

    assert (
        target.read_bytes()
        == (
            Path(__file__).parents[1]
            / "qualification/home_assistant/voip_qualification.yaml"
        ).read_bytes()
    )
    assert len(digest) == 64
    assert install(tmp_path, check=True) == (target, digest)
