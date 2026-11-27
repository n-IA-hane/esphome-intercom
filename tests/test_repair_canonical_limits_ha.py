"""A policy-bound change reaches both validation and the native Repair flow."""

from copy import deepcopy

import pytest

from .test_duration_migration_repair_ha import blocked_repair, edit_durations, owner_choice
from .test_managed_phones_ha import custom_integration_path as custom_integration_path

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("kind", ["queue", "ring", "parking", "mailbox", "paging"])
async def test_repair_and_migration_share_the_policy_limit(hass, monkeypatch, kind):
    """Exercise changed bounds, not a second table of today's expected numbers.

    The fixture changes one canonical upper bound. Migration must enforce it,
    guidance must name it, and a correction at that exact bound must load.
    This fails if either validation or the user-facing explanation drifts.
    """
    from custom_components.voip_stack import mailbox_config, parking_config, queue_policy, ringing_policy

    maximum = 281
    invalid_value = maximum + 1
    data, rows = {}, []
    if kind == "queue":
        monkeypatch.setitem(queue_policy.QUEUE_DURATION_LIMITS, "max_wait", (1, maximum))
        key, row_kind, path = "max_wait", "group", ("policy", "max_wait")
        rows = [(row_kind, {"kind": "queue", "policy": {key: invalid_value, "ring_timeout": 0}})]
    elif kind == "ring":
        monkeypatch.setattr(ringing_policy, "RINGING_TIMEOUT_LIMITS", (0, maximum))
        key, row_kind, path = "overall_timeout", "group", ("policy", "overall_timeout")
        rows = [(row_kind, {"kind": "ring", "policy": {key: invalid_value, "step_timeout": 15}})]
    elif kind == "parking":
        monkeypatch.setitem(parking_config.PARKING_DURATION_LIMITS, "timeout", (1, maximum))
        key, row_kind, path = "timeout", None, ("parking", "timeout")
        data = {"parking": {key: invalid_value, "return_timeout": 0}}
    elif kind == "mailbox":
        monkeypatch.setattr(mailbox_config, "MAILBOX_MAX_SECONDS_LIMITS", (1, maximum))
        key, row_kind, path = "max_seconds", "phone", ("voicemail", "max_seconds")
        rows = [(row_kind, {"kind": "sip_account", "username": "Desk", "password": "synthetic-secret",
                           "voicemail": {"enabled": True, key: invalid_value}})]
    else:
        monkeypatch.setattr(ringing_policy, "RINGING_TIMEOUT_LIMITS", (0, maximum))
        key, row_kind, path = "answer_timeout", "paging", ("answer_timeout",)
        rows = [(row_kind, {"name": "Page", "members": ["Desk"], "timeout": 120, key: invalid_value})]

    entry, setup, manager, initial, _ = await blocked_repair(hass, monkeypatch, data, rows)
    form = await manager.async_configure(initial["flow_id"], {"owner": owner_choice(initial)})
    detail = form["description_placeholders"]["error"]
    assert f" {maximum} " in detail
    assert "3600" not in detail and "synthetic-secret" not in repr(form)

    before = deepcopy(entry.as_dict())
    rejected = await edit_durations(manager, form["flow_id"], {key: invalid_value})
    assert rejected["errors"] == {"base": "invalid_duration"}
    assert entry.as_dict() == before
    setup.assert_not_awaited()

    accepted = await edit_durations(manager, form["flow_id"], {key: maximum})
    assert accepted["type"] == "create_entry"
    setup.assert_awaited_once()
    value = entry.data if row_kind is None else next(
        row.data for row in entry.subentries.values() if row.subentry_type == row_kind)
    for part in path:
        value = value[part]
    assert value == maximum
