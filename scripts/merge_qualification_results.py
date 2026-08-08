#!/usr/bin/env python3
"""Merge disjoint qualification job results without hiding duplicates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def merge_results(paths: list[Path]) -> dict[str, object]:
    jobs: dict[str, object] = {}
    identity: tuple[str, str, str] | None = None
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != 1:
            raise RuntimeError(f"unsupported qualification result schema: {path}")
        source_identity = (
            str(payload.get("plan_id") or ""),
            str(payload.get("candidate_id") or ""),
            str(payload.get("head") or ""),
        )
        if not all(source_identity):
            raise RuntimeError(f"qualification result has no identity: {path}")
        if identity is None:
            identity = source_identity
        elif source_identity != identity:
            raise RuntimeError(f"qualification result identity mismatch: {path}")
        source_jobs = payload.get("jobs")
        if not isinstance(source_jobs, dict):
            raise RuntimeError(f"qualification result has no jobs: {path}")
        duplicates = jobs.keys() & source_jobs.keys()
        if duplicates:
            raise RuntimeError(
                f"duplicate qualification jobs: {', '.join(sorted(duplicates))}"
            )
        jobs.update(source_jobs)
    assert identity is not None
    return {
        "schema_version": 1,
        "plan_id": identity[0],
        "candidate_id": identity[1],
        "head": identity[2],
        "jobs": jobs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = merge_results(args.inputs)
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
