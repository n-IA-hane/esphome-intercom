#!/usr/bin/env python3
"""Create one hashed qualification job result for final verification."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_result(
    job: str,
    status: str,
    artifacts: list[Path],
    root: Path,
    *,
    plan_id: str,
    candidate_id: str,
    head: str,
    scenario_evidence: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    if status == "success" and not artifacts:
        raise RuntimeError(f"successful qualification job has no evidence: {job}")
    root = root.resolve()
    records: list[dict[str, object]] = []
    for artifact in artifacts:
        path = artifact.resolve()
        if not path.is_file():
            raise RuntimeError(f"qualification artifact is unavailable: {artifact}")
        try:
            relative = path.relative_to(root)
        except ValueError as error:
            raise RuntimeError(
                f"qualification artifact is outside evidence root: {artifact}"
            ) from error
        records.append(
            {
                "path": str(relative),
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    evidence = []
    for claim in scenario_evidence or []:
        if not isinstance(claim, dict):
            raise RuntimeError("scenario evidence entry is invalid")
        evidence.append({**claim, "job": job})
    return {
        "schema_version": 1,
        "plan_id": plan_id,
        "candidate_id": candidate_id,
        "head": head,
        "jobs": {job: {"status": status, "artifacts": records}},
        "scenario_evidence": evidence,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True)
    parser.add_argument(
        "--status",
        choices=("success", "failure", "cancelled", "skipped"),
        required=True,
    )
    parser.add_argument("--artifact", type=Path, action="append", default=[])
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--plan-id", required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--scenario-evidence", type=Path)
    args = parser.parse_args()
    scenario_evidence: list[dict[str, object]] = []
    if args.scenario_evidence:
        source = json.loads(args.scenario_evidence.read_text(encoding="utf-8"))
        scenario_evidence = source.get("scenarios", []) if isinstance(source, dict) else []
        if not isinstance(scenario_evidence, list):
            raise RuntimeError("scenario evidence does not contain a scenario list")
    payload = build_result(
        args.job,
        args.status,
        args.artifact,
        args.root,
        plan_id=args.plan_id,
        candidate_id=args.candidate_id,
        head=args.head,
        scenario_evidence=scenario_evidence,
    )
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
