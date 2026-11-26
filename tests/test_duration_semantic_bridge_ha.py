"""Local 7.6 retirement and reference duration states converge at 7.7."""
from copy import deepcopy
import math

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant.common import MockConfigEntry

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("minor", [6, 7])
async def test_partial_duration_state_converges_without_losing_choices(hass, minor):
    from custom_components.voip_stack import async_migrate_entry
    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=minor,
        data={"service_timeouts": {"media_timeout": 91, "trunk_first_digit_timeout": 10, "future_choice": "kept"},
              "parking": {"timeout": 217},
              "default_trunk_id": "kept-trunk", "unrelated": {"literal": "keep"}},
        options={"unrelated": "keep"})
    entry.add_to_hass(hass)
    row = ConfigSubentry(subentry_type="contact", title="Explicit automation", unique_id="contact:kept",
        data={"id": "Kept", "name": "Kept", "metadata": {
            "virtual_endpoint": "automation", "automation_timeout": 47, "custom": "keep"}})
    hass.config_entries.async_add_subentry(entry, row)
    assert await async_migrate_entry(hass, entry)
    assert (entry.version, entry.minor_version) == (7, 7)
    from custom_components.voip_stack.duration_policy import SERVICE_TIMEOUT_LIMITS, configured_service_timeout
    assert set(SERVICE_TIMEOUT_LIMITS) <= set(entry.data["service_timeouts"])
    assert configured_service_timeout(entry.data, "contact_step_timeout") > 0
    assert entry.data["service_timeouts"]["media_timeout"] == 91
    assert "trunk_first_digit_timeout" not in entry.data["service_timeouts"]
    assert entry.data["service_timeouts"]["future_choice"] == "kept"
    assert entry.data["parking"]["timeout"] == 217
    assert entry.data["default_trunk_id"] == "kept-trunk"
    assert entry.options == {"unrelated": "keep"}
    current = entry.subentries[row.subentry_id]
    assert current.unique_id == row.unique_id and current.data == row.data
    snapshot = deepcopy(entry.as_dict())
    assert await async_migrate_entry(hass, entry)
    assert entry.as_dict() == snapshot


@pytest.mark.parametrize("bad", [0, True, math.nan, math.inf, 10**1000])
async def test_invalid_duration_blocks_before_initialization_writes(hass, bad):
    from custom_components.voip_stack import async_migrate_entry
    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=6,
                            data={"service_timeouts": {"media_timeout": bad}})
    entry.add_to_hass(hass)
    before = deepcopy(dict(entry.data))
    assert not await async_migrate_entry(hass, entry)
    assert entry.minor_version == 6
    assert entry.data.keys() == before.keys()
    assert entry.data["service_timeouts"]["media_timeout"] is bad or entry.data == before
    issue = ir.async_get(hass).async_get_issue("voip_stack", "duration_configuration_invalid_" + entry.entry_id)
    assert issue and issue.is_fixable
