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
        "candidate": {
            "candidate_id": "candidate-a",
            "repositories": {"repo": {"commit": "abc123", "dirty": False}},
        },
        "results": [
            {"scenario": "both_directions", "status": "passed"},
            {"scenario": "remote_hangup", "status": "passed"},
        ],
    }


def test_accepts_fresh_clean_evidence_for_exact_candidate() -> None:
    now = datetime.now(UTC)
    assert gate.validate_artifact(
        _artifact(now),
        candidate_id="candidate-a",
        required={"both_directions", "remote_hangup"},
        now=now,
        max_age=timedelta(hours=24),
    ) == []


def test_accepts_current_dev_evidence_without_a_source_lock() -> None:
    now = datetime.now(UTC)
    artifact = _artifact(now)
    artifact["candidate"] = {
        "qualifying": False,
        "commit": "current-dev",
        "dirty": True,
    }

    assert gate.validate_artifact(
        artifact,
        candidate_id=None,
        required={"both_directions", "remote_hangup"},
        now=now,
        max_age=timedelta(hours=24),
    ) == []


def test_rejects_other_candidate_dirty_stale_and_missing_scenario() -> None:
    now = datetime.now(UTC)
    artifact = _artifact(now - timedelta(days=2))
    artifact["candidate"] = {
        "candidate_id": "old",
        "repositories": {"repo": {"commit": "old", "dirty": True}},
    }

    errors = gate.validate_artifact(
        artifact,
        candidate_id="new",
        required={"both_directions", "dtmf"},
        now=now,
        max_age=timedelta(hours=24),
    )

    assert "artifact candidate does not match the source lock" in errors
    assert "live qualification was not run from clean repositories" in errors
    assert "live qualification artifact is stale" in errors
    assert "required scenarios missing: dtmf" in errors
    assert "required scenarios not passed: dtmf" in errors
