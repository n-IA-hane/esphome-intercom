#!/usr/bin/env python3
"""Require live call evidence produced from the exact candidate commit."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def _candidate_commit() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()


def validate_artifact(
    artifact: dict[str, object],
    *,
    commit: str,
    required: set[str],
    now: datetime,
    max_age: timedelta,
) -> list[str]:
    errors: list[str] = []
    candidate = artifact.get("candidate")
    if not isinstance(candidate, dict):
        return ["artifact has no candidate metadata"]
    if candidate.get("commit") != commit:
        errors.append("artifact commit does not match the candidate")
    if candidate.get("dirty") is not False:
        errors.append("live qualification was not run from a clean worktree")

    try:
        created = datetime.fromisoformat(str(artifact["created_at"]))
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if now - created.astimezone(UTC) > max_age:
            errors.append("live qualification artifact is stale")
        if created > now + timedelta(minutes=5):
            errors.append("live qualification timestamp is in the future")
    except (KeyError, TypeError, ValueError):
        errors.append("artifact has an invalid created_at timestamp")

    results = artifact.get("results")
    if not isinstance(results, list):
        errors.append("artifact has no executable scenario results")
        return errors
    statuses = {
        str(item.get("scenario")): str(item.get("status"))
        for item in results
        if isinstance(item, dict)
    }
    missing = sorted(required - statuses.keys())
    failed = sorted(name for name in required if statuses.get(name) != "passed")
    if missing:
        errors.append(f"required scenarios missing: {', '.join(missing)}")
    if failed:
        errors.append(f"required scenarios not passed: {', '.join(failed)}")
    return errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--commit", default="")
    parser.add_argument("--max-age-hours", type=float, default=24.0)
    parser.add_argument("--require", action="append", default=[])
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    artifact = json.loads(args.artifact.read_text(encoding="utf-8"))
    errors = validate_artifact(
        artifact,
        commit=args.commit or _candidate_commit(),
        required=set(args.require),
        now=datetime.now(UTC),
        max_age=timedelta(hours=args.max_age_hours),
    )
    if errors:
        for error in errors:
            print(f"qualification_error={error}")
        return 1
    print("live_qualification=passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
