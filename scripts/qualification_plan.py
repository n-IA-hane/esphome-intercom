#!/usr/bin/env python3
"""Build a deterministic qualification plan from a repository diff."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import fnmatch
import hashlib
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qualification.registry import (  # noqa: E402
    ALL_JOBS,
    AREAS,
    EXECUTOR_JOBS,
    FIRMWARE_PROFILES,
    HIL_FIRMWARE_PROFILES,
    Risk,
    SCENARIOS,
    regression_ledger,
)


RISK_ORDER = {risk: index for index, risk in enumerate(Risk)}


def plan_id(payload: dict[str, object]) -> str:
    """Return the content identity without trusting a stored digest."""

    canonical_payload = {
        key: value for key, value in payload.items() if key != "plan_id"
    }
    canonical = json.dumps(canonical_payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _json_record(value: object) -> dict[str, object]:
    record = asdict(value)
    return {
        key: sorted(item) if isinstance(item, frozenset) else item
        for key, item in record.items()
    }


def _git_diff(base: str, head: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...{head}"],
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return sorted(filter(None, result.stdout.splitlines()))


def _matches(path: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(path, pattern) or Path(path).match(pattern)


def build_plan(
    changed_files: list[str],
    *,
    base: str,
    head: str,
    full: bool,
    event: str,
) -> dict[str, object]:
    matched = {
        area.id: area
        for area in AREAS
        if any(
            _matches(path, pattern) for path in changed_files for pattern in area.paths
        )
    }
    unknown = [
        path
        for path in changed_files
        if not any(_matches(path, pattern) for area in AREAS for pattern in area.paths)
    ]

    run_all = full or event in {"push-dev", "schedule"} or bool(unknown)
    if run_all:
        matched = {area.id: area for area in AREAS}

    jobs = {"static"}
    for area in matched.values():
        jobs.update(area.jobs)
    if run_all:
        jobs.update(ALL_JOBS)

    area_ids = frozenset(matched)
    scenarios = [
        scenario
        for scenario in SCENARIOS
        if run_all or scenario.areas.intersection(area_ids)
    ]
    for scenario in scenarios:
        jobs.update(EXECUTOR_JOBS[executor] for executor in scenario.executors)
    hil_profiles = {
        profile_id for job, profile_id in HIL_FIRMWARE_PROFILES.items() if job in jobs
    }
    if hil_profiles:
        jobs.add("firmware")
    profiles = [
        profile
        for profile in FIRMWARE_PROFILES
        if "firmware" in jobs
        and (
            run_all
            or profile.id in hil_profiles
            or profile.areas.intersection(area_ids)
        )
    ]
    selected_scenario_ids = {scenario.id for scenario in scenarios}
    regressions = [
        record
        for record in regression_ledger()
        if selected_scenario_ids.intersection(map(str, record["scenarios"]))
    ]
    risk = max(
        (area.risk for area in matched.values()), key=RISK_ORDER.get, default=Risk.LOW
    )
    skipped = {
        job: "not required by changed areas"
        for job in sorted(ALL_JOBS.difference(jobs))
    }
    payload: dict[str, object] = {
        "schema_version": 1,
        "base": base,
        "head": head,
        "event": event,
        "full": run_all,
        "changed_files": changed_files,
        "unknown_files": unknown,
        "areas": sorted(matched),
        "risk": risk.value,
        "required_jobs": sorted(jobs),
        "skipped_jobs": skipped,
        "firmware_profiles": [_json_record(profile) for profile in profiles],
        "scenarios": [_json_record(scenario) for scenario in scenarios],
        "regressions": regressions,
    }
    payload["plan_id"] = plan_id(payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument(
        "--event",
        choices=("pull-request", "push-dev", "schedule", "manual"),
        default="manual",
    )
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--changed-file", action="append", default=[])
    parser.add_argument("--output", type=Path, default=Path("qualification-plan.json"))
    args = parser.parse_args()

    changed_files = sorted(set(args.changed_file or _git_diff(args.base, args.head)))
    plan = build_plan(
        changed_files, base=args.base, head=args.head, full=args.full, event=args.event
    )
    args.output.write_text(
        json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
