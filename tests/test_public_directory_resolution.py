"""Public dial strings share one exact and compatible directory contract."""

import pytest

from .voip_phase1_support import _load_intercom_module, roster, router

PhoneDirectory = _load_intercom_module("phone_directory").PhoneDirectory


def test_public_resolution_rejects_cross_namespace_aliases():
    rows = [roster.RosterEntry("desk", name="Reception", extension="204"),
            roster.RosterEntry("other-desk", name="204", number="Reception")]
    directory = PhoneDirectory(rows)
    for selector in ("204", "Reception"):
        with pytest.raises(roster.RosterError, match="Ambiguous"):
            directory.resolve(selector)
        decision = router.resolve_ha_router(selector, rows)
        assert decision.action is router.RouteAction.REJECT and decision.status == 485


def test_exact_public_alias_precedes_compatible_punctuation():
    rows = [roster.RosterEntry("desk_one", name="Desk one"),
            roster.RosterEntry("desk-two", name="Desk.one")]
    directory = PhoneDirectory(rows)
    assert directory.resolve("Desk.one").id == "desk-two"
    assert roster.find_entry(rows, "Desk.one").id == "desk-two"
    for resolve in (directory.resolve, lambda value: roster.find_entry(rows, value)):
        with pytest.raises(roster.RosterError, match="Ambiguous"):
            resolve("Desk%20one")


def test_internal_references_are_explicit_and_do_not_shadow_public_aliases():
    rows = [roster.RosterEntry("desk", name="Reception", metadata={
                "endpoint_id": "a52832282eca4f82a000cfacaca56d0c", "device_id": "ha-device"}),
            roster.RosterEntry("public", name="ha-device")]
    directory = PhoneDirectory(rows)
    assert directory.get("a52832282eca4f82a000cfacaca56d0c").id == "desk"
    assert directory.resolve("a52832282eca4f82a000cfacaca56d0c") is None
    assert directory.resolve_reference("a52832282eca4f82a000cfacaca56d0c").id == "desk"
    assert directory.resolve("ha-device").id == "public"
    with pytest.raises(roster.RosterError, match="Ambiguous"):
        roster.find_entry(rows, "ha-device", include_internal_ids=True)
    with pytest.raises(roster.RosterError, match="Ambiguous"):
        directory.resolve_reference("ha-device")
    assert roster.find_entry(rows, "hadevice", include_internal_ids=True).id == "public"
    assert roster.find_entry(rows, "a52832282eca4f82a000cfacaca56d0c", include_internal_ids=True).id == "desk"


def test_one_destination_can_own_several_matching_public_aliases():
    row = roster.RosterEntry("204", name="204", extension="204", number="204")
    assert PhoneDirectory([row]).resolve("204") == row


def test_internal_extension_precedes_only_an_external_contacts_number():
    internal = roster.RosterEntry("Office", extension="101", address="192.0.2.10")
    external = roster.RosterEntry("Reception", name="External reception", number="101")
    directory = PhoneDirectory([internal, external])
    assert directory.resolve("101") == internal
    assert roster.find_entry([internal, external], "101") == internal
    assert directory.resolve("Reception") == external
    assert router.resolve_ha_router("Reception", [internal, external], trunk_ready=True).target == "101"


def test_extension_does_not_override_another_accounts_explicit_public_name():
    rows = [roster.RosterEntry("Office", extension="101"), roster.RosterEntry("101", name="Other account",
            metadata={'endpoint_kind': 'sip_account', 'endpoint_id': '101'})]
    with pytest.raises(roster.RosterError, match="Ambiguous"):
        PhoneDirectory(rows).resolve("101")
