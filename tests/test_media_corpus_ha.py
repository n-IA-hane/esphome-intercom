"""Validate the frozen migration reference and its differential ledger.

Guarantees:
- every case is well formed and carries no private address;
- the recorded historical engine differs from the written contract only where the ledger says
  so, field by field. A difference the ledger does not list is a defect or an undeclared contract
  change. A listed difference that disappears means the rule was implemented and its ledger
  entry must be removed.

Production is independently checked against the normative cases in
``test_media_active_entrypoints_ha.py``. The historical algorithm is retired.
"""

import copy
import json
import re

import pytest

from tests.media_corpus_support import (
    CORPUS,
    GOLDEN,
    ledger_problems,
    OFFER_PRESETS,
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


def test_frozen_reference_differs_from_the_contract_only_as_the_ledger_declares():
    problems = ledger_problems(LEDGER, STEPS, OBSERVED)
    assert not problems, "\n".join(problems)


# One well known row for the negative controls: an inbound offer whose DTMF clock changes (CC-4).
CONTROL = next(index for index, row in enumerate(LEDGER)
               if (row["step"], row["field"]) == ("phone-pcma-dtmf-two-clocks#0", "dtmf.clock"))


def _altered(**changes) -> list[dict]:
    rows = copy.deepcopy(LEDGER)
    rows[CONTROL].update(changes)
    return rows


@pytest.mark.parametrize("ledger,expected", [
    (lambda: _altered(prior=123), "prior is"),
    (lambda: _altered(intended=456), "intended is"),
    (lambda: _altered(entry_point="answer to a local offer"), "entry point is"),
    (lambda: _altered(cc="CC-2"), "does not cover this field"),
    (lambda: [*LEDGER, copy.deepcopy(LEDGER[CONTROL])], "duplicate row"),
    (lambda: [row for index, row in enumerate(LEDGER) if index != CONTROL], "not in the ledger"),
], ids=["wrong prior", "wrong intended", "wrong entry point", "wrong change", "duplicate", "missing row"])
def test_ledger_check_rejects_a_falsified_ledger(ledger, expected):
    """Negative controls: each kind of wrong row must be noticed."""
    assert any(expected in problem for problem in ledger_problems(ledger(), STEPS, OBSERVED))


def test_an_error_is_not_accepted_where_a_refusal_was_declared():
    row = next(row for row in LEDGER if row["field"] == "outcome")
    observed = {**OBSERVED, row["step"]: {"outcome": "error", "error": "ValueError"}}
    assert any("prior is 'error'" in problem for problem in ledger_problems(LEDGER, STEPS, observed))


def test_frozen_reference_covers_every_case_and_recorded_offer():
    golden = json.loads(GOLDEN.read_text())
    assert set(golden) == {step.name for step in STEPS} | {"offer:" + name for name in OFFER_PRESETS}


ALIAS_CONTROL = next(index for index, row in enumerate(LEDGER) if row["field"] == "answer.payloads")


@pytest.mark.parametrize("change,expected", [
    ({"prior": [0, 8, 101]}, "prior is"),
    ({"intended": [0, 8, 101, 120]}, "intended is"),
    ({"cc": "RFC3556"}, "does not cover this field"),
    ({"entry_point": "answer to a local offer"}, "entry point is"),
])
def test_alias_ledger_rejects_wrong_frozen_prior_intended_or_authority(change, expected):
    rows = copy.deepcopy(LEDGER)
    rows[ALIAS_CONTROL].update(change)
    assert any(expected in problem for problem in ledger_problems(rows, STEPS, OBSERVED))


def test_alias_ledger_requires_one_exact_row_and_detects_stale_declaration():
    row = LEDGER[ALIAS_CONTROL]
    assert any("not in the ledger" in problem for problem in ledger_problems(
        [item for index, item in enumerate(LEDGER) if index != ALIAS_CONTROL], STEPS, OBSERVED))
    assert any("duplicate row" in problem for problem in ledger_problems(
        [*LEDGER, copy.deepcopy(row)], STEPS, OBSERVED))
    observed = copy.deepcopy(OBSERVED)
    media = next(line for line in observed[row["step"]]["answer_bytes"].split("\r\n") if line.startswith("m=audio "))
    suffix = " ".join(map(str, row["prior"]))
    changed = media[:-len(suffix)] + " ".join(map(str, row["intended"]))
    observed[row["step"]]["answer_bytes"] = observed[row["step"]]["answer_bytes"].replace(media, changed, 1)
    assert any("stale row" in problem and "answer.payloads" in problem for problem in ledger_problems(LEDGER, STEPS, observed))
