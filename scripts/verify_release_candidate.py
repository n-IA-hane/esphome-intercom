#!/usr/bin/env python3
"""Verify that a release uses the archive qualified for its exact tag commit."""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import subprocess
import zipfile

from scripts.package_release_candidate import sha256


REQUIRED_CI_JOBS = frozenset({
    "source-channel", "coordinated-sources", "software", "home-assistant",
    "browser-presentation", "assist-media-host", "documentation", "editorial",
    "mipi-driver-host (5)", "mipi-driver-host (6)", "box-v2-firmware",
    "box-v2-voip-only-firmware", "ws3-public-firmware",
})


def verify_ci_run(
    head: str,
    ref: str,
    run: dict[str, object],
    jobs: list[dict[str, object]],
) -> list[str]:
    """Require the completed CI run for the exact publication source."""
    errors: list[str] = []
    if run.get("head_sha") != head or run.get("head_branch") != ref:
        errors.append("CI does not match the candidate commit and source ref")
    if run.get("path") != ".github/workflows/ci.yml" or run.get("event") not in {
        "push", "workflow_dispatch",
    }:
        errors.append("candidate CI must use the repository publication workflow")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        errors.append("candidate CI has not completed successfully")
    names = {str(job.get("name")) for job in jobs}
    missing = REQUIRED_CI_JOBS - names
    if missing:
        errors.append("candidate CI is missing required jobs: " + ", ".join(sorted(missing)))
    for job in jobs:
        if job.get("head_sha") != head:
            errors.append(f"CI job {job.get('name')} does not match the candidate commit")
        if job.get("status") != "completed" or job.get("conclusion") != "success":
            errors.append(f"CI job {job.get('name')} did not succeed")
    return errors


def load_ci_run(repository: str, run_id: int) -> tuple[dict, list[dict]]:
    """Read authenticated GitHub results, including every page of jobs."""
    endpoint = f"repos/{repository}/actions/runs/{run_id}"
    run = json.loads(subprocess.check_output(["gh", "api", endpoint], text=True))
    pages = json.loads(subprocess.check_output([
        "gh", "api", "--paginate", "--slurp", endpoint + "/jobs?filter=latest&per_page=100",
    ], text=True))
    jobs = [job for page in pages for job in page["jobs"]]
    if not pages or len(jobs) != pages[0]["total_count"]:
        raise ValueError("GitHub did not return the complete CI job list")
    return run, jobs


def load_ci_sources(repository: str, run_id: int) -> dict:
    """Read the immutable component manifest uploaded by this CI run."""
    endpoint = f"repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100"
    pages = json.loads(subprocess.check_output([
        "gh", "api", "--paginate", "--slurp", endpoint,
    ], text=True))
    artifacts = [artifact for page in pages for artifact in page["artifacts"]
                 if artifact["name"] == "resolved-sources" and not artifact["expired"]]
    if len(artifacts) != 1:
        raise ValueError("candidate CI has no unique resolved-sources artifact")
    archive = subprocess.check_output([
        "gh", "api", f"repos/{repository}/actions/artifacts/{artifacts[0]['id']}/zip",
    ])
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        return json.loads(bundle.read("coordinated-sources.json"))


def verify_ci_sources(candidate: dict, sources: dict) -> list[str]:
    """Reject CI that tested a different graph despite sharing the backend SHA."""
    repositories = candidate.get("repositories", {})
    if not repositories or sources.get("version") != 1:
        return ["candidate CI source manifest is invalid"]
    actual = {name: item.get("sha") for name, item in sources.get("components", {}).items()}
    head = sources.get("backend_sha")
    if "esphome-intercom" in actual and actual["esphome-intercom"] != head:
        return ["candidate CI backend identities disagree"]
    actual["esphome-intercom"] = head
    catalog = sources.get("phone_profiles", {})
    if catalog.get("repository"):
        actual[catalog["repository"].rsplit("/", 1)[-1]] = catalog.get("sha")
    expected = {name: item.get("commit") for name, item in repositories.items()}
    return [] if actual == expected else ["candidate CI tested a different coordinated source graph"]


def verify_release(
    tag_sha: str,
    package: dict[str, object],
    qualification: dict[str, object],
    archive: Path,
) -> list[str]:
    errors: list[str] = []
    archive_record = package.get("archive")
    if package.get("schema_version") != 1:
        errors.append("release package schema is unsupported")
    if qualification.get("schema_version") != 1:
        errors.append("qualification manifest schema is unsupported")
    if qualification.get("qualified") is not True:
        errors.append("candidate is not qualified")
    if package.get("head") != tag_sha or qualification.get("head") != tag_sha:
        errors.append("release evidence does not match tag commit")
    if package.get("candidate_id") != qualification.get("candidate_id"):
        errors.append("release package and qualification candidate differ")
    package_job = qualification.get("jobs", {}).get("release-package", {})
    if not isinstance(package_job, dict) or package_job.get("status") != "success":
        errors.append("release-package job did not succeed")
    if not isinstance(archive_record, dict) or not archive.is_file():
        errors.append("qualified release archive is missing")
        return errors
    if archive_record.get("name") != archive.name:
        errors.append("release archive name differs from package manifest")
    if archive_record.get("bytes") != archive.stat().st_size:
        errors.append("release archive size differs from package manifest")
    if archive_record.get("sha256") != sha256(archive):
        errors.append("release archive hash differs from package manifest")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag-sha", required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--qualification", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--ci-run-id", type=int, required=True)
    parser.add_argument("--ci-ref", required=True)
    parser.add_argument("--repository", default="n-IA-hane/esphome-intercom")
    args = parser.parse_args()
    qualification = json.loads(args.qualification.read_text(encoding="utf-8"))
    errors = verify_release(
        args.tag_sha,
        json.loads(args.package.read_text(encoding="utf-8")),
        qualification,
        args.archive,
    )
    try:
        run, jobs = load_ci_run(args.repository, args.ci_run_id)
        errors.extend(verify_ci_run(args.tag_sha, args.ci_ref, run, jobs))
        sources = load_ci_sources(args.repository, args.ci_run_id)
        errors.extend(verify_ci_sources(qualification.get("candidate", {}), sources))
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, subprocess.CalledProcessError) as error:
        errors.append(f"candidate CI could not be verified: {error}")
    for error in errors:
        print(f"release_error={error}")
    return 2 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
