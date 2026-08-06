#!/usr/bin/env python3
"""Verify that every required qualification result belongs to one candidate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def verify(
    plan: dict[str, object],
    candidate: dict[str, object],
    results: dict[str, dict[str, object]],
) -> list[str]:
    errors: list[str] = []
    expected_commit = candidate["repositories"]["esphome-intercom"]["commit"]
    for job in plan["required_jobs"]:
        result = results.get(job)
        if result is None:
            errors.append(f"missing required job result: {job}")
            continue
        if result.get("status") != "passed":
            errors.append(f"required job did not pass: {job}")
        if result.get("candidate_commit") != expected_commit:
            errors.append(f"candidate mismatch for job: {job}")
        if not result.get("artifacts"):
            errors.append(f"missing artifacts for job: {job}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("qualification-manifest.json"))
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    candidate = json.loads(args.candidate.read_text())
    results = json.loads(args.results.read_text())
    errors = verify(plan, candidate, results)
    manifest = {
        "schema_version": 1,
        "candidate": candidate,
        "plan": plan,
        "results": results,
        "status": "failed" if errors else "passed",
        "errors": errors,
    }
    args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    if errors:
        for error in errors:
            print(error)
        return 1
    print("qualification=passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
