#!/usr/bin/env python3
"""Verify that qualification results prove the exact planned candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from scripts.candidate_lock import candidate_id as compute_candidate_id
from scripts.qualification_plan import plan_id as compute_plan_id
from qualification.evidence import CLAIMS
from qualification.registry import EXECUTOR_JOBS, regression_ledger


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_REPOSITORIES = frozenset(
    json.loads((ROOT / "qualification/sources.json").read_text(encoding="utf-8"))[
        "repositories"
    ]
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(
    plan: dict[str, object],
    candidate: dict[str, object],
    results: dict[str, object],
    *,
    artifact_root: Path,
) -> tuple[list[str], dict[str, object]]:
    errors: list[str] = []
    if plan.get("schema_version") != 1:
        errors.append("qualification plan schema is unsupported")
    if candidate.get("schema_version") != 1:
        errors.append("candidate schema is unsupported")
    if results.get("schema_version") != 1:
        errors.append("qualification results schema is unsupported")
    head = str(plan.get("head") or "")
    plan_id = str(plan.get("plan_id") or "")
    candidate_id = str(candidate.get("candidate_id") or "")
    if plan_id != compute_plan_id(plan):
        errors.append("qualification plan identity is invalid")
    if candidate_id != compute_candidate_id(candidate):
        errors.append("candidate identity is invalid")
    if results.get("plan_id") != plan_id:
        errors.append("qualification results do not match plan")
    if results.get("candidate_id") != candidate_id:
        errors.append("qualification results do not match candidate")
    if results.get("head") != head:
        errors.append("qualification results do not match head")
    repositories = candidate.get("repositories")
    if (
        not isinstance(repositories, dict)
        or frozenset(repositories) != EXPECTED_REPOSITORIES
    ):
        errors.append("candidate repository set is invalid")
    intercom = (
        repositories.get("esphome-intercom", {})
        if isinstance(repositories, dict)
        else {}
    )
    if intercom.get("commit") != head:
        errors.append("candidate intercom commit does not match plan head")
    if (
        any(
            isinstance(value, dict) and value.get("dirty")
            for value in repositories.values()
        )
        if isinstance(repositories, dict)
        else True
    ):
        errors.append("candidate contains a dirty or invalid repository")

    job_results = results.get("jobs")
    if not isinstance(job_results, dict):
        errors.append("qualification results do not contain jobs")
        job_results = {}
    required_jobs = [str(job) for job in plan.get("required_jobs", [])]
    for extra_job in sorted(set(job_results).difference(required_jobs)):
        errors.append(f"qualification result was not required: {extra_job}")
    verified_jobs: dict[str, object] = {}
    for job in required_jobs:
        result = job_results.get(job)
        if not isinstance(result, dict):
            errors.append(f"required job is missing: {job}")
            continue
        status = result.get("status")
        if status != "success":
            errors.append(f"required job did not succeed: {job} ({status})")
        artifacts = result.get("artifacts", [])
        checked_artifacts: list[dict[str, str]] = []
        if not isinstance(artifacts, list):
            errors.append(f"job artifacts are invalid: {job}")
            artifacts = []
        if not artifacts:
            errors.append(f"required job has no evidence: {job}")
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                errors.append(f"job artifact entry is invalid: {job}")
                continue
            relative = Path(str(artifact.get("path") or ""))
            if relative.is_absolute() or ".." in relative.parts:
                errors.append(f"job artifact path escapes evidence root: {job}")
                continue
            root = artifact_root.resolve()
            path = (root / relative).resolve()
            if path != root and root not in path.parents:
                errors.append(f"job artifact path escapes evidence root: {job}")
                continue
            if not path.is_file():
                errors.append(f"job artifact is missing: {job}/{relative}")
                continue
            actual = _sha256(path)
            expected = str(artifact.get("sha256") or "")
            if actual != expected:
                errors.append(f"job artifact hash mismatch: {job}/{relative}")
            if artifact.get("bytes") != path.stat().st_size:
                errors.append(f"job artifact size mismatch: {job}/{relative}")
            checked_artifacts.append({"path": str(relative), "sha256": actual})
        verified_jobs[job] = {"status": status, "artifacts": checked_artifacts}

    planned_scenarios = {
        str(scenario.get("id") or ""): scenario
        for scenario in plan.get("scenarios", [])
        if isinstance(scenario, dict)
    }
    selected_scenario_ids = set(planned_scenarios)
    expected_regressions = [
        record
        for record in regression_ledger()
        if selected_scenario_ids.intersection(map(str, record["scenarios"]))
    ]
    if plan.get("regressions") != expected_regressions:
        errors.append("qualification regression ledger selection is invalid")
    evidence = results.get("scenario_evidence", [])
    if not isinstance(evidence, list):
        errors.append("qualification scenario evidence is invalid")
        evidence = []
    observed = {
        scenario_id: {"executors": set(), "oracles": set(), "postconditions": set()}
        for scenario_id in planned_scenarios
    }
    verified_evidence: list[dict[str, object]] = []
    for claim in evidence:
        if not isinstance(claim, dict):
            errors.append("qualification scenario evidence entry is invalid")
            continue
        scenario_id = str(claim.get("scenario_id") or "")
        job = str(claim.get("job") or "")
        if scenario_id not in planned_scenarios:
            errors.append(f"scenario evidence was not planned: {scenario_id}")
            continue
        if job not in required_jobs or job_results.get(job, {}).get("status") != "success":
            errors.append(f"scenario evidence job did not succeed: {scenario_id}/{job}")
            continue
        if claim.get("status") != "passed":
            errors.append(f"planned scenario did not pass: {scenario_id}/{job}")
            continue
        contract = planned_scenarios[scenario_id]
        supported = CLAIMS.get((scenario_id, job))
        if supported is None:
            errors.append(
                f"scenario evidence is claimed by an unrelated job: {scenario_id}/{job}"
            )
            continue
        normalized: dict[str, list[str]] = {}
        invalid = False
        for field in ("executors", "oracles", "postconditions"):
            values = claim.get(field, [])
            if not isinstance(values, list) or not all(
                isinstance(value, str) and value for value in values
            ):
                errors.append(f"scenario {field} evidence is invalid: {scenario_id}/{job}")
                invalid = True
                break
            values = sorted(set(values))
            unsupported = set(values).difference(map(str, contract.get(field, [])))
            unsupported.update(set(values).difference(supported[field]))
            if unsupported:
                errors.append(
                    f"scenario evidence exceeds contract: {scenario_id}/{field}"
                )
                invalid = True
            if field == "executors" and any(
                EXECUTOR_JOBS.get(value) != job for value in values
            ):
                errors.append(
                    f"scenario executor is claimed by the wrong job: {scenario_id}/{job}"
                )
                invalid = True
            normalized[field] = values
        if invalid:
            continue
        for field, values in normalized.items():
            observed[scenario_id][field].update(values)
        verified_evidence.append(
            {"scenario_id": scenario_id, "job": job, "status": "passed", **normalized}
        )

    scenario_manifest: dict[str, object] = {}
    for scenario_id, contract in planned_scenarios.items():
        missing: dict[str, list[str]] = {}
        for field in ("executors", "oracles", "postconditions"):
            absent = sorted(set(map(str, contract.get(field, []))) - observed[scenario_id][field])
            if absent:
                missing[field] = absent
                errors.append(
                    f"planned scenario lacks {field}: {scenario_id} ({', '.join(absent)})"
                )
        scenario_manifest[scenario_id] = {
            field: sorted(values) for field, values in observed[scenario_id].items()
        } | {"complete": not missing, "missing": missing}

    manifest = {
        "schema_version": 1,
        "candidate": candidate,
        "plan_id": plan_id,
        "candidate_id": candidate_id,
        "head": head,
        "jobs": verified_jobs,
        "scenario_evidence": verified_evidence,
        "scenarios": scenario_manifest,
        "qualified": not errors,
        "errors": errors,
    }
    return errors, manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, default=Path("."))
    parser.add_argument(
        "--output", type=Path, default=Path("qualification-manifest.json")
    )
    args = parser.parse_args()

    errors, manifest = verify(
        json.loads(args.plan.read_text(encoding="utf-8")),
        json.loads(args.candidate.read_text(encoding="utf-8")),
        json.loads(args.results.read_text(encoding="utf-8")),
        artifact_root=args.artifact_root,
    )
    args.output.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    for error in errors:
        print(f"qualification_error={error}")
    print(f"qualification_manifest={args.output}")
    return 2 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
