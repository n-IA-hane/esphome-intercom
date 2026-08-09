from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import yaml

from qualification.registry import FIRMWARE_PROFILES, regression_ledger
from qualification.evidence import EvidenceError, derive_scenario_evidence
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
    assert plan["required_jobs"] == [
        "ha-runtime",
        "peer-live",
        "software-full",
        "static",
    ]
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
        "firmware",
        "hil-s3",
    }
    assert "waveshare-s3-full" in {
        profile["id"] for profile in plan["firmware_profiles"]
    }
    assert any(
        scenario["id"] == "registered-sip-to-esp-bidirectional-hangup"
        for scenario in plan["scenarios"]
    )
    assert "browser-real" in plan["required_jobs"]
    assert {record["id"] for record in plan["regressions"]} == {
        "issue-85",
        "issue-88",
        "issue-93",
        "issue-94",
        "issue-95",
    }


def test_community_regression_ledger_has_executable_scenarios() -> None:
    records = regression_ledger()

    assert {record["id"] for record in records} == {
        "issue-85",
        "issue-88",
        "issue-93",
        "issue-94",
        "issue-95",
    }
    assert all(record["scenarios"] for record in records)


def test_software_evidence_claims_only_mapped_replay_contracts(
    tmp_path: Path,
) -> None:
    plan = build_plan(
        ["custom_components/voip_stack/sip_listener.py"],
        base="base",
        head="head",
        full=False,
        event="pull-request",
    )
    log = tmp_path / "software-full.log"
    log.write_text("1498 passed\n", encoding="utf-8")

    claims = derive_scenario_evidence("software-full", plan, [log])

    assert {claim["scenario_id"] for claim in claims} == {
        "dahua-interop-contract-replay",
        "fritzbox-pcma-to-assist-frame-reassembly",
    }
    assert not any(
        claim["scenario_id"] == "trunk-dtmf-routing-and-established-dtmf"
        for claim in claims
    )


def test_hil_evidence_requires_exact_passed_scenario_and_quiescence(
    tmp_path: Path,
) -> None:
    plan = build_plan(
        ["custom_components/voip_stack/endpoint_termination.py"],
        base="base",
        head="head",
        full=False,
        event="pull-request",
    )
    artifact = tmp_path / "hil-s3.json"
    artifact.write_text(
        json.dumps(
            {
                "jobs": {
                    "hil-s3": {
                        "status": "passed",
                        "results": [
                            {
                                "scenario": "esp-to-ha-answer-hangup",
                                "status": "passed",
                                "snapshots": {"post": {"call_scoped_quiescent": True}},
                            },
                            {
                                "scenario": "esp-to-esp-watchdog-and-bidirectional-hangup",
                                "status": "passed",
                                "snapshots": {"post": {"call_scoped_quiescent": True}},
                            },
                        ],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    claims = derive_scenario_evidence("hil-s3", plan, [artifact])

    assert {claim["scenario_id"] for claim in claims} == {
        "esp-to-ha-answer-hangup",
        "esp-to-esp-watchdog-and-bidirectional-hangup",
    }

    payload = json.loads(artifact.read_text(encoding="utf-8"))
    payload["jobs"]["hil-s3"]["results"][0]["snapshots"]["post"][
        "call_scoped_quiescent"
    ] = False
    artifact.write_text(json.dumps(payload), encoding="utf-8")
    try:
        derive_scenario_evidence("hil-s3", plan, [artifact])
    except EvidenceError as error:
        assert "did not prove planned scenario" in str(error)
    else:
        raise AssertionError("non-quiescent HIL evidence was accepted")


def test_browser_evidence_rejects_a_failed_real_matrix(tmp_path: Path) -> None:
    plan = build_plan(
        ["custom_components/voip_stack/sip_listener.py"],
        base="base",
        head="head",
        full=False,
        event="pull-request",
    )
    artifact = tmp_path / "browser-matrix.json"
    artifact.write_text(
        json.dumps([{"name": "in_call_rfc4733_dtmf_event", "status": "fail"}]),
        encoding="utf-8",
    )

    try:
        derive_scenario_evidence("browser-real", plan, [artifact])
    except EvidenceError as error:
        assert str(error) == "browser-real produced no supported scenario artifact"
    else:
        raise AssertionError("failed browser matrix was accepted as evidence")


def test_peer_evidence_requires_exact_live_matrix_scenario_ids(
    tmp_path: Path,
) -> None:
    plan = build_plan(
        ["custom_components/voip_stack/endpoint_termination.py"],
        base="base",
        head="head",
        full=False,
        event="pull-request",
    )
    matrix = tmp_path / "peer-live.json"
    matrix.write_text(
        json.dumps(
            {
                "results": [
                    {"name": name, "status": "passed"}
                    for name in (
                        "browser_phone_auto_answer_enabled_ha_runtime",
                        "browser_phone_auto_answer_disabled_ha_runtime",
                        "browser_phone_dnd_enabled",
                        "browser_phone_dnd_disabled",
                        "stale_route_sequence_is_rejected",
                        "concurrent_route_requests_remain_distinct",
                    )
                ]
            }
        ),
        encoding="utf-8",
    )
    dtmf = tmp_path / "dtmf-extension-precedence.json"
    dtmf.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "name": "dtmf_assist_extension_bypasses_automation",
                        "status": "pass",
                    },
                    {
                        "name": "dtmf_secondary_extension_bypasses_automation",
                        "status": "pass",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    claims = derive_scenario_evidence("peer-live", plan, [matrix, dtmf])

    assert {
        claim["scenario_id"] for claim in claims
    }.issuperset(
        {
            "ha-phone-policy-and-dnd-routing",
            "inbound-route-decision-guards",
            "trunk-dtmf-routing-and-established-dtmf",
        }
    )

    dtmf.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "name": "dtmf_assist_extension_bypasses_automation",
                        "status": "pass",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    try:
        derive_scenario_evidence("peer-live", plan, [matrix, dtmf])
    except EvidenceError as error:
        assert "did not prove exact scenarios" in str(error)
        assert "dtmf_secondary_extension_bypasses_automation" in str(error)
    else:
        raise AssertionError("partial DTMF evidence was accepted")


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


def _scenario_plan() -> dict[str, object]:
    plan = build_plan(
        ["docs/qualification.md"],
        base="base",
        head="head",
        full=False,
        event="pull-request",
    )
    plan["required_jobs"] = ["software-full", "static"]
    plan["scenarios"] = [
        {
            "id": "dahua-interop-contract-replay",
            "areas": ["ha_lifecycle", "sip_core"],
            "executors": ["software-replay"],
            "oracles": [
                "digest-challenge",
                "negotiated-pcm",
                "tcp-flow-state",
                "teardown-state",
            ],
            "postconditions": ["tcp-flow-reused", "terminal-idempotent"],
            "regressions": ["issue-85"],
        }
    ]
    plan["regressions"] = [
        record for record in regression_ledger() if record["id"] == "issue-85"
    ]
    from scripts.qualification_plan import plan_id

    plan["plan_id"] = plan_id(plan)
    return plan


def _job_results(tmp_path: Path, plan: dict[str, object]) -> dict[str, object]:
    jobs: dict[str, object] = {}
    for job in plan["required_jobs"]:
        artifact = tmp_path / f"{job}.json"
        artifact.write_text("{}\n", encoding="utf-8")
        jobs[job] = {
            "status": "success",
            "artifacts": [
                {
                    "path": artifact.name,
                    "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                    "bytes": artifact.stat().st_size,
                }
            ],
        }
    return _results(plan, jobs)


def test_summary_requires_all_planned_scenario_evidence(tmp_path: Path) -> None:
    plan = _scenario_plan()
    results = _job_results(tmp_path, plan)

    errors, manifest = verify(plan, _candidate("head"), results, artifact_root=tmp_path)

    assert any(error.startswith("planned scenario lacks executors") for error in errors)
    assert any(error.startswith("planned scenario lacks oracles") for error in errors)
    assert any(
        error.startswith("planned scenario lacks postconditions") for error in errors
    )
    assert manifest["scenarios"]["dahua-interop-contract-replay"]["complete"] is False


def test_summary_accepts_scenario_evidence_from_owning_jobs(tmp_path: Path) -> None:
    plan = _scenario_plan()
    results = _job_results(tmp_path, plan)
    results["scenario_evidence"] = [
        {
            "scenario_id": "dahua-interop-contract-replay",
            "job": "software-full",
            "status": "passed",
            "executors": ["software-replay"],
            "oracles": [
                "digest-challenge",
                "negotiated-pcm",
                "tcp-flow-state",
                "teardown-state",
            ],
            "postconditions": ["tcp-flow-reused", "terminal-idempotent"],
        },
    ]

    errors, manifest = verify(plan, _candidate("head"), results, artifact_root=tmp_path)

    assert errors == []
    assert manifest["scenarios"]["dahua-interop-contract-replay"]["complete"] is True


def test_summary_rejects_executor_claimed_by_wrong_job(tmp_path: Path) -> None:
    plan = _scenario_plan()
    results = _job_results(tmp_path, plan)
    results["scenario_evidence"] = [
        {
            "scenario_id": "dahua-interop-contract-replay",
            "job": "static",
            "status": "passed",
            "executors": ["software-replay"],
            "oracles": [],
            "postconditions": [],
        }
    ]

    errors, _manifest = verify(
        plan, _candidate("head"), results, artifact_root=tmp_path
    )

    assert (
        "scenario evidence is claimed by an unrelated job: "
        "dahua-interop-contract-replay/static"
    ) in errors


def test_summary_rejects_claim_not_observed_by_owning_job(tmp_path: Path) -> None:
    plan = build_plan(
        ["docs/qualification.md"],
        base="base",
        head="head",
        full=False,
        event="pull-request",
    )
    plan["required_jobs"] = ["ha-runtime", "static"]
    plan["scenarios"] = [
        {
            "id": "esp-to-ha-answer-hangup",
            "areas": [],
            "executors": ["ha-lab", "playwright", "sipp", "ws3"],
            "oracles": [
                "browser-state",
                "esp-state",
                "ha-state",
                "rtp-duplex",
                "sip-trace",
            ],
            "postconditions": [
                "cleanup-barrier",
                "immediate-redial",
                "resources-at-baseline",
                "single-terminal",
            ],
            "regressions": ["issue-93"],
        }
    ]
    plan["regressions"] = [
        record for record in regression_ledger() if record["id"] == "issue-93"
    ]
    from scripts.qualification_plan import plan_id

    plan["plan_id"] = plan_id(plan)
    results = _job_results(tmp_path, plan)
    results["scenario_evidence"] = [
        {
            "scenario_id": "esp-to-ha-answer-hangup",
            "job": "ha-runtime",
            "status": "passed",
            "executors": [],
            "oracles": ["ha-state", "rtp-duplex"],
            "postconditions": [],
        }
    ]

    errors, _manifest = verify(
        plan, _candidate("head"), results, artifact_root=tmp_path
    )

    assert (
        "scenario evidence exceeds contract: esp-to-ha-answer-hangup/oracles"
    ) in errors


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


def test_job_result_binds_scenario_evidence_to_its_job(tmp_path: Path) -> None:
    evidence = tmp_path / "peer.json"
    evidence.write_text("{}\n", encoding="utf-8")

    result = build_result(
        "peer-live",
        "success",
        [evidence],
        tmp_path,
        plan_id="plan",
        candidate_id="candidate",
        head="head",
        scenario_evidence=[
            {
                "scenario_id": "call",
                "status": "passed",
                "executors": ["sipp"],
                "oracles": ["sip-trace"],
                "postconditions": [],
            }
        ],
    )

    assert result["scenario_evidence"][0]["job"] == "peer-live"


def test_result_merge_preserves_disjoint_scenario_evidence(tmp_path: Path) -> None:
    paths = []
    for job, executor in (("peer-live", "sipp"), ("hil-s3", "ws3")):
        path = tmp_path / f"{job}.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "plan_id": "plan",
                    "candidate_id": "candidate",
                    "head": "head",
                    "jobs": {job: {"status": "success", "artifacts": []}},
                    "scenario_evidence": [
                        {
                            "scenario_id": "call",
                            "job": job,
                            "status": "passed",
                            "executors": [executor],
                            "oracles": [],
                            "postconditions": [],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        paths.append(path)

    merged = merge_results(paths)

    assert [claim["job"] for claim in merged["scenario_evidence"]] == [
        "peer-live",
        "hil-s3",
    ]


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
    assert "scripts/run_peer_live_qualification.sh" in workflow
    assert "VOIP_QUALIFICATION_POLICY_ENDPOINT_ID" in workflow
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
    choice = next(action for action in automation["actions"] if "choose" in action)
    route_action = choice["default"][0]
    assert route_action["action"] == "voip_stack.route"
    assert route_action["data"]["expected_sequence"] == "{{ selected_sequence }}"
    forward_action = choice["choose"][0]["sequence"][0]
    assert forward_action["action"] == "voip_stack.select_inbound_destination"
    assert forward_action["data"]["expected_sequence"] == "{{ selected_sequence }}"


def test_real_ha_package_uses_one_context_branch_and_one_forward_automation() -> None:
    package_path = (
        Path(__file__).parents[1]
        / "qualification/home_assistant/voip_qualification.yaml"
    )
    package = yaml.safe_load(package_path.read_text(encoding="utf-8"))
    automations = {item["id"]: item for item in package["automation"]}

    route = automations["voip_qualification_route_decision"]
    rendered = yaml.safe_dump(route)
    assert "voip_qualification_condition" in rendered
    assert "voip_qualification_false_route_action" in rendered

    forward = automations["voip_qualification_ringing_forward"]
    assert forward["triggers"] == [
        {
            "trigger": "state",
            "entity_id": "sensor.casa_call_state",
            "to": "ringing",
            "for": {"seconds": 1},
        }
    ]
    assert any(
        action.get("action") == "voip_stack.forward" for action in forward["actions"]
    )
    assert "sensor.voip_stack_call_state" not in yaml.safe_dump(package)


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
