"""Native adoption and explicit physical key order have one clear operation."""


from copy import deepcopy

from homeassistant.config_entries import ConfigSubentry
import pytest
import voluptuous as vol

from .test_physical_phone_flow_ha import physical_flow as physical_flow, identity, start, submit
from .test_managed_phones_ha import phone_lab as phone_lab, custom_integration_path as custom_integration_path

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("assigned", [False, True])
async def test_one_form_adoption_uses_native_identity_and_optional_account(physical_flow, assigned):
    from custom_components.voip_stack.physical_phone import physical_phone_data

    state = physical_flow
    before = set(state.lab.entry.subentries)
    form = await start(state)
    optional_account = next(value for key, value in form["data_schema"].schema.items() if key.schema == "account_device")
    assert optional_account.selector_type == "device"
    assert "account_device" not in form["data_schema"]({"profile_id": "fanvil/x3u-pro"})
    device = state.lab.runtime.endpoints.get("481").device_id
    result = await submit(state, form, identity(**({"account_device": device} if assigned else {})))
    assert result["type"] == "abort" and result["reason"] == "phone_configured"
    (record,) = [value for key, value in state.lab.entry.subentries.items() if key not in before]
    assert record.subentry_type == "physical_phone"
    assert record.unique_id == "physical_phone:" + record.subentry_id
    assert "phone_id" not in record.data
    data = physical_phone_data(record)
    assert data["phone_id"] == record.subentry_id and not data["keys"]
    assert data["lines"] == ([{"slot": "1", "endpoint_id": "481", "transport": "udp"}] if assigned else [])


@pytest.mark.parametrize("scenario", [
    "sparse_drag", "add_lower", "add_and_drag", "remove_and_drag", "clear_all",
    "duplicate", "restricted_function", "first_bindings", "explicit_swap", "replace_same_slot",
    "change_and_drag", "target_edit_and_drag", "unknown_origin", "duplicate_origin", "legacy_payload",
])
async def test_physical_key_rows_keep_explicit_new_slots_and_reorder_managed_keys(physical_flow, scenario):
    from custom_components.voip_stack.physical_phone import normalize_physical_phone, physical_phone_data

    state = physical_flow
    caps = state.catalog.profiles["fanvil/x3u-pro"][0]["capabilities"]
    caps["key_slots"] = [{**caps["key_slots"][0], "slot": str(pos)} for pos in range(1, 8)]
    caps["clear_keys"] = True
    line = {"slot": "2", "line": "1", "function": "line", "label": "Reception"}
    watch = {"slot": "7", "line": "2", "function": "blf", "target": "481", "label": "Night BLF"}
    dial = {"slot": "1", "line": "1", "function": "speed_dial", "target": "482", "label": "Speed"}
    old_keys = [line, watch]
    submitted, expected = {
        "sparse_drag": ([watch, line], [dict(watch, slot="2"), dict(line, slot="7")]),
        "add_lower": ([line, watch, dial], [dial, line, watch]),
        "add_and_drag": ([watch, dial, line], [dial, dict(watch, slot="2"), dict(line, slot="7")]),
        "remove_and_drag": ([watch, line], [dict(watch, slot="2"), dict(line, slot="7")]),
        "clear_all": ([], []),
        "duplicate": ([line, dict(watch, slot="2")], None),
        "restricted_function": ([watch, line], None),
        "first_bindings": ([watch, line], [line, watch]),
        "explicit_swap": ([dict(line, slot="7"), dict(watch, slot="2")], [dict(watch, slot="2"), dict(line, slot="7")]),
        "replace_same_slot": ([watch, dict(dial, slot="2")], [dict(dial, slot="2"), watch]),
        "change_and_drag": ([watch, dict(line, slot="5"), dial], [dict(watch, slot="1"), dict(line, slot="5"), dict(dial, slot="7")]),
        "target_edit_and_drag": ([dict(watch, target="482"), line], [dict(watch, slot="2", target="482"), dict(line, slot="7")]),
        "unknown_origin": ([line, watch], None),
        "duplicate_origin": ([line, watch], None),
        "legacy_payload": ([watch, line], [line, watch]),
    }[scenario]
    if scenario == "first_bindings":
        old_keys = []
    elif scenario == "change_and_drag":
        old_keys.append(dial)
    elif scenario == "remove_and_drag":
        old_keys.append(dict(dial, slot="4"))
    elif scenario == "restricted_function":
        caps["key_slots"][1]["functions"] = ["line"]
    original = normalize_physical_phone({
        **identity(), "phone_id": "legacy-physical", "catalog_commit": "a" * 40,
        "lines": [{"slot": "1", "endpoint_id": "481", "transport": "udp"},
                  {"slot": "2", "endpoint_id": "482", "transport": "tcp"}],
        "keys": old_keys,
    })
    record = ConfigSubentry(subentry_type="physical_phone", title=original["name"], data=deepcopy(original),
                            unique_id="physical_phone:legacy-physical")
    state.lab.hass.config_entries.async_add_subentry(state.lab.entry, record)

    async def editor():
        menu = await state.lab.hass.config_entries.subentries.async_init(
            (state.lab.entry.entry_id, "physical_phone"),
            context={"source": "reconfigure", "subentry_id": record.subentry_id})
        assert menu["menu_options"] == ["user", "bindings"]
        return await submit(state, menu, {"next_step_id": "bindings"})

    form = await editor()
    marker, selector = next((k, v) for k, v in form["data_schema"].schema.items() if k.schema == "keys")
    assert "_original_slot" not in selector.serialize()["selector"]["object"]["fields"]
    defaults = {row["label"]:row for row in marker.default()}
    assert all(row["_original_slot"] == row["slot"] for row in defaults.values())
    submitted = deepcopy(submitted)
    for row in submitted:
        if row["label"] in defaults and scenario != "legacy_payload":
            row["_original_slot"] = defaults[row["label"]]["_original_slot"]
    if scenario == "unknown_origin":
        submitted[0]["_original_slot"] = "6"
    elif scenario == "duplicate_origin":
        submitted[0]["_original_slot"] = submitted[1]["_original_slot"]
    result = await submit(state, form, {"lines": original["lines"], "keys": submitted})
    saved = physical_phone_data(state.lab.entry.subentries[record.subentry_id])
    if expected is None:
        reasons = {"duplicate": "invalid_phone_key", "restricted_function": "phone_key_unsupported",
                   "unknown_origin": "invalid_phone_key_order", "duplicate_origin": "invalid_phone_key_order"}
        assert result["errors"]["base"] == reasons[scenario]
        assert saved == original
        if scenario == "restricted_function":
            # The draft now displays attempted destinations. Dragging the first
            # two rows back must use these fresh origins, saving the valid layout.
            schema_rows = next(k.default() for k in result["data_schema"].schema if k.schema == "keys")
            assert [(row["_original_slot"], row["slot"]) for row in schema_rows] == [("2", "2"), ("7", "7")]
            retried = await submit(state, result, {"lines": original["lines"], "keys": list(reversed(schema_rows))})
            assert retried["reason"] == "reconfigure_successful"
            assert physical_phone_data(state.lab.entry.subentries[record.subentry_id])["keys"] == original["keys"]
        return
    assert result["type"] == "abort" and result["reason"] == "reconfigure_successful"
    assert saved["keys"] == [{"target": "", **key} for key in expected]
    for field in ("phone_id", "mac", "provisioning_token", "lines", "catalog_commit"):
        assert saved[field] == original[field]
    reopened = await editor()
    rendered = next(key.default() for key in reopened["data_schema"].schema if key.schema == "keys")
    assert [(row["slot"], row["line"], row["label"]) for row in rendered] == [
        (row["slot"], row["line"], row["label"]) for row in expected]


@pytest.mark.parametrize("case", ["one_line", "two_lines", "no_lines", "choose_line", "free_hole",
                                  "new_drag", "restricted_positions", "full"])
async def test_native_add_key_uses_bound_sip_accounts_and_display_order(physical_flow, case):
    from custom_components.voip_stack.physical_phone import normalize_physical_phone, physical_phone_data

    state = physical_flow
    caps = state.catalog.profiles["fanvil/x3u-pro"][0]["capabilities"]
    template = caps["key_slots"][0]
    caps["key_slots"] = [dict(template, slot=str(pos)) for pos in range(1, 5)]
    if case == "restricted_positions":
        caps["key_slots"][1] = dict(template, slot="2", functions=["line"])
    caps["clear_keys"] = True
    lines = [{"slot":"2", "endpoint_id":"481", "transport":"udp"}]
    if case in {"two_lines", "choose_line"}:
        lines.append({"slot":"4", "endpoint_id":"482", "transport":"tcp"})
    if case == "no_lines":
        lines = []
    old_slots = {"free_hole":[1,4], "new_drag":[1,3], "restricted_positions":[1,4],
                 "full":[1,2,3,4]}.get(case, [])
    keys = [{"slot":str(slot), "line":"2", "function":"line", "label":f"Line {slot}"} for slot in old_slots]
    original = normalize_physical_phone({**identity(), "phone_id":"public-keys", "catalog_commit":"a"*40,
                                         "lines":lines, "keys":keys})
    record = ConfigSubentry(subentry_type="physical_phone", title=original["name"], data=deepcopy(original),
                            unique_id="physical_phone:public-keys")
    state.lab.hass.config_entries.async_add_subentry(state.lab.entry, record)
    async def open_form():
        menu = await state.lab.hass.config_entries.subentries.async_init((state.lab.entry.entry_id, "physical_phone"),
            context={"source":"reconfigure","subentry_id":record.subentry_id})
        return await submit(state, menu, {"next_step_id":"bindings"})
    form = await open_form()
    selector = next(v for k,v in form["data_schema"].schema.items() if k.schema == "keys")
    fields = selector.serialize()["selector"]["object"]["fields"]
    assert "slot" not in fields and "_original_slot" not in fields
    if len(lines) > 1:
        options = fields["line"]["selector"]["select"]["options"]
        assert [item["value"] for item in options] == ["2", "4"]
        assert "Front desk" in options[0]["label"] and "Office" in options[1]["label"]
    else:
        assert "line" not in fields
    raw = form["data_schema"]({})
    key = {"function":"Speed dial", "target":"200","label":"New"}
    if case == "two_lines":
        key["line"] = "4"
    if case == "new_drag":
        raw["keys"].insert(0, key)
    else:
        raw["keys"].append(key)
    result = await state.lab.hass.config_entries.subentries.async_configure(form["flow_id"], raw)
    stored = physical_phone_data(state.lab.entry.subentries[record.subentry_id])
    if case in {"no_lines","choose_line","full"}:
        assert result["errors"] == {"base":"phone_key_unsupported" if case == "full" else "phone_key_line_required"}
        assert stored == original
        return
    assert result["type"] == "abort" and result["reason"] == "reconfigure_successful"
    expected_order = (["New", "Line 1", "Line 3"] if case == "new_drag" else
                      [*(f"Line {n}" for n in old_slots), "New"])
    assert [r["label"] for r in stored["keys"]] == expected_order
    expected_slots = {"new_drag":["1","3","4"], "restricted_positions":["1","2","4"],
                      "free_hole":["1","2","4"]}.get(case, ["1"])
    assert [r["slot"] for r in stored["keys"]] == expected_slots
    new_key = next(r for r in stored["keys"] if r["label"]=="New")
    assert (new_key["line"],new_key["target"]) == ("4" if case=="two_lines" else "2","200")
    assert stored["lines"] == original["lines"]
    reopened = await open_form()
    assert [r["label"] for r in reopened["data_schema"]({})["keys"]] == expected_order


async def test_admin_password_is_not_prefilled_and_explicit_username_clear_removes_credentials(physical_flow):
    from custom_components.voip_stack.physical_phone import draft_physical_subentry, normalize_physical_phone, physical_phone_data

    state = physical_flow
    data = normalize_physical_phone({**identity(), "catalog_commit": "a" * 40,
        "admin_username": "fixture-admin", "admin_password": "fixture-private"})
    record = draft_physical_subentry(data)
    state.lab.hass.config_entries.async_add_subentry(state.lab.entry, record)
    form = await state.lab.hass.config_entries.subentries.async_init(
        (state.lab.entry.entry_id, "physical_phone"), context={"source": "reconfigure", "subentry_id": record.subentry_id})
    form = await submit(state, form, {"next_step_id": "user"})
    advanced = next(value for key, value in form["data_schema"].schema.items() if key.schema == "advanced")
    fields = {key.schema: key for key in advanced.schema.schema}
    assert fields["admin_username"].default() == "fixture-admin"
    assert fields["admin_password"].default is vol.UNDEFINED
    result = await submit(state, form, identity(advanced={"admin_username": "fixture-admin", "admin_password": ""}))
    assert result["type"] == "abort"
    saved = physical_phone_data(state.lab.entry.subentries[record.subentry_id])
    assert saved["admin_password"] == "fixture-private"

    form = await state.lab.hass.config_entries.subentries.async_init(
        (state.lab.entry.entry_id, "physical_phone"), context={"source": "reconfigure", "subentry_id": record.subentry_id})
    form = await submit(state, form, {"next_step_id": "user"})
    result = await submit(state, form, identity(advanced={"admin_username": "", "admin_password": ""}))
    assert result["type"] == "abort"
    cleared = physical_phone_data(state.lab.entry.subentries[record.subentry_id])
    assert cleared["admin_username"] == cleared["admin_password"] == ""
    for field in ("phone_id", "provisioning_token", "mac", "catalog_commit", "lines", "keys"):
        assert cleared[field] == saved[field]
