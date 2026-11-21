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

import copy
import json
import re

import pytest

from tests.media_corpus_support import (
    CORPUS,
    GOLDEN,
    ledger_problems,
    legacy_snapshot,
    load_steps,
    run_legacy,
)

pytestmark = pytest.mark.ha

STEPS = load_steps()
PRIVATE_ADDRESS = re.compile(rb"\b(?:10\.\d+|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d+\.\d+\b")


LEDGER = json.loads((CORPUS / "ledger.json").read_text())
OBSERVED = {step.name: run_legacy(step) for step in STEPS}


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


def test_engine_differs_from_the_contract_only_as_the_ledger_declares():
    problems = ledger_problems(LEDGER, STEPS, OBSERVED)
    assert not problems, "\n".join(problems)


def _altered(index: int, **changes) -> list[dict]:
    rows = copy.deepcopy(LEDGER)
    rows[index].update(changes)
    return rows


@pytest.mark.parametrize("ledger,expected", [
    (lambda: _altered(0, prior=123), "prior is"),
    (lambda: _altered(0, intended=456), "intended is"),
    (lambda: _altered(0, entry_point="answer to a local offer"), "entry point is"),
    (lambda: _altered(0, cc="CC-2"), "does not cover this field"),
    (lambda: [*LEDGER, copy.deepcopy(LEDGER[0])], "duplicate row"),
    (lambda: LEDGER[1:], "not in the ledger"),
], ids=["wrong prior", "wrong intended", "wrong entry point", "wrong change", "duplicate", "missing row"])
def test_ledger_check_rejects_a_falsified_ledger(ledger, expected):
    """Negative controls: each kind of wrong row must be noticed."""
    assert any(expected in problem for problem in ledger_problems(ledger(), STEPS, OBSERVED))


def test_an_error_is_not_accepted_where_a_refusal_was_declared():
    row = next(row for row in LEDGER if row["field"] == "outcome")
    observed = {**OBSERVED, row["step"]: {"outcome": "error", "error": "ValueError"}}
    assert any("prior is 'error'" in problem for problem in ledger_problems(LEDGER, STEPS, observed))


def test_current_engine_matches_its_golden_snapshot():
    """Complete written descriptions included. Regenerate only with
    ``python -m tests.media_corpus_support --write-golden`` and review the diff."""
    actual = legacy_snapshot()
    golden = json.loads(GOLDEN.read_text())
    changed = sorted(name for name in actual.keys() | golden.keys() if actual.get(name) != golden.get(name))
    assert not changed, "engine output differs from the golden snapshot for: " + ", ".join(changed)
