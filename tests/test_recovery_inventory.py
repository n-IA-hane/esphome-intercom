"""Guard against losing quarantined capabilities while rebuilding stable."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from qualification.recovery_inventory import validate_inventory


@pytest.fixture
def inventory():
    return json.loads((Path(__file__).parents[1] / "qualification/recovery_inventory.json").read_text())


def test_recovery_catalogue_has_explicit_contracts(inventory):
    assert validate_inventory(inventory) == []


def test_candidate_lock_covers_every_component_used_by_the_baseline(inventory):
    sources = json.loads((Path(__file__).parents[1] / "qualification/sources.json").read_text())
    expected = {name for name, source in inventory["sources"].items() if source["baseline_included"]}
    assert set(sources["repositories"]) == expected


def test_missing_product_file_is_not_silently_dropped(inventory):
    changes = {(item["repo"], item["path"]) for item in inventory["changes"]}
    omitted = inventory["changes"].pop()
    assert f"unmapped product change: {omitted['repo']}/{omitted['path']}" in validate_inventory(inventory, changes=changes)


def test_removed_feature_cannot_leave_unresolved_owners(inventory):
    owner = inventory["changes"][0]["owner_feature_ids"][0]
    inventory["features"] = [feature for feature in inventory["features"] if feature["id"] != owner]
    assert any("has no known feature owner" in error for error in validate_inventory(inventory))


def test_cyclic_feature_dependencies_are_rejected(inventory):
    first, second = inventory["features"][:2]
    first["deps"] = [second["id"]]
    second["deps"] = [first["id"]]
    assert any("cyclic feature dependency" in error for error in validate_inventory(inventory))


def test_final_qualification_cannot_disappear_when_it_owns_no_product_file(inventory):
    inventory["features"] = [feature for feature in inventory["features"] if feature["id"] != "F088"]
    assert any("required feature is missing: F088" in error for error in validate_inventory(inventory))


@pytest.mark.parametrize("field", ["source_paths", "source_commits"])
def test_feature_provenance_cannot_be_erased(inventory, field):
    inventory["features"][0][field] = []
    assert any("feature lacks source provenance" in error for error in validate_inventory(inventory))


@pytest.mark.parametrize("mutation", ["moving_source", "unreviewed_owner", "false_pass", "lost_negative_case", "esp_imap", "ha_imap", "imap_scope"])
def test_incomplete_recovery_contract_fails(inventory, mutation):
    candidate = deepcopy(inventory)
    if mutation == "moving_source":
        candidate["sources"]["esphome-intercom"]["quarantine_sha"] = "dev"
    elif mutation == "unreviewed_owner":
        candidate["changes"][0]["ownership_state"] = "pending_review"
    elif mutation == "false_pass":
        candidate["features"][0]["state"] = "passed"
    elif mutation == "lost_negative_case":
        candidate["features"][0]["negative_acceptance"] = []
    elif mutation == "esp_imap":
        candidate["user_decisions"]["esp_imap_client"] = True
    elif mutation == "ha_imap":
        candidate["user_decisions"]["ha_imap_server"] = True
    else:
        next(feature for feature in candidate["features"] if feature["id"] == "F043")["state"] = "pending"
    assert validate_inventory(candidate)
