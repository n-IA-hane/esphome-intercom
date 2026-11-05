from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
import struct
import subprocess
import sys

import pytest
import yaml

from qualification.registry import FIRMWARE_PROFILES, SCENARIOS, regression_ledger
from qualification.evidence import (
    CLAIMS,
    EvidenceError,
    derive_scenario_evidence,
    hil_artifact_paths,
    validate_claim_contracts,
)
from scripts.candidate_lock import build_lock, candidate_id
from scripts.install_ha_qualification_package import install
from scripts.qualification_plan import build_plan
from scripts.merge_qualification_results import merge_results
from scripts.record_qualification_result import build_result
from scripts.verify_qualification import verify


def test_regular_environment_contains_live_websocket_client() -> None:
    requirements = (
        Path(__file__).parents[1] / "requirements-test.txt"
    ).read_text(encoding="utf-8")

    assert "websockets==17.0" in requirements.splitlines()


def test_browser_gate_sanitizes_baresip_artifact_names() -> None:
    gate = (
        Path(__file__).parents[1] / "scripts/run_lab_regression_gate.sh"
    ).read_text(encoding="utf-8")

    assert 'find "$CAPTURE_DIR" -type f -print0' in gate
    assert "tr ':<>\"|*?' '_______'" in gate


def _candidate(head: str) -> dict[str, object]:
    payload = {
        "schema_version": 1,
        "repositories": {
            "esphome-intercom": {"commit": head, "dirty": False},
            "esphome-voip-stack": {"commit": "voip", "dirty": False},
            "esphome-audio-stack": {"commit": "audio", "dirty": False},
            "esphome-runtime-controller": {"commit": "runtime", "dirty": False},
            "esphome-esp-video-camera": {"commit": "camera", "dirty": False},
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


def test_every_scenario_contract_is_producible_by_owned_executors() -> None:
    validate_claim_contracts()


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
        "software-full",
        "static",
    ]
    assert plan["skipped_jobs"]["peer-live"] == (
        "requires trusted push to dev or workflow dispatch"
    )
    assert plan["firmware_profiles"] == []


def test_lifecycle_change_requires_real_qualification() -> None:
    plan = build_plan(
        ["custom_components/voip_stack/endpoint_termination.py"],
        base="base",
        head="head",
        full=False,
        event="pull-request",
    )

    assert set(plan["required_jobs"]) >= {"software-full", "ha-runtime", "firmware"}
    assert not set(plan["required_jobs"]).intersection(
        {"peer-live", "browser-real", "hil-s3", "hil-p4"}
    )
    assert "waveshare-s3-full" in {
        profile["id"] for profile in plan["firmware_profiles"]
    }
    assert not any(
        scenario["id"] == "registered-sip-to-esp-bidirectional-hangup"
        for scenario in plan["scenarios"]
    )
    assert {record["id"] for record in plan["regressions"]} == {
        "issue-85",
        "issue-94",
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
    receipt = tmp_path / "software-full-command.json"
    receipt.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "job": "software-full",
                "status": "success",
                "returncode": 0,
            }
        ),
        encoding="utf-8",
    )

    claims = derive_scenario_evidence("software-full", plan, [receipt])

    assert {claim["scenario_id"] for claim in claims} == {
        "dahua-interop-contract-replay",
        "fritzbox-register-contract-replay",
        "fritzbox-pcma-to-assist-frame-reassembly",
    }
    assert not any(
        claim["scenario_id"] == "trunk-dtmf-routing-and-established-dtmf"
        for claim in claims
    )


def test_nonempty_log_is_not_structured_software_evidence(tmp_path: Path) -> None:
    plan = build_plan(
        ["custom_components/voip_stack/sip_listener.py"],
        base="base",
        head="head",
        full=False,
        event="pull-request",
    )
    log = tmp_path / "software-full.log"
    log.write_text("all tests passed\n", encoding="utf-8")

    with pytest.raises(EvidenceError, match="no supported scenario artifact"):
        derive_scenario_evidence("software-full", plan, [log])


def test_hil_state_only_result_cannot_attest_duplex_or_redial(tmp_path: Path) -> None:
    plan = {
        "plan_id": "plan", "head": "head",
        "scenarios": [{"id": "esp-to-ha-answer-hangup"}],
    }
    artifact = tmp_path / "hil-s3.json"
    artifact.write_text(json.dumps({"jobs": {"hil-s3": {
        "status": "passed", "results": [{
            "scenario": "esp-to-ha-answer-hangup", "status": "passed",
            "exit_code": 0,
            "snapshots": {"post": {"call_scoped_quiescent": True}},
        }],
    }}}), encoding="utf-8")

    with pytest.raises(EvidenceError, match="did not prove planned scenario"):
        derive_scenario_evidence("hil-s3", plan, [artifact])


def write_measured_hil_fixture(root: Path, **identity) -> dict:
    """Synthetic wire observations for gate tests, never a physical HIL result."""
    from tools.rtp_pcap_evidence import analyze_rows

    def reference(path, raw):
        path.write_bytes(raw)
        return {"path": str(path.relative_to(root)), "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest()}

    shared_payload = identity.pop("shared_payload", False)
    receipt = {"schema_version": 1, "plan_id": "plan", "head": "head",
               "candidate_id": "candidate", "scenario": "esp-to-ha-answer-hangup",
               "run_id": "run", **identity, "sessions": []}
    time_origin = receipt.pop("time_origin", 0.0)
    for number in range(2):
        call_id = f"call-{number}"
        rows = []
        capture = bytearray(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
        for direction, (src, dst, sport, dport) in enumerate((
            (b"\xc0\x00\x02\x01", b"\xc0\x00\x02\x02", 40000, 40002),
            (b"\xc0\x00\x02\x02", b"\xc0\x00\x02\x01", 40002, 40000),
        )):
            ssrc = 100 + number * 2 + direction
            for packet in range(2001):
                payload_type = 120 if shared_payload and packet == 1000 else 96
                payload = struct.pack("!BBH",1,0x8a,160) if payload_type==120 else bytes(320)
                rtp = struct.pack("!BBHII", 0x80, payload_type|(0x80 if payload_type==120 else 0), packet, packet * 160, ssrc) + payload
                udp = struct.pack("!HHHH", sport, dport, 8 + len(rtp), 0) + rtp
                ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 0, 0, 64, 17, 0, src, dst)
                frame = bytes(12) + b"\x08\x00" + ip + udp
                capture.extend(struct.pack("<IIII", number * 23 + 1 + packet // 100,
                    (packet % 100) * 10000, len(frame), len(frame)) + frame)
                rows.append("\t".join((str(number * 23 + 1 + packet * .01),
                    ".".join(map(str, src)), str(sport), ".".join(map(str, dst)), str(dport),
                    hex(ssrc), str(payload_type), str(packet), str(packet * 160))))
        streams = analyze_rows(rows)
        for stream in streams:
            stream.update(media_type="telephone-event" if stream["payload_type"]==120 else "audio", clock_rate=16000, expected_delta_ms=10.0)
        capture_ref = reference(root / f"{receipt['run_id']}-{call_id}.pcap", capture)
        report = {"status": "passed", "failures": [], "pcap_sha256": capture_ref["sha256"],
                  "call_id": call_id, "run_id": receipt["run_id"], "streams": streams}
        report_ref = reference(root / f"{receipt['run_id']}-{call_id}-rtp.json", json.dumps(report).encode())
        receipt["sessions"].append({"call_id": call_id, "started_at": number * 23,
            "connected_at": number * 23 + 1, "ended_at": number * 23 + 22,
            "quiescent_at": number * 23 + 22.5, "states": ["in_call", "idle"],
            "capture": capture_ref, "rtp_report": report_ref,
            **{direction: {**{key: stream[key] for key in ("source", "destination", "ssrc", "payload_type")},
                           "clock_rate": 16000, "ptime_ms": 10.0}
               for direction, stream in zip(("tx", "rx"), [s for s in streams if s["media_type"]=="audio"], strict=True)}})
        for key in ("started_at", "connected_at", "ended_at", "quiescent_at"):
            receipt["sessions"][-1][key] += time_origin
    return receipt


def _measured_hil_artifact(root: Path, receipt: dict) -> Path:
    path = root / "measurement.json"
    raw = json.dumps(receipt).encode()
    path.write_bytes(raw)
    artifact = root / "hil-s3.json"
    artifact.write_text(json.dumps({"plan_id": receipt["plan_id"], "head": receipt["head"], "candidate_id": receipt["candidate_id"],
        "jobs": {"hil-s3": {"status": "passed", "results": [{
            "scenario": "esp-to-ha-answer-hangup", "run_id": "run", "status": "passed",
            "started_monotonic": 0.0, "finished_monotonic": 46.0,
            "snapshots": {stage: {"call_scoped_quiescent": True, "state": "idle",
                "active_dialogs": 0, "resource_counts": {"tasks": 0}}
                for stage in ("pre", "post")},
            "measurement_receipt": {"path": path.name, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)},
        }]}}}), encoding="utf-8")
    return artifact


def test_bound_transport_and_new_call_receipt_can_attest_hil(tmp_path: Path) -> None:
    artifact = _measured_hil_artifact(tmp_path, write_measured_hil_fixture(tmp_path))
    claims = derive_scenario_evidence("hil-s3",
        {"plan_id": "plan", "head": "head", "scenarios": [{"id": "esp-to-ha-answer-hangup"}]},
        [artifact], candidate_id="candidate")
    assert claims[0]["oracles"] == ("esp-state", "rtp-duplex")
    assert "immediate-redial" in claims[0]["postconditions"]


@pytest.mark.parametrize("fault",[None,"shared_loss","missing_ledger","unscoped_none"])
def test_hil_shared_payload_loss_cannot_be_coerced_or_ignored(tmp_path: Path, fault):
    receipt=write_measured_hil_fixture(tmp_path,shared_payload=True)
    if fault:
        reference=receipt["sessions"][0]["rtp_report"]
        path=tmp_path/reference["path"]
        report=json.loads(path.read_bytes())
        first=report["streams"][0]
        assert first["lost_packets"] is None
        if fault=="shared_loss":
            first["sequence_ledger"]["lost_packets"]=1
        elif fault=="missing_ledger":
            first.pop("sequence_ledger")
        else:
            first.pop("loss_scope")
        raw=json.dumps(report).encode()
        path.write_bytes(raw)
        reference.update(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
    artifact=_measured_hil_artifact(tmp_path,receipt)
    def derive():
        return derive_scenario_evidence("hil-s3",{"plan_id":"plan","head":"head","scenarios":[{"id":"esp-to-ha-answer-hangup"}]},
                                       [artifact],candidate_id="candidate")
    if fault:
        with pytest.raises(EvidenceError,match="did not prove planned scenario"):
            derive()
    else:
        assert "rtp-duplex" in derive()[0]["oracles"]


@pytest.mark.parametrize("fault", [None, "changed_lifetime", "active_relay", "negative", "boolean"])
def test_hil_call_resources_zero_and_lifetime_tasks_match_baseline(tmp_path: Path, fault) -> None:
    artifact = _measured_hil_artifact(tmp_path, write_measured_hil_fixture(tmp_path))
    payload = json.loads(artifact.read_bytes())
    snapshots = payload["jobs"]["hil-s3"]["results"][0]["snapshots"]
    for snapshot in snapshots.values():
        snapshot["resource_counts"].update(runtime_tasks=1, relays=0)
    post = snapshots["post"]["resource_counts"]
    if fault == "changed_lifetime":
        post["runtime_tasks"] = 2
    elif fault == "active_relay":
        post["relays"] = 1
    elif fault == "negative":
        post["runtime_tasks"] = -1
    elif fault == "boolean":
        post["runtime_tasks"] = True
    artifact.write_text(json.dumps(payload))
    plan = {"plan_id": "plan", "head": "head", "scenarios": [{"id": "esp-to-ha-answer-hangup"}]}
    if fault is None:
        assert derive_scenario_evidence("hil-s3", plan, [artifact], candidate_id="candidate")
    else:
        with pytest.raises(EvidenceError, match="did not prove planned scenario"):
            derive_scenario_evidence("hil-s3", plan, [artifact], candidate_id="candidate")


def test_measured_hil_inputs_are_included_in_final_job_hashes(tmp_path: Path) -> None:
    artifact = _measured_hil_artifact(tmp_path, write_measured_hil_fixture(tmp_path))
    measured = hil_artifact_paths([artifact], tmp_path)
    result = build_result("hil-s3", "success", [artifact, *measured], tmp_path,
        plan_id="plan", candidate_id="candidate", head="head")
    records = result["jobs"]["hil-s3"]["artifacts"]
    assert len(records) == 6  # Wrapper, receipt and both call-scoped capture/report pairs.
    assert all(record["sha256"] == hashlib.sha256((tmp_path / record["path"]).read_bytes()).hexdigest()
        for record in records)


def _hil_verification_inputs(root: Path, *, measured: bool, include_inputs=True):
    from scripts.qualification_plan import plan_id

    plan = build_plan(["docs/qualification.md"], base="base", head="head", full=False, event="manual")
    scenario = next(item for item in SCENARIOS if item.id == "esp-to-ha-answer-hangup")
    plan["scenarios"] = json.loads(json.dumps([asdict(scenario)], default=sorted))
    jobs = [job for (name, job) in CLAIMS if name == scenario.id]
    plan["required_jobs"] = jobs
    plan["regressions"] = [item for item in regression_ledger() if scenario.id in item["scenarios"]]
    plan["plan_id"] = plan_id(plan)
    candidate = _candidate("head")
    results = _job_results(root, plan)
    results["scenario_evidence"] = [
        {"scenario_id": scenario.id, "job": job, "status": "passed",
         **{key: list(value) for key, value in CLAIMS[(scenario.id, job)].items()}}
        for job in jobs
    ]
    if measured:
        artifact = _measured_hil_artifact(root, write_measured_hil_fixture(root,
            candidate_id=candidate["candidate_id"], plan_id=plan["plan_id"]))
        paths = [artifact, *hil_artifact_paths([artifact], root)] if include_inputs else [artifact]
        results["jobs"]["hil-s3"] = build_result("hil-s3", "success", paths, root,
            plan_id=plan["plan_id"], candidate_id=candidate["candidate_id"], head="head")["jobs"]["hil-s3"]
    return plan, candidate, results


def test_final_verifier_rejects_hil_claim_strings_without_measurements(tmp_path: Path) -> None:
    plan, candidate, results = _hil_verification_inputs(tmp_path, measured=False)
    errors, manifest = verify(plan, candidate, results, artifact_root=tmp_path)
    assert manifest["qualified"] is False
    assert any("measured HIL evidence is invalid" in error for error in errors)


def test_final_verifier_accepts_bound_measured_hil_inputs(tmp_path: Path) -> None:
    plan, candidate, results = _hil_verification_inputs(tmp_path, measured=True)
    errors, manifest = verify(plan, candidate, results, artifact_root=tmp_path)
    assert errors == []
    assert manifest["qualified"] is True  # Synthetic transport fixture, no hardware claim.


def test_final_verifier_requires_every_measured_input_in_job_hashes(tmp_path: Path) -> None:
    plan, candidate, results = _hil_verification_inputs(tmp_path, measured=True, include_inputs=False)
    errors, manifest = verify(plan, candidate, results, artifact_root=tmp_path)
    assert manifest["qualified"] is False
    assert "measured HIL inputs are missing from job artifact hashes: hil-s3" in errors


def test_final_verifier_rejects_capture_symlink_outside_evidence_root(tmp_path: Path) -> None:
    plan, candidate, results = _hil_verification_inputs(tmp_path, measured=True)
    capture = next(tmp_path.glob("*.pcap"))
    outside = tmp_path.parent / (tmp_path.name + "-outside.pcap")
    outside.write_bytes(capture.read_bytes())
    capture.unlink()
    capture.symlink_to(outside)
    errors, manifest = verify(plan, candidate, results, artifact_root=tmp_path)
    assert manifest["qualified"] is False
    assert any("escapes evidence root" in error for error in errors)


def test_hil_script_result_preserves_inputs_through_final_verification(tmp_path: Path) -> None:
    plan, candidate, results = _hil_verification_inputs(tmp_path, measured=True)
    plan_path, candidate_path = tmp_path / "plan.json", tmp_path / "candidate.json"
    plan_path.write_text(json.dumps(plan))
    candidate_path.write_text(json.dumps(candidate))
    subprocess.run([sys.executable, str(Path(__file__).parents[1] / "scripts/run_qualification_command.py"),
        "--job", "hil-s3", "--plan", str(plan_path), "--candidate", str(candidate_path),
        "--evidence-root", str(tmp_path), "--artifact", str(tmp_path / "hil-s3.json"),
        "--", sys.executable, "-c", "print('synthetic fixture command')"], check=True, capture_output=True)
    recorded = json.loads((tmp_path / "result-hil-s3.json").read_bytes())
    results["jobs"]["hil-s3"] = recorded["jobs"]["hil-s3"]
    errors, manifest = verify(plan, candidate, results, artifact_root=tmp_path)
    assert len(recorded["jobs"]["hil-s3"]["artifacts"]) == 8
    assert errors == []
    assert manifest["qualified"] is True


@pytest.mark.parametrize("scenario", [
    "registered-sip-to-esp-bidirectional-hangup",
    "esp-to-esp-watchdog-and-bidirectional-hangup",
    "p4-audio-to-bidirectional-video-reinvite",
    "p4-full-landscape-jpeg-call-lifecycle",
])
def test_other_hil_properties_require_their_own_measured_validator(scenario: str) -> None:
    job = "hil-p4" if scenario.startswith("p4-") else "hil-s3"
    with pytest.raises(EvidenceError, match="no measured validator"):
        derive_scenario_evidence(job,
            {"plan_id": "plan", "head": "head", "scenarios": [{"id": scenario}]},
            [], candidate_id="candidate")


def test_explicit_claim_file_cannot_bypass_hil_measurements(tmp_path: Path) -> None:
    command = Path(__file__).parents[1] / "scripts/run_qualification_command.py"
    marker = tmp_path / "command-must-not-run"
    result = subprocess.run([sys.executable, str(command), "--job", "hil-s3",
        "--plan", str(tmp_path / "plan.json"), "--candidate", str(tmp_path / "candidate.json"),
        "--evidence-root", str(tmp_path), "--scenario-evidence", str(tmp_path / "forged.json"),
        "--", sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
        capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert "cannot supply HIL claims" in result.stderr
    assert not marker.exists()


@pytest.mark.parametrize("fault", [
    "missing_rx", "old_call_id", "no_redial", "wrong_candidate", "wrong_scenario",
    "old_run", "wrong_report_call", "changed_capture", "changed_report", "reused_capture",
    "counter_only", "wrong_format", "redial_before_cleanup", "delayed_redial", "path_escape",
    "short_window", "hidden_loss", "old_report_run", "one_way", "changed_receipt",
    "boolean_duration",
])
def test_hil_rejects_unmeasured_or_uncorrelated_transport(tmp_path: Path, fault: str) -> None:
    receipt = write_measured_hil_fixture(tmp_path)
    first, second = receipt["sessions"]
    if fault == "missing_rx":
        first.pop("rx")
    elif fault == "old_call_id":
        second["call_id"] = first["call_id"]
    elif fault == "no_redial":
        receipt["sessions"].pop()
    elif fault in {"wrong_candidate", "wrong_scenario", "old_run"}:
        receipt[{"wrong_candidate": "candidate_id", "wrong_scenario": "scenario", "old_run": "run_id"}[fault]] = "other"
    elif fault == "wrong_report_call":
        first["rtp_report"] = second["rtp_report"]
    elif fault in {"changed_capture", "changed_report"}:
        (tmp_path / first["capture" if fault == "changed_capture" else "rtp_report"]["path"]).write_bytes(b"changed")
    elif fault == "reused_capture":
        second["capture"] = first["capture"]
    elif fault == "counter_only":
        first["rtp_report"] = {"tx_packets": 100, "rx_packets": 100}
    elif fault == "wrong_format":
        first["rx"]["clock_rate"] = 48000
    elif fault == "redial_before_cleanup":
        second["started_at"] = first["quiescent_at"] - .1
    elif fault == "delayed_redial":
        second.update(started_at=100, connected_at=101, ended_at=109, quiescent_at=110)
    elif fault == "path_escape":
        first["capture"]["path"] = "../outside.pcap"
    elif fault == "one_way":
        first["rx"] = first["tx"]
    elif fault in {"short_window", "hidden_loss", "old_report_run", "boolean_duration"}:
        altered = second if fault == "boolean_duration" else first
        path = tmp_path / altered["rtp_report"]["path"]
        report = json.loads(path.read_bytes())
        if fault == "short_window":
            for stream in report["streams"]:
                stream["duration_seconds"] = .02
        elif fault == "hidden_loss":
            report["streams"][0]["lost_packets"] = 1
        elif fault == "boolean_duration":
            report["streams"][0]["duration_seconds"] = True
        else:
            report["run_id"] = "previous-run"
        raw = json.dumps(report).encode()
        path.write_bytes(raw)
        altered["rtp_report"].update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    artifact = _measured_hil_artifact(tmp_path, receipt)
    if fault == "changed_receipt":
        (tmp_path / "measurement.json").write_text("{}")
    with pytest.raises(EvidenceError, match="did not prove planned scenario"):
        derive_scenario_evidence("hil-s3",
            {"plan_id": "plan", "head": "head", "scenarios": [{"id": "esp-to-ha-answer-hangup"}]},
            [artifact], candidate_id="candidate")


def test_hil_evidence_requires_exact_passed_scenario_and_quiescence(
    tmp_path: Path,
) -> None:
    plan = build_plan(
        ["custom_components/voip_stack/endpoint_termination.py"],
        base="base",
        head="head",
        full=False,
        event="manual",
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
                            {
                                "scenario": "registered-sip-to-esp-bidirectional-hangup",
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

    # Scenario names and quiescence alone never attest measured media.
    with pytest.raises(EvidenceError, match="did not prove planned scenario"):
        derive_scenario_evidence("hil-s3", plan, [artifact])

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
        event="manual",
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
        event="manual",
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
                        "route_default",
                        "registered_sip_peer_auto_answer_on_caller_bye",
                        "registered_sip_peer_auto_answer_off_callee_bye",
                        "initial_delayed_offer_caller_bye",
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
                        "name": "dtmf_primary_extension_bypasses_automation",
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
    wildix = tmp_path / "wildix-dtmf.json"
    wildix.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "name": "wildix_trunk_dtmf_route_to_esp",
                        "status": "pass",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    claims = derive_scenario_evidence("peer-live", plan, [matrix, dtmf, wildix])

    assert {
        claim["scenario_id"] for claim in claims
    }.issuperset(
        {
            "ha-phone-policy-and-dnd-routing",
            "inbound-route-decision-guards",
            "esp-to-ha-answer-hangup",
            "registered-sip-to-esp-bidirectional-hangup",
            "trunk-dtmf-routing-and-established-dtmf",
            "p4-audio-to-bidirectional-video-reinvite",
        }
    )

    dtmf.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "name": "dtmf_primary_extension_bypasses_automation",
                        "status": "pass",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    try:
        derive_scenario_evidence("peer-live", plan, [matrix, dtmf, wildix])
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
    assert {"peer-live", "browser-real", "hil-s3", "hil-p4"}.issubset(
        plan["required_jobs"]
    )


def test_manual_full_plan_keeps_trusted_runner_jobs() -> None:
    plan = build_plan(
        ["README.md"],
        base="base",
        head="head",
        full=True,
        event="manual",
    )

    assert {"peer-live", "browser-real", "hil-s3", "hil-p4"}.issubset(
        plan["required_jobs"]
    )


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
            "trigger": "voip_stack.route_requested",
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
            "trigger": "voip_stack.call_unanswered",
            "options": {"for": {"seconds": 1}},
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
