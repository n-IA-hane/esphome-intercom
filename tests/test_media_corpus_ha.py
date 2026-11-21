"""The media corpus against the engine in production.

Guarantees:
- every case is well formed and carries no private address;
- the current engine differs from the written contract only where the differential ledger says
  so, field by field. A difference the ledger does not list is a defect or an undeclared contract
  change. A listed difference that disappears means the rule was implemented and its ledger
  entry must be removed.

A golden snapshot records what the current engine answers for every case, so a refactoring
that claims to change nothing can prove it.
"""

import json
import os
import re

import pytest

from tests.media_corpus_support import CORPUS, GOLDEN, load_steps, mismatches, run_legacy

pytestmark = pytest.mark.ha

STEPS = load_steps()
PRIVATE_ADDRESS = re.compile(rb"\b(?:10\.\d+|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d+\.\d+\b")


LEDGER = json.loads((CORPUS / "ledger.json").read_text())
DECLARED = {"CC-1", "CC-2", "CC-4", "CC-5", "CC-11"}


def _field(mismatch: str) -> str:
    return mismatch.split(":", 1)[0]


def test_corpus_is_not_empty():
    assert len(STEPS) >= 20


@pytest.mark.parametrize("path", sorted((CORPUS / "cases").glob("*/case.json")), ids=lambda path: path.parent.name)
def test_case_is_well_formed(path):
    case = json.loads(path.read_text())
    assert case["id"] == path.parent.name
    assert case["source"]["kind"] in {"capture", "reconstructed", "synthetic"}
    assert case["steps"], "a case needs at least one step"
    assert len(case.get("expect") or []) == len(case["steps"]), "one expectation per step"
    assert case["rules"], "a case names the contract rules it defends"
    for step in case["steps"]:
        assert step["op"] in {"answer_offer", "apply_answer"}
        for key in ("sdp", "local_offer"):
            if key not in step:
                continue
            body = (path.parent / step[key]).read_bytes()
            assert body.startswith(b"v=0"), f"{step[key]} is not a session description"
            assert not PRIVATE_ADDRESS.search(body), f"{step[key]} carries a private address"
    for extra in path.parent.glob("*.sdp"):
        assert not PRIVATE_ADDRESS.search(extra.read_bytes()), f"{extra.name} carries a private address"


@pytest.mark.parametrize("step", STEPS, ids=lambda step: step.name)
def test_case_differs_from_the_contract_only_as_the_ledger_declares(step):
    assert step.expect, "the case states no expectation for this step"
    actual = run_legacy(step)
    found = {_field(item): item for item in mismatches(step.expect, actual)}
    declared = {entry["field"]: entry for entry in LEDGER if entry["step"] == step.name}
    undeclared = sorted(set(found) - set(declared))
    assert not undeclared, "not in the ledger: " + "; ".join(found[field] for field in undeclared)
    stale = sorted(set(declared) - set(found))
    assert not stale, "ledger entries no longer needed: " + ", ".join(stale)


def test_ledger_entries_name_a_declared_contract_change():
    names = {step.name for step in STEPS}
    for entry in LEDGER:
        assert entry["step"] in names, entry
        assert entry["cc"] in DECLARED, entry
        assert {"entry_point", "field", "prior", "intended"} <= entry.keys(), entry


def test_current_engine_matches_its_golden_snapshot():
    actual = {step.name: run_legacy(step) for step in STEPS}
    if os.environ.get("MEDIA_CORPUS_WRITE_GOLDEN") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(actual, indent=1, sort_keys=True) + "\n")
    golden = json.loads(GOLDEN.read_text())
    changed = sorted(name for name in actual.keys() | golden.keys() if actual.get(name) != golden.get(name))
    assert not changed, "engine output differs from the golden snapshot for: " + ", ".join(changed)
