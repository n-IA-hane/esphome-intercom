#!/usr/bin/env python3
"""Verify that qualification results prove the exact planned candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from scripts.candidate_lock import candidate_id as compute_candidate_id
from scripts.qualification_plan import plan_id as compute_plan_id


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

    manifest = {
        "schema_version": 1,
        "candidate": candidate,
        "plan_id": plan_id,
        "candidate_id": candidate_id,
        "head": head,
        "jobs": verified_jobs,
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
