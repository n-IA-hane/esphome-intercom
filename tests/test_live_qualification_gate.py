"""Tests for candidate-bound live qualification evidence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_live_qualification.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_live_qualification", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load()


def _artifact(now: datetime) -> dict[str, object]:
    return {
        "created_at": now.isoformat(),
        "candidate": {"commit": "abc123", "dirty": False},
        "results": [
            {"scenario": "both_directions", "status": "passed"},
            {"scenario": "remote_hangup", "status": "passed"},
        ],
    }


def test_accepts_fresh_clean_evidence_for_exact_commit() -> None:
    now = datetime.now(UTC)
    assert gate.validate_artifact(
        _artifact(now),
        commit="abc123",
        required={"both_directions", "remote_hangup"},
        now=now,
        max_age=timedelta(hours=24),
    ) == []


def test_rejects_other_commit_dirty_stale_and_missing_scenario() -> None:
    now = datetime.now(UTC)
    artifact = _artifact(now - timedelta(days=2))
    artifact["candidate"] = {"commit": "old", "dirty": True}

    errors = gate.validate_artifact(
        artifact,
        commit="new",
        required={"both_directions", "dtmf"},
        now=now,
        max_age=timedelta(hours=24),
    )

    assert "artifact commit does not match the candidate" in errors
    assert "live qualification was not run from a clean worktree" in errors
    assert "live qualification artifact is stale" in errors
    assert "required scenarios missing: dtmf" in errors
    assert "required scenarios not passed: dtmf" in errors
