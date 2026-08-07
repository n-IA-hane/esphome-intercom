"""Behavioral contract for the mutation effectiveness gate."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts" / "check_mutation_score.py"


def run_gate(tmp_path: Path, report: dict[str, int], minimum: float) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "mutation.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(GATE), str(path), "--minimum", str(minimum)],
        text=True,
        capture_output=True,
        check=False,
    )


def test_bounded_timeout_counts_as_detected_mutation(tmp_path: Path) -> None:
    result = run_gate(
        tmp_path,
        {"killed": 60, "survived": 35, "timeout": 5},
        65.0,
    )

    assert result.returncode == 0
    assert "mutation_score=65.00% detected=65" in result.stdout


def test_survivor_and_interrupted_run_still_fail(tmp_path: Path) -> None:
    low_score = run_gate(
        tmp_path,
        {"killed": 59, "survived": 40, "timeout": 1},
        65.0,
    )
    interrupted = run_gate(
        tmp_path,
        {
            "killed": 65,
            "survived": 35,
            "timeout": 0,
            "check_was_interrupted_by_user": 1,
        },
        65.0,
    )

    assert low_score.returncode == 1
    assert interrupted.returncode == 1
