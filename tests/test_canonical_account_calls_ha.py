"""Upgrade existing HA receivers, then exercise their public SIP call paths."""

from dataclasses import replace

import pytest

from custom_components.voip_stack.account_runtime import recompute_account_availability
from custom_components.voip_stack.ha_bindings import HaCardBindings
from custom_components.voip_stack.phone_config import migrate_browser_accounts, endpoint_from_subentry
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
    fixture.browser_subentry = entry.subentries[before]
    assert fixture.browser_subentry.data['kind'] == 'sip_account'
    # The transfer peer fixture creates independent runtime-only SIP accounts.
    # Replace only the migrated subject; a full config sync deliberately removes
    # those unconfigured peers and would test fixture deletion, not upgrade.
    previous = fixture.runtime.endpoints.require('casa')
    fixture.runtime.endpoints.upsert(replace(endpoint_from_subentry(entry, fixture.browser_subentry),
        device_id=previous.device_id, entity_ids=previous.entity_ids))
    bindings = HaCardBindings(fixture.hass, entry, fixture.runtime,
        lambda ids: recompute_account_availability(fixture.hass, ids))
    fixture.runtime.ha_bindings = bindings
    await bindings.async_start()
    recompute_account_availability(fixture.hass)
    assert fixture.runtime.endpoints.require('casa').availability.value == 'available'
    yield fixture
    await bindings.async_close()


@pytest.mark.parametrize('trunks', [True], indirect=True)
@pytest.mark.parametrize('transport', ['udp', 'tcp'])
async def test_canonical_saved_account_without_browser_uses_its_noanswer_policy(
        canonical_browser, registered_phones, transport):
    await no_card_policy(canonical_browser, registered_phones, transport)


@pytest.mark.parametrize('trunks', [True], indirect=True)
async def test_canonical_account_presence_is_not_its_availability(canonical_browser, hass_ws_client):
    await card_presence(canonical_browser, hass_ws_client)


@pytest.mark.parametrize('trunks', [True], indirect=True)
@pytest.mark.parametrize('logical_browser', [{'ringing_timeout': 3}], indirect=True)
@pytest.mark.parametrize('transport', ['udp', 'tcp'])
async def test_canonical_late_card_answers_same_call_and_cancels_fallback(
        canonical_browser, registered_phones, hass_ws_client, transport):
    await late_answer(canonical_browser, registered_phones, hass_ws_client, transport)
