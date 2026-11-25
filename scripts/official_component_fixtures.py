"""Hash-verified official source snapshots for offline host/schema tests only.

Production packages select immutable official Git commits. Tests use identical
checked-in bytes so a unit run neither fetches Git nor depends on a manually
prepared checkout. Updating a pin requires updating its fixture provenance too.
"""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/official_esphome"


def verified_fixture_directory(relative, *, revision):
    directory = FIXTURES / relative
    provenance = json.loads((FIXTURES / "sources.json").read_text())
    selected = {name: entry for name, entry in provenance.items() if name.startswith(relative + "/")}
    if not selected:
        raise ValueError(f"No recorded official fixture: {relative}")
    for name, entry in selected.items():
        if entry["repository"] != "esphome/esphome" or entry["revision"] != revision:
            raise ValueError(f"Official fixture revision mismatch: {name}")
        if hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError(f"Official fixture SHA256 mismatch: {name}")
    return directory


def verified_component_directory(component):
    manifest = json.loads((ROOT / "esphome/upstream_sources.json").read_text())
    entry = manifest["official_component_pins"][component]
    directory = verified_fixture_directory("components/" + component, revision=entry["revision"])
    for filename, digest in entry["source_sha256"].items():
        if hashlib.sha256((directory / filename).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Official pin/fixture content mismatch: {component}/{filename}")
    return directory
