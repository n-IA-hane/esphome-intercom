"""Owner-configured ringing limits preserve zero, inheritance and native owners."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant.common import MockConfigEntry

from custom_components.voip_stack.phone_policy import PhonePolicy
from custom_components.voip_stack.phone_policy_runtime import apply_phone_policy_state
from custom_components.voip_stack.runtime_data import runtime_data
from .test_phone_policy_runtime_ha import ringing as ringing

pytestmark = pytest.mark.ha


def test_zero_and_unpaired_no_answer_are_valid_configuration():
    unlimited = PhonePolicy.from_data({"ringing_timeout": 0, "no_answer": {"kind": "hangup"}})
    assert unlimited.as_data()["ringing_timeout"] == 0
    assert PhonePolicy(ringing_timeout=23).no_answer is None
    assert PhonePolicy.from_data({"no_answer": {"kind": "hangup"}}).ringing_timeout is None


async def test_inherited_global_budget_ends_without_requiring_fallback(hass, ringing):
    runtime_data(hass).transport_config["default_ringing_timeout"] = 120
    ringing.endpoints.update("kitchen", policy=PhonePolicy())
    apply_phone_policy_state(hass, ringing.payload)
    timer = ringing.calls.resource_for("call-one", "deadline:phone_policy")
    assert timer is not None, "An inherited global budget must own the existing call deadline"
    timer._expired()
    await asyncio.wait_for(ringing.ended.wait(), 1)
    assert ringing.session.termination_intent.reason == "no_answer"
    assert ringing.session.termination_intent.response_status == 480


@pytest.mark.parametrize("global_value,account_value,armed", [(120, 0, False), (0, None, False), (0, 23, True)])
async def test_global_and_account_zero_are_distinct_from_inheritance(hass, ringing, global_value, account_value, armed):
    runtime_data(hass).transport_config["default_ringing_timeout"] = global_value
    ringing.endpoints.update("kitchen", policy=PhonePolicy(ringing_timeout=account_value))
    apply_phone_policy_state(hass, ringing.payload)
    assert bool(ringing.calls.resource_for("call-one", "deadline:phone_policy")) is armed


async def test_global_edit_does_not_restart_already_ringing_call(hass, ringing):
    runtime = runtime_data(hass)
    runtime.transport_config["default_ringing_timeout"] = 120
    ringing.endpoints.update("kitchen", policy=PhonePolicy())
    apply_phone_policy_state(hass, ringing.payload)
    first = ringing.calls.resource_for("call-one", "deadline:phone_policy")
    assert first is not None
    runtime.transport_config["default_ringing_timeout"] = 0
    apply_phone_policy_state(hass, ringing.payload)
    assert ringing.calls.resource_for("call-one", "deadline:phone_policy") is first


async def test_first_form_prefills_stored_configuration_and_keeps_explicit_zero(hass):
    from custom_components.voip_stack.config_flow import VoipStackConfigFlow
    flow = VoipStackConfigFlow()
    flow.hass = hass
    flow._current_entry_data = lambda: (None, {})
    initial = await flow.async_step_user()
    assert initial["data_schema"]({})["default_ringing_timeout"] == 120
    assert initial["data_schema"]({})["max_dialog_subscription_expires"] == 60
    flow._current_entry_data = lambda: (None, {"default_ringing_timeout": 0})
    reconfigured = await flow.async_step_user()
    assert reconfigured["data_schema"]({})["default_ringing_timeout"] == 0


@pytest.mark.parametrize("existing", [{}, {"default_ringing_timeout": 0}, {"default_ringing_timeout": 47}])
async def test_migration_75_stores_prefill_once_without_overwriting_values(hass, existing):
    from custom_components.voip_stack import async_migrate_entry
    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=4,
        data={"initial_phone_created": True, "unrelated": "preserved", **existing})
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    assert entry.minor_version == 7
    assert entry.data["max_dialog_subscription_expires"] == 60
    assert entry.data["default_ringing_timeout"] == existing.get("default_ringing_timeout", 120)
    before = deepcopy(entry.as_dict())
    assert await async_migrate_entry(hass, entry)
    assert entry.as_dict() == before


async def test_75_does_not_run_past_real_73_identity_conflict(hass):
    from custom_components.voip_stack import async_migrate_entry
    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=3,
        data={"initial_phone_created": True})
    entry.add_to_hass(hass)
    row = ConfigSubentry(subentry_type="phone", title="Home", unique_id="phone:default", data={
        "kind": "sip_account", "username": "Home", "name": "Home", "username_source": "user",
        "password": "fixture-only", "extension": "home", "ha_bindings": [
            {"source": "migration", "reference": "legacy_ha_phone:default"},
            {"source": "migration", "reference": "legacy_ha_phone:browser:home"}],
    })
    hass.config_entries.async_add_subentry(entry, row)
    devices = dr.async_get(hass)
    one = devices.async_get_or_create(config_entry_id=entry.entry_id, config_subentry_id=row.subentry_id,
        identifiers={("voip_stack", "phone_endpoint:default")}, name="Home")
    other = devices.async_get_or_create(config_entry_id=entry.entry_id, config_subentry_id=row.subentry_id,
        identifiers={("voip_stack", "phone_endpoint:browser:home")}, name="Previous Home")
    before = deepcopy(entry.as_dict())
    assert await async_migrate_entry(hass, entry) is False
    assert entry.as_dict() == before
    assert entry.minor_version == 3 and "default_ringing_timeout" not in entry.data
    devices.async_remove_device(other.id)
    await hass.async_block_till_done()
    assert await async_migrate_entry(hass, entry)
    assert entry.minor_version == 7 and entry.data["default_ringing_timeout"] == 120
    assert devices.async_get(one.id) is not None
    assert row.subentry_id in entry.subentries


async def test_global_update_does_not_request_transport_reload(hass, ringing, monkeypatch):
    from custom_components.voip_stack.config_entry_runtime import entry_runtime_signature, async_config_entry_updated
    from custom_components.voip_stack.store import config_entry
    entry = config_entry(hass)
    runtime = runtime_data(hass)
    runtime.entry_runtime_signature = entry_runtime_signature(entry)
    reload = AsyncMock()
    monkeypatch.setattr(hass.config_entries, "async_reload", reload)
    hass.config_entries.async_update_entry(entry, data={**entry.data, "default_ringing_timeout": 0, "max_dialog_subscription_expires": 600})
    await async_config_entry_updated(hass, entry)
    assert not reload.called
    assert runtime.transport_config["default_ringing_timeout"] == 0
    assert runtime.transport_config["max_dialog_subscription_expires"] == 600


def test_rule_inheritance_preserves_explicit_values_and_unlimited():
    from custom_components.voip_stack.dial_plan import RingPolicy
    from custom_components.voip_stack.duration_config import initialize_duration_config
    from custom_components.voip_stack.group_config import normalize_group_data
    from custom_components.voip_stack.parking_config import normalize_parking_config
    from custom_components.voip_stack.queue_policy import QueuePolicy
    from custom_components.voip_stack.queue_config import initial_queue_policy
    from custom_components.voip_stack.ringing_policy import cap_ringing_timeout, resolve_ringing_timeout

    group = normalize_group_data({'kind': 'ring', 'name': 'Support'})
    assert group['policy']['overall_timeout'] is None
    assert RingPolicy.from_metadata(group['policy'], default_timeout=120).overall_timeout == 120
    assert RingPolicy.from_metadata(group['policy'], default_timeout=1).step_timeout == 1
    explicit = normalize_group_data({'kind': 'ring', 'name': 'Support', 'policy': {'overall_timeout': 30}})
    assert RingPolicy.from_metadata(explicit['policy'], default_timeout=120).overall_timeout == 30
    unlimited = RingPolicy.from_metadata(explicit['policy'], default_timeout=120, timeout_override=0)
    assert unlimited.overall_timeout == float('inf')
    short = RingPolicy.from_metadata(explicit['policy'], timeout_override=0.5)
    assert short.overall_timeout == short.step_timeout == 0.5
    assert QueuePolicy.from_data(initial_queue_policy({})).ring_timeout is None
    assert QueuePolicy.from_data(initial_queue_policy({'ring_timeout': 20})).ring_timeout == 20
    assert QueuePolicy.from_data(initial_queue_policy({'ring_timeout': 0})).ring_timeout == 0
    assert normalize_parking_config(initialize_duration_config({})['parking'])['return_timeout'] is None
    assert normalize_parking_config(initialize_duration_config({'parking': {'return_timeout': 30}})['parking'])['return_timeout'] == 30
    assert normalize_parking_config(initialize_duration_config({'parking': {'return_timeout': 0}})['parking'])['return_timeout'] == 0
    assert cap_ringing_timeout(resolve_ringing_timeout({'default_ringing_timeout': 120}, override=0), 7) == 7
    assert resolve_ringing_timeout({}) is None


@pytest.mark.parametrize('saved,account_value,expected', [(0, None, None), (300, None, 300),
                                                        (None, None, 120), (None, 23, 23)])
async def test_parking_return_override_preserves_zero_and_owner_deadline(hass, ringing, saved, account_value, expected):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from custom_components.voip_stack.parking import ParkingCall
    runtime = runtime_data(hass)
    runtime.transport_config['default_ringing_timeout'] = 120
    runtime.endpoints.update('kitchen', policy=PhonePolicy(ringing_timeout=account_value))
    ringing.calls.forward_call = AsyncMock(return_value=None)
    application = SimpleNamespace(hass=hass, session=ringing.session,
        parked=SimpleNamespace(return_destination='kitchen'), settings={'return_timeout': saved},
        stop_music=AsyncMock())
    claim = Mock()
    before = hass.loop.time()
    await ParkingCall._return(application, claim)
    claim.commit.assert_called_once()
    assert ringing.calls.forward_call.call_args.kwargs['ringing_timeout'] == (expected if expected is not None else 0)
    timer = ringing.calls.resource_for(ringing.session.call_id, 'parking_return')
    if expected is None:
        assert timer is None
    else:
        assert timer is not None and before + expected <= timer.handle.when() <= hass.loop.time() + expected
