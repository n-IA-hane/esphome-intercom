"""Check that the recovery catalogue accounts for the frozen product delta."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import re
import subprocess


PRODUCT_PATHS = {
    "esphome-intercom": ("custom_components/voip_stack/", "esphome/components/", "packages/", "yamls/"),
    "esphome-voip-stack": ("esphome/components/",),
    "esphome-audio-stack": ("esphome/components/",),
    "esphome-runtime-controller": ("esphome/components/",),
    "esphome-screenshot": ("esphome/components/",),
    "esphome-video-renderer": ("esphome/components/",),
    "esphome-esp-video-camera": ("components/",),
}
STATES = {"pending", "in_progress", "passed", "blocked", "excluded"}
# Stable identities of the initial agreed catalogue. New entries may extend it;
# removing an independent obligation must not pass merely because it owns no file.
REQUIRED_FEATURE_IDS = frozenset(f"F{index:03d}" for index in range(1, 90))


def _sha(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def _relative(value: object) -> bool:
    return isinstance(value, str) and bool(value) and not PurePosixPath(value).is_absolute() and ".." not in PurePosixPath(value).parts


def observed_changes(inventory: dict, sources_root: Path) -> set[tuple[str, str]]:
    """Read Git objects only; never check out, download or change a source."""
    result = set()
    for name, source in inventory["sources"].items():
        repo = sources_root / name
        base, head = source["baseline_sha"], source["quarantine_sha"]
        args = ["diff", "--name-only", "--no-renames", "-z", base, head] if base else ["ls-tree", "-r", "--name-only", "-z", head]
        output = subprocess.check_output(["git", "-C", str(repo), *args])
        for raw in output.split(b"\0"):
            path = raw.decode()
            if path.startswith(PRODUCT_PATHS[name]) and not path.endswith((".md", ".rst")):
                result.add((name, path))
    return result


def validate_inventory(inventory: dict, *, changes: set[tuple[str, str]] | None = None) -> list[str]:
    """Reject missing ownership and inconsistent dependencies before recovery."""
    errors = []
    if inventory.get("schema_version") != 1:
        errors.append("unsupported inventory schema")
    sources = inventory.get("sources", {})
    if set(sources) != set(PRODUCT_PATHS):
        errors.append("inventory must retain all seven source repositories")
    for name, source in sources.items():
        if not _sha(source.get("quarantine_sha")) or (source.get("baseline_sha") is not None and not _sha(source["baseline_sha"])):
            errors.append(f"source has no immutable revision: {name}")
    stages = inventory.get("stages", [])
    stage_ids = [stage.get("id") for stage in stages]
    if stage_ids != list(range(16)):
        errors.append("inventory must retain the sixteen ordered stages")
    for stage in stages:
        if stage.get("state") not in STATES or any(type(dep) is not int or dep not in stage_ids or dep >= stage["id"] for dep in stage.get("depends_on", [])):
            errors.append(f"invalid stage dependency or state: {stage.get('id')}")
    features = inventory.get("features", [])
    by_id = {feature.get("id"): feature for feature in features}
    if not features or len(by_id) != len(features):
        errors.append("feature IDs must be nonempty and unique")
    errors.extend(f"required feature is missing: {identity}" for identity in sorted(REQUIRED_FEATURE_IDS - by_id.keys()))
    for feature in features:
        identity = feature.get("id")
        if not isinstance(identity, str) or re.fullmatch(r"F[0-9]{3}", identity) is None:
            errors.append("invalid feature ID")
        if feature.get("stage") not in stage_ids or feature.get("state") not in STATES:
            errors.append(f"invalid feature stage or state: {identity}")
        if not all(feature.get(key) for key in ("behaviour", "acceptance", "negative_acceptance", "proof_layers")):
            errors.append(f"feature lacks an acceptance contract: {identity}")
        if feature.get("state") == "passed" and not feature.get("evidence"):
            errors.append(f"feature passed without evidence: {identity}")
        if not feature.get("source_paths") or not feature.get("source_commits"):
            errors.append(f"feature lacks source provenance: {identity}")
        for dependency in feature.get("deps", []):
            target = by_id.get(dependency)
            if target is None or target["stage"] > feature.get("stage", -1):
                errors.append(f"invalid feature dependency: {identity}/{dependency}")
        for reference in feature.get("source_paths", []):
            if reference.get("repo") not in sources or not _relative(reference.get("path")):
                errors.append(f"invalid source path: {identity}")
        for reference in [*feature.get("source_commits", []), *feature.get("originating_changes", [])]:
            if reference.get("repo") not in sources or not _sha(reference.get("sha")):
                errors.append(f"invalid source revision: {identity}")
    visited, active = set(), set()

    def visit(identity):
        if identity in active:
            errors.append(f"cyclic feature dependency: {identity}")
            return
        if identity in visited or identity not in by_id:
            return
        active.add(identity)
        for dependency in by_id[identity].get("deps", []):
            visit(dependency)
        active.remove(identity)
        visited.add(identity)

    for identity in by_id:
        visit(identity)
    recorded = set()
    for change in inventory.get("changes", []):
        key = (change.get("repo"), change.get("path"))
        owners = change.get("owner_feature_ids", [])
        if key in recorded or key[0] not in sources or not _relative(key[1]):
            errors.append(f"duplicate or invalid changed path: {key}")
        recorded.add(key)
        if not owners or any(owner not in by_id for owner in owners):
            errors.append(f"changed path has no known feature owner: {key}")
        if change.get("ownership_state") == "pending_review":
            errors.append(f"changed path still needs ownership review: {key}")
    if changes is not None:
        errors.extend(f"unmapped product change: {repo}/{path}" for repo, path in sorted(changes - recorded))
        errors.extend(f"recorded path is outside the product delta: {repo}/{path}" for repo, path in sorted(recorded - changes))
    if inventory.get("user_decisions", {}).get("esp_imap_client") is not False:
        errors.append("the removed ESP IMAP client was explicitly excluded")
    return errors
