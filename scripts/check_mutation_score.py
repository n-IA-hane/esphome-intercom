#!/usr/bin/env python3
"""Fail when a mutmut result falls below the accepted effectiveness floor."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("--minimum", type=float, default=60.0)
    args = parser.parse_args()

    report = json.loads(args.report.read_text())
    killed = int(report.get("killed", 0))
    survived = int(report.get("survived", 0))
    timeout = int(report.get("timeout", 0))
    suspicious = int(report.get("suspicious", 0))
    interrupted = int(report.get("check_was_interrupted_by_user", 0))
    assessed = killed + survived + timeout + suspicious
    score = 100.0 * killed / assessed if assessed else 0.0
    print(
        f"mutation_score={score:.2f}% killed={killed} survived={survived} "
        f"timeout={timeout} no_tests={int(report.get('no_tests', 0))}"
    )
    if interrupted or score < args.minimum:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
