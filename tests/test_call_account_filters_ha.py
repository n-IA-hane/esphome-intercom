"""Native call filters keep HA account selection separate from SIP text."""

import pytest
import voluptuous as vol
from homeassistant.helpers.trigger import TriggerConfig

from .test_managed_phones_ha import custom_integration_path as custom_integration_path
from .test_managed_phones_ha import phone_lab as phone_lab
from .test_group_configuration_native_ha import add_account

pytestmark = pytest.mark.ha


@pytest.mark.parametrize(("options", "expected"), [
    ({"caller": "Alice"}, True),
    ({"destination": "301"}, True),
    ({"caller_number": "+3902", "caller_match": "prefix"}, True),
    ({"caller_number": "+3902000", "caller_range_end": "+3902999", "caller_match": "range"}, True),
    ({"caller_number": "+3902100", "caller_range_end": "+3902200", "caller_match": "range"}, False),
    ({"caller_number": r"\+3902[0-9]{3}", "caller_match": "pattern"}, True),
    ({"destination_extension": "300", "destination_range_end": "399", "destination_match": "range"}, True),
    ({"caller_extension": ".*", "caller_match": "pattern"}, False),
])
def test_explicit_value_modes_preserve_default_exact_aliases(options, expected):
    from custom_components.voip_stack.automation_routing import matches_call
    payload = {"caller": "Alice", "caller_number": "+3902050", "callee": "Reception", "called_extension": "301"}
    assert matches_call(payload, options) is expected


async def test_account_selector_retains_native_device_id_and_follows_username_rename(phone_lab):
    from custom_components.voip_stack.automation_routing import matches_call
    from custom_components.voip_stack.sip_account_identity import async_migrate_account_identities
    from custom_components.voip_stack.trigger import VoipCallTrigger

    lab = phone_lab
    row, device = add_account(lab, "Account @ one", extension="301")
    options = {"caller_account": device.id, "caller_extension": "301"}
    validated = await VoipCallTrigger.async_validate_config(lab.hass, {"options": options})
    assert validated["options"]["caller_account"] == device.id
    assert matches_call({"source_account_username": "Account @ one"}, options, hass=lab.hass)
    # A display name that happens to equal the selected username is insufficient.
    assert not matches_call({"caller": "Account @ one", "caller_number": "301"}, options, hass=lab.hass)
    native_id, subentry_id = device.id, row.subentry_id
    await async_migrate_account_identities(lab.hass, lab.entry,
        account_updates={row.subentry_id: {**row.data, "username": "Renamed, Account%"}})
    assert device.id == native_id and row.subentry_id == subentry_id
    assert row.data["username"] == "Renamed, Account%"
    assert lab.runtime.endpoints.by_username("Account @ one") is None
    assert matches_call({"source_account_username": "Renamed, Account%"}, options, hass=lab.hass)
    assert not matches_call({"source_account_username": "Account @ one"}, options, hass=lab.hass)


async def test_route_window_uses_the_same_account_and_extension_filters(phone_lab):
    from custom_components.voip_stack.automation_routing import RouteRequest, has_native_route_window
    from custom_components.voip_stack.runtime_data import registration_data

    lab = phone_lab
    _row, device = add_account(lab, "Literal SIP account", extension="301")
    owner = object()
    registration_data(lab.hass).route_trigger_filters[owner] = {
        "caller_account": device.id, "caller_extension": "30", "caller_match": "prefix"}
    try:
        request = RouteRequest("", "Readable caller", "Reception", source_account_username="Literal SIP account")
        assert has_native_route_window(lab.hass, request)
        spoofed = RouteRequest("", "Literal SIP account", "Reception", caller_number="301")
        assert not has_native_route_window(lab.hass, spoofed)
    finally:
        registration_data(lab.hass).route_trigger_filters.pop(owner)


@pytest.mark.parametrize("options", [
    {"caller_number": "200", "caller_range_end": "100", "caller_match": "range"},
    {"destination_extension": "100", "destination_match": "range"},
    {"caller_number": "[", "caller_match": "pattern"},
])
async def test_native_configuration_rejects_invalid_range_or_pattern(phone_lab, options):
    from custom_components.voip_stack.trigger import VoipCallTrigger
    with pytest.raises(vol.Invalid):
        await VoipCallTrigger.async_validate_config(phone_lab.hass, {"options": options})


async def test_native_account_trigger_fires_once_and_keeps_automatic_call_context(phone_lab):
    from custom_components.voip_stack import automation_context
    from custom_components.voip_stack.const import CALL_EVENT
    from custom_components.voip_stack.trigger import VoipCallTrigger
    from homeassistant.core import ServiceCall

    lab = phone_lab
    _row, device = add_account(lab, "Selected account", extension="301")
    session = lab.runtime.sip.upsert("native-account-filter", state="ringing", owner="router", callee="Reception")
    seen = []

    async def action(payload):
        call = automation_context.bind_call_action(ServiceCall(lab.hass, "voip_stack", "hangup", {}))
        seen.append((payload["call"]["source_account_username"], call.data["call_id"], call.data["expected_generation"]))
        return object()

    subject = VoipCallTrigger(lab.hass, TriggerConfig("voip_stack.call_received", options={
        "caller_account": device.id, "destination_number": "+3902", "destination_match": "prefix"}))
    detach = await subject.async_attach_runner(
        lambda payload, *_args: lab.hass.async_create_task(action(payload)))
    try:
        event = {"event_type": "ringing", "scope": "session", "direction": "incoming",
            "call_id": session.call_id, "generation": session.generation,
            "sequence": session.event_context.sequence, "callee": "Reception",
            "source_account_username": "Selected account", "called_number": "+3902000"}
        lab.hass.bus.async_fire(CALL_EVENT, event)
        lab.hass.bus.async_fire(CALL_EVENT, event)
        await lab.hass.async_block_till_done()
        assert seen == [("Selected account", session.call_id, session.generation)]
        assert subject._options["caller_account"] == device.id
    finally:
        detach()
