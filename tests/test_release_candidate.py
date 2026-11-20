"""Contracts for promoting only the archive qualified for the tag commit."""

from __future__ import annotations

import hashlib
from pathlib import Path

from scripts.verify_release_candidate import REQUIRED_CI_JOBS, verify_ci_run, verify_ci_sources, verify_release


def _records(archive: Path) -> tuple[dict[str, object], dict[str, object]]:
    package = {
        "schema_version": 1,
        "head": "head",
        "candidate_id": "candidate",
        "archive": {
            "name": archive.name,
            "bytes": archive.stat().st_size,
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        },
    }
    qualification = {
        "schema_version": 1,
        "qualified": True,
        "head": "head",
        "candidate_id": "candidate",
        "jobs": {"release-package": {"status": "success"}},
    }
    return package, qualification


def test_exact_qualified_release_archive_is_accepted(tmp_path: Path) -> None:
    archive = tmp_path / "voip_stack.zip"
    archive.write_bytes(b"qualified archive")
    package, qualification = _records(archive)

    assert verify_release("head", package, qualification, archive) == []


def test_wrong_tag_or_modified_archive_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "voip_stack.zip"
    archive.write_bytes(b"qualified archive")
    package, qualification = _records(archive)
    archive.write_bytes(b"modified archive")

    errors = verify_release("other", package, qualification, archive)

    assert "release evidence does not match tag commit" in errors
    assert "release archive hash differs from package manifest" in errors


def _successful_ci():
    run = {"head_sha": "qualified", "head_branch": "dev", "status": "completed",
           "conclusion": "success", "event": "push", "path": ".github/workflows/ci.yml"}
    jobs = [{"name": name, "head_sha": "qualified", "status": "completed", "conclusion": "success"}
            for name in REQUIRED_CI_JOBS]
    return run, jobs


def test_promotion_requires_success_on_its_exact_commit_and_ref():
    run, jobs = _successful_ci()
    assert verify_ci_run("qualified", "dev", run, jobs) == []
    assert verify_ci_run("new-candidate", "dev", run, jobs)
    assert verify_ci_run("qualified", "main", run, jobs)


def test_successful_summary_cannot_hide_missing_skipped_or_stale_jobs():
    run, jobs = _successful_ci()
    assert verify_ci_run("qualified", "dev", run, jobs[1:])
    jobs[0] = {**jobs[0], "conclusion": "skipped"}
    assert verify_ci_run("qualified", "dev", run, jobs)
    jobs[0] = {**jobs[0], "conclusion": "success", "head_sha": "old-candidate"}
    assert verify_ci_run("qualified", "dev", run, jobs)


def test_cancelled_or_pull_request_run_cannot_qualify_a_release():
    run, jobs = _successful_ci()
    assert verify_ci_run("qualified", "dev", {**run, "conclusion": "cancelled"}, jobs)
    assert verify_ci_run("qualified", "dev", {**run, "event": "pull_request"}, jobs)


def test_same_backend_commit_cannot_hide_different_firmware_components():
    candidate = {"repositories": {"esphome-intercom": {"commit": "head"},
                                  "esphome-voip-stack": {"commit": "voip"},
                                  "voip-stack-phone-profiles": {"commit": "phones"}}}
    sources = {"version": 1, "backend_sha": "head", "components": {
        "esphome-voip-stack": {"sha": "voip"}},
        "phone_profiles": {"repository": "owner/voip-stack-phone-profiles", "sha": "phones"}}
    assert verify_ci_sources(candidate, sources) == []
    changed = {**sources, "components": {"esphome-voip-stack": {"sha": "new-dev"}}}
    assert verify_ci_sources(candidate, changed)
    assert verify_ci_sources(candidate, {**sources, "phone_profiles": {}})
    assert verify_ci_sources({}, sources)
