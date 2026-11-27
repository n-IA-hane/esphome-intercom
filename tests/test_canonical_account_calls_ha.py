"""Upgrade existing HA receivers, then exercise their public SIP call paths."""

import asyncio
from dataclasses import replace

import pytest

from custom_components.voip_stack.account_runtime import recompute_account_availability
from custom_components.voip_stack.ha_bindings import HaCardBindings
from custom_components.voip_stack.phone_config import migrate_browser_accounts, endpoint_from_subentry
from custom_components.voip_stack.sip_account_identity import async_migrate_account_identities
from custom_components.voip_stack.store import config_entry
from .test_browser_logical_availability_ha import (
    logical_browser as logical_browser,
    test_late_card_can_publicly_answer_and_cancel_noanswer as late_answer,
    test_no_card_uses_noanswer_instead_of_unavailable_policy as no_card_policy,
    test_public_snapshot_and_card_lease_do_not_change_enabled_state as card_presence,
    registered_phones as registered_phones,
    trunks as trunks,
    lab as lab,
)

pytestmark = pytest.mark.ha


@pytest.fixture
async def canonical_browser(logical_browser):
    fixture = logical_browser
    entry = config_entry(fixture.hass)
    before = fixture.browser_subentry.subentry_id
    migrate_browser_accounts(fixture.hass, entry)
    await async_migrate_account_identities(fixture.hass, entry)
    fixture.browser_subentry = entry.subentries[before]
    assert fixture.browser_subentry.data['kind'] == 'sip_account'
    # The transfer peer fixture creates independent runtime-only SIP accounts.
    # Replace only the migrated subject; a full config sync deliberately removes
    # those unconfigured peers and would test fixture deletion, not upgrade.
    configured = endpoint_from_subentry(entry, fixture.browser_subentry)
    previous = fixture.runtime.endpoints.require(configured.endpoint_id)
    fixture.runtime.endpoints.upsert(replace(configured,
        device_id=previous.device_id, entity_ids=previous.entity_ids))
    fixture.browser_endpoint_id = fixture.runtime.endpoints.require(configured.endpoint_id).endpoint_id
    assert fixture.browser_endpoint_id == configured.endpoint_id
    assert fixture.browser_endpoint_id == fixture.browser_subentry.data['username']
    bindings = HaCardBindings(fixture.hass, entry, fixture.runtime,
        lambda ids: recompute_account_availability(fixture.hass, ids))
    fixture.runtime.ha_bindings = bindings
    await bindings.async_start()
    recompute_account_availability(fixture.hass)
    assert fixture.runtime.endpoints.require(fixture.browser_endpoint_id).availability.value == 'available'
    yield fixture
    await bindings.async_close()


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_named_destination_calls_its_registered_account_with_a_uri_shaped_name(
    canonical_browser, registered_phones,
):
    from custom_components.voip_stack.endpoint_device import async_ensure_endpoint_device
    from custom_components.voip_stack.endpoint_routing import roster_from_peers
    from .test_browser_logical_availability_ha import resources

    lab = canonical_browser
    registrar = lab.calls.component("registrar")
    original_account = registrar.accounts["transfer_b"]
    registry = lab.runtime.endpoints
    registry.update("transfer_b", name="Reception@Hotel")
    async_ensure_endpoint_device(lab.hass, config_entry(lab.hass), registry.require("transfer_b"), registry)
    lab.runtime.directory.replace(roster_from_peers(lab.hass, [], registrar.registered_roster_entries()))
    reply = await lab.hass.services.async_call("voip_stack", "call", {
        "device_id": "browser-casa", "destination": "Reception@Hotel", "media_client_id": "operator-card",
    }, blocking=True, return_response=True)
    call_id = reply["call"]["call_id"]
    try:
        await asyncio.to_thread(registered_phones[1].wait_for, "Call established", 4)
        assert "Incoming call" not in registered_phones[2].read()
        assert registrar.accounts["transfer_b"] == original_account
    finally:
        await lab.hass.services.async_call("voip_stack", "hangup", {
            "device_id": "browser-casa", "call_id": call_id,
        }, blocking=True)
    async with asyncio.timeout(3):
        while lab.calls.sessions:
            await asyncio.sleep(.01)
    assert resources(lab)["call_scoped_quiescent"]


@pytest.mark.parametrize('trunks', [True], indirect=True)
@pytest.mark.parametrize('transport', ['udp', 'tcp'])
async def test_canonical_saved_account_without_browser_uses_its_noanswer_policy(
        canonical_browser, registered_phones, transport):
    await no_card_policy(canonical_browser, registered_phones, transport)


@pytest.mark.parametrize('trunks', [True], indirect=True)
async def test_canonical_account_presence_is_not_its_availability(canonical_browser, hass_ws_client):
    await card_presence(canonical_browser, hass_ws_client)


@pytest.mark.parametrize('trunks', [True], indirect=True)
@pytest.mark.parametrize('logical_browser', [{'ringing_timeout': 3, 'extension': '101'}], indirect=True)
@pytest.mark.parametrize(('transport', 'dial_target'), [('udp', 'Casa'), ('tcp', '101')])
async def test_canonical_late_card_answers_same_call_and_cancels_fallback(
        canonical_browser, registered_phones, hass_ws_client, transport, dial_target):
    assert canonical_browser.browser_subentry.data["username"] == "Casa"
    await late_answer(canonical_browser, registered_phones, hass_ws_client, transport, dial_target)
