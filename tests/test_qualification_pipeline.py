"""Behavioral tests for fail-closed candidate qualification."""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plan_module = _load("qualification_plan", "scripts/qualification_plan.py")
verify_module = _load("verify_qualification", "scripts/verify_qualification.py")


def test_documentation_only_plan_has_explicit_skips() -> None:
    plan = plan_module.make_plan(("docs/TESTING_AND_DEBUG.md",))

    assert plan["risk"] == "low"
    assert plan["required_jobs"] == ("quick",)
    assert plan["skipped_jobs"]["firmware"] == "documentation-only diff"


def test_product_plan_requires_all_maintained_firmware_profiles() -> None:
    plan = plan_module.make_plan(("custom_components/voip_stack/sip_client.py",))

    assert plan["risk"] == "critical"
    assert "software-full" in plan["required_jobs"]
    assert "firmware" in plan["required_jobs"]
    assert len(plan["required_firmware_profiles"]) == 6


def test_summary_rejects_missing_required_job() -> None:
    plan = {"required_jobs": ("quick", "firmware")}
    candidate = {
        "repositories": {"esphome-intercom": {"commit": "candidate-sha"}}
    }
    results = {
        "quick": {
            "status": "passed",
            "candidate_commit": "candidate-sha",
            "artifacts": ("quick.log",),
        }
    }

    assert verify_module.verify(plan, candidate, results) == [
        "missing required job result: firmware"
    ]


def test_summary_rejects_skipped_job_and_foreign_artifact() -> None:
    plan = {"required_jobs": ("peer",)}
    candidate = {
        "repositories": {"esphome-intercom": {"commit": "candidate-sha"}}
    }
    results = {
        "peer": {
            "status": "skipped",
            "candidate_commit": "other-sha",
            "artifacts": (),
        }
    }

    assert verify_module.verify(plan, candidate, results) == [
        "required job did not pass: peer",
        "candidate mismatch for job: peer",
        "missing artifacts for job: peer",
    ]
