#!/usr/bin/env python3
"""Merge disjoint qualification job results without hiding duplicates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def merge_results(paths: list[Path]) -> dict[str, object]:
    jobs: dict[str, object] = {}
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        source_jobs = payload.get("jobs")
        if not isinstance(source_jobs, dict):
            raise RuntimeError(f"qualification result has no jobs: {path}")
        duplicates = jobs.keys() & source_jobs.keys()
        if duplicates:
            raise RuntimeError(f"duplicate qualification jobs: {', '.join(sorted(duplicates))}")
        jobs.update(source_jobs)
    return {"schema_version": 1, "jobs": jobs}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = merge_results(args.inputs)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

