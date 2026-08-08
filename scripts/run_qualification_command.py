#!/usr/bin/env python3
"""Run one qualification command and bind its log to candidate evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.record_qualification_result import build_result  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, action="append", default=[])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a qualification command is required")

    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
    evidence_root = args.evidence_root.resolve()
    evidence_root.mkdir(parents=True, exist_ok=True)
    log = evidence_root / f"{args.job}.log"
    with log.open("wb") as output:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            stdout=output,
            stderr=subprocess.STDOUT,
            check=False,
        )
    result = build_result(
        args.job,
        "success" if completed.returncode == 0 else "failure",
        [log, *args.artifact],
        evidence_root,
        plan_id=str(plan["plan_id"]),
        candidate_id=str(candidate["candidate_id"]),
        head=str(plan["head"]),
    )
    result_path = evidence_root / f"result-{args.job}.json"
    result_path.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(log.read_text(encoding="utf-8", errors="replace"), end="")
    print(result_path)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
