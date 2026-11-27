#!/usr/bin/env python3
"""Read-only copied-storage preflight for the 7.5/7.6 to 7.7 policy bridge.

No setup or network connection runs. Reports contain changed paths and failure
classes, never configuration values. A pass validates a plan, not deployment.
"""
from __future__ import annotations
import argparse
from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

sys.dont_write_bytecode = True
REPOSITORY = Path(__file__).resolve().parents[1]

def _entry(raw):
    if not isinstance(raw, Mapping) or not isinstance(raw.get("data"), Mapping):
        raise ValueError("Invalid configuration entry")
    subentries = raw.get("subentries", [])
    if not isinstance(subentries, list):
        raise ValueError("Invalid native subentries")
    rows = {}
    for item in subentries:
        if (not isinstance(item, Mapping) or not isinstance(item.get("data"), Mapping)
                or not isinstance(item.get("subentry_id"), str)
                or not isinstance(item.get("subentry_type"), str)
                or item["subentry_id"] in rows):
            raise ValueError("Invalid native subentry")
        rows[item["subentry_id"]] = SimpleNamespace(
            subentry_id=item["subentry_id"], subentry_type=item["subentry_type"],
            title=str(item.get("title") or ""), unique_id=item.get("unique_id"), data=deepcopy(dict(item["data"])),
        )
    version, minor = raw.get("version"), raw.get("minor_version", 1)
    if (isinstance(version, bool) or not isinstance(version, int)
            or isinstance(minor, bool) or not isinstance(minor, int)):
        raise ValueError("Invalid schema version")
    return SimpleNamespace(entry_id=str(raw.get("entry_id") or ""), title=str(raw.get("title") or "VoIP Stack"),
        version=version, minor_version=minor, data=deepcopy(dict(raw["data"])),
        options=deepcopy(raw.get("options", {})), subentries=rows)

def _changed_paths(before, after, path=()):
    """Return locations only: never serialize configuration values."""
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        changes = []
        for key in sorted(before.keys() | after.keys()):
            if key not in before or key not in after:
                changes.append([*path, key])
            else:
                changes.extend(_changed_paths(before[key], after[key], (*path, key)))
        return changes
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        return [change for index, (left, right) in enumerate(zip(before, after, strict=True))
                for change in _changed_paths(left, right, (*path, index))]
    return [list(path)] if before != after else []

def preflight_store(payload):
    """Run the same detached plan used by migration and Repairs."""
    from custom_components.voip_stack.duration_config import duration_migration_plan
    from custom_components.voip_stack.duration_repair import duration_repair_owners
    if not isinstance(payload, Mapping):
        raise ValueError("Expected copied config-entry storage")
    if payload.get("domain") == "voip_stack":
        raw_entries = [payload]
    else:
        data = payload.get("data", payload)
        raw_entries = data.get("entries") if isinstance(data, Mapping) else None
    if not isinstance(raw_entries, list):
        raise ValueError("Expected copied core.config_entries entries")
    results = []
    for raw in raw_entries:
        if not isinstance(raw, Mapping) or raw.get("domain") != "voip_stack":
            continue
        entry = _entry(raw)
        supported = entry.version == 7 and entry.minor_version in (5, 6, 7)
        result = {"entry_id": entry.entry_id, "schema": [entry.version, entry.minor_version],
                  "target_schema": [7, 7], "prerequisite_migrations_required": not supported}
        try:
            owners = duration_repair_owners(entry)
            result["invalid_duration_owners"] = [{"subentry_id": owner.subentry_id, "kind": owner.kind,
                "path": list(owner.path), "fields": [field.name for field in owner.fields]} for owner in owners]
            proposed, updates = duration_migration_plan(entry)
        except (ValueError, TypeError, KeyError, OSError, RuntimeError, OverflowError) as error:
            result.update(status="blocked", error_type=type(error).__name__)
        else:
            # Runtime must also move a residual legacy parent trunk into a
            # native subentry. The pure validation plan cannot allocate that
            # HA identity, so disclose the required move instead of claiming
            # that an already initialized numeric 7.7 is fully migrated.
            parent_trunk_move = "trunk_enabled" in entry.data
            changed = proposed != entry.data or bool(updates) or parent_trunk_move
            result.update(entry_fields=_changed_paths(entry.data, proposed),
                subentries=[{"subentry_id": row.subentry_id, "fields": _changed_paths(row.data, data)}
                            for row, data in updates], stored_postconditions_satisfied=not changed,
                parent_trunk_move_required=parent_trunk_move,
                migration_required=entry.minor_version < 7 or changed)
            result["status"] = "passed" if supported and (entry.minor_version < 7 or not changed) else "blocked"
            if entry.minor_version == 7 and changed:
                result["recovery_required"] = "Settings > System > Repairs"
        results.append(result)
    if not results:
        raise ValueError("No VoIP Stack entry found")
    return {"read_only": True, "scope": "7.5/7.6 upgrade validation and 7.7 duration and retired-trunk postconditions",
            "status": "passed" if all(row["status"] == "passed" for row in results) else "blocked", "entries": results}

def source_fingerprint():
    digest = hashlib.sha256()
    digest.update(Path(__file__).read_bytes() + b"\0")
    for source in sorted((REPOSITORY / "custom_components" / "voip_stack").rglob("*.py")):
        digest.update(source.relative_to(REPOSITORY).as_posix().encode() + b"\0" + source.read_bytes() + b"\0")
    return digest.hexdigest()

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Copy of core.config_entries or one exported VoIP Stack entry")
    parser.add_argument("--output", type=Path, help="Create a new report; never overwrite an existing file")
    args = parser.parse_args(argv)
    if args.output is not None and args.output.resolve() == args.config.resolve():
        parser.error("Report output must be different from the input copy")
    try:
        original = args.config.read_bytes()
        payload = json.loads(original)
        sys.path.insert(0, str(REPOSITORY))
        source_before = source_fingerprint()
        report = preflight_store(payload)
        report["input_sha256"] = hashlib.sha256(original).hexdigest()
        report["source_sha256"] = source_before
        if source_fingerprint() != source_before:
            raise RuntimeError("Source changed during preflight")
        if args.config.read_bytes() != original:
            raise RuntimeError("Input copy changed during preflight")
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
        report = {"read_only": True, "status": "invalid_input", "error_type": type(error).__name__}
    encoded = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.output is not None:
        with args.output.open("x", encoding="utf-8") as target:
            target.write(encoded)
    else:
        print(encoded, end="")
    return 0 if report["status"] == "passed" else 2

if __name__ == "__main__":
    raise SystemExit(main())
