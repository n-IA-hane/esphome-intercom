#!/usr/bin/env python3
"""Inspect or validate the Home Assistant dial-plan use-case matrix."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qualification.dialplan_matrix import USE_CASES, validate_use_cases  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--qualification", help="show one qualification class")
    args = parser.parse_args()

    cases = [
        case
        for case in USE_CASES
        if not args.qualification or case.qualification == args.qualification
    ]
    errors = validate_use_cases()
    if args.json:
        print(json.dumps([asdict(case) for case in cases], indent=2))
    else:
        for case in cases:
            print(
                f"{case.id}: {case.hook} -> {case.operation} "
                f"[{case.qualification}]"
            )
    if args.validate and errors:
        for error in errors:
            print(f"matrix error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
