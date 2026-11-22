"""Actual Git source and package identity, including adverse audit inputs."""

from __future__ import annotations

import subprocess
import zipfile

import pytest

from scripts.candidate_lock import _repository, candidate_id
from scripts.package_release_candidate import package_candidate, sha256
from scripts.verify_release_candidate import verify_release


def _git(repository, *args):
    return subprocess.check_output(["git", "-C", str(repository), *args], text=True).strip()


@pytest.fixture
def release_source(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    source = repository / "custom_components/voip_stack"
    source.mkdir(parents=True)
    (source / "manifest.json").write_text('{"domain":"voip_stack","version":"1.0.0"}\n')
    (source / "__init__.py").write_text('VALUE = "qualified source"\n')
    (repository / "LICENSE").write_text("Test source license\n")
    _git(repository, "add", ".")
    _git(repository, "-c", "user.name=Qualification fixture", "-c", "user.email=fixture@example.invalid",
         "commit", "-qm", "Frozen source")
    head = _git(repository, "rev-parse", "HEAD")
    candidate = {"repositories": {"esphome-intercom": _repository(repository)}}
    candidate["candidate_id"] = candidate_id(candidate)
    archive = tmp_path / "voip_stack.zip"
    return repository, source, head, candidate, archive


def _qualification(package):
    return {"schema_version": 1, "qualified": True, "head": package["head"],
            "candidate_id": package["candidate_id"], "jobs": {"release-package": {"status": "success"}}}


def test_exact_git_source_is_reproducible_and_independently_verified(release_source):
    repository, source, head, candidate, archive = release_source
    package = package_candidate(source, archive, candidate, {"head": head})
    first = archive.read_bytes()
    assert verify_release(head, package, _qualification(package), archive, source_repository=repository) == []
    package_candidate(source, archive, candidate, {"head": head})
    assert archive.read_bytes() == first
    with zipfile.ZipFile(archive) as bundle:
        assert set(bundle.namelist()) == {"manifest.json", "__init__.py", "LICENSE"}
        assert bundle.read("LICENSE") == (repository / "LICENSE").read_bytes()


@pytest.mark.parametrize("mutation", ["source", "license", "extra", "deleted", "symlink"])
def test_honest_metadata_cannot_attest_modified_source(release_source, mutation):
    repository, source, head, candidate, archive = release_source
    if mutation == "source":
        (source / "__init__.py").write_text('VALUE = "unqualified source"\n')
    elif mutation == "license":
        (repository / "LICENSE").write_text("Other license\n")
    elif mutation == "extra":
        (source / "extra.py").write_text("VALUE = 2\n")
    elif mutation == "deleted":
        (source / "__init__.py").unlink()
    else:
        (source / "__init__.py").unlink()
        (source / "__init__.py").symlink_to(repository / "LICENSE")
    with pytest.raises(ValueError):
        package_candidate(source, archive, candidate, {"head": head})
    assert not archive.exists()


def test_foreign_directory_cannot_borrow_candidate_identity(release_source, tmp_path):
    _, _, head, candidate, archive = release_source
    source = tmp_path / "foreign"
    source.mkdir()
    (source / "manifest.json").write_text('{"version":"0.0.0"}')
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        package_candidate(source, archive, candidate, {"head": head})
    assert not archive.exists()


def test_zip_cannot_be_replaced_even_with_recomputed_archive_hash(release_source):
    repository, source, head, candidate, archive = release_source
    package = package_candidate(source, archive, candidate, {"head": head})
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("manifest.json", '{"version":"0.0.0"}')
        bundle.writestr("__init__.py", "UNQUALIFIED = True")
        bundle.writestr("LICENSE", "Test source license\n")
    package["archive"].update(bytes=archive.stat().st_size, sha256=sha256(archive))
    errors = verify_release(head, package, _qualification(package), archive, source_repository=repository)
    assert "release archive bytes differ from the tag tree" in errors


def test_dirty_candidate_cannot_be_packaged(release_source):
    _, source, head, candidate, archive = release_source
    candidate["repositories"]["esphome-intercom"]["dirty"] = True
    candidate["candidate_id"] = candidate_id(candidate)
    with pytest.raises(RuntimeError, match="clean coordinated sources"):
        package_candidate(source, archive, candidate, {"head": head})


def test_dirty_source_identity_tracks_bytes_modes_and_untracked_inputs(release_source):
    repository, source, _, _, _ = release_source
    target = source / "__init__.py"
    target.write_text("VALUE = 1\n")
    first = _repository(repository)
    target.write_text("VALUE = 999\n")
    second = _repository(repository)
    assert first["commit"] == second["commit"]
    assert first["dirty"] is second["dirty"] is True
    assert candidate_id(first) != candidate_id(second)
    _git(repository, "add", ".")
    assert _repository(repository)["worktree_sha256"] == second["worktree_sha256"]
    target.chmod(0o755)
    executable = _repository(repository)
    assert executable["worktree_sha256"] != second["worktree_sha256"]
    target.chmod(0o644)
    untracked = source / "untracked.py"
    untracked.write_text("VALUE = 2\n")
    extra = _repository(repository)
    assert extra["worktree_sha256"] != second["worktree_sha256"]
    untracked.unlink()
    assert _repository(repository)["worktree_sha256"] == second["worktree_sha256"]
    target.unlink()
    assert _repository(repository)["worktree_sha256"] != second["worktree_sha256"]
