#!/usr/bin/env python3
"""Validate recovery feature ownership, optionally against frozen Git objects."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qualification.recovery_inventory import observed_changes, validate_inventory  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, default=ROOT / "qualification/recovery_inventory.json")
    parser.add_argument("--sources-root", type=Path)
    args = parser.parse_args()
    inventory = json.loads(args.inventory.read_text())
    errors = validate_inventory(inventory)
    if not errors and args.sources_root:
        errors = validate_inventory(inventory, changes=observed_changes(inventory, args.sources_root))
    for error in errors:
        print(error, file=sys.stderr)
    if not errors:
        print(json.dumps({"features": len(inventory["features"]), "product_files": len(inventory["changes"]), "git_coverage_checked": args.sources_root is not None}))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
