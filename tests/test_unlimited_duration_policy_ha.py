"""Zero limits stay persisted and only disable their approved runtime deadlines."""

from copy import deepcopy

import pytest
import voluptuous as vol

from custom_components.voip_stack.duration_config import initialize_duration_config
from custom_components.voip_stack.duration_policy import validate_duration
from custom_components.voip_stack.mailbox_config import initial_mailbox_settings
from custom_components.voip_stack.parking_config import normalize_parking_config
from custom_components.voip_stack.queue_config import initial_queue_policy
from custom_components.voip_stack.services import async_register_services

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("value", [0, 0.25, 180, 3600])
def test_approved_limits_preserve_zero_and_fractional_values(value):
    assert validate_duration(value, "recording_timeout") == value
    assert normalize_parking_config({"timeout": value})["timeout"] == value
    assert initial_queue_policy({"max_wait": value})["max_wait"] == value
    assert initial_mailbox_settings({"enabled": True, "max_seconds": value})["max_seconds"] == value
    stored = {"service_timeouts": {"recording_timeout": value}, "parking": {"timeout": value}}
    before = deepcopy(stored)
    restored = initialize_duration_config(stored)
    assert restored["service_timeouts"]["recording_timeout"] == value
    assert restored["parking"]["timeout"] == value
    assert stored == before
    assert initialize_duration_config(restored) == restored


@pytest.mark.parametrize("value", [True, False, -1, float("nan"), float("inf"), 10**1000])
@pytest.mark.parametrize("owner", ["recording", "parking", "queue", "mailbox"])
def test_unlimited_policy_keeps_invalid_values_rejected(owner, value):
    with pytest.raises(ValueError):
        if owner == "recording":
            validate_duration(value, "recording_timeout")
        elif owner == "parking":
            normalize_parking_config({"timeout": value})
        elif owner == "queue":
            initial_queue_policy({"max_wait": value})
        else:
            initial_mailbox_settings({"max_seconds": value})


@pytest.mark.parametrize("key", ["automation_timeout", "media_timeout", "ivr_digit_timeout",
    "ivr_interdigit_timeout", "ivr_prompt_timeout", "voicemail_menu_setup_timeout", "voicemail_greeting_timeout"])
def test_other_action_deadlines_remain_positive(key):
    with pytest.raises(ValueError):
        validate_duration(0, key)


@pytest.mark.parametrize("service,identity,field", [
    ("start_recording", "device_id", "max_seconds"),
    ("set_voicemail_settings", "device_id", "max_seconds"),
    ("configure_queue", "queue", "max_wait"),
])
async def test_public_service_limits_preserve_zero_and_fractional_values(hass, service, identity, field):
    await async_register_services(hass, {})
    schema = hass.services.async_services()["voip_stack"][service].schema
    for value in (0, 0.25, 31):
        assert schema({identity: "phone", field: value})[field] == value
    for value in (True, -1, float("nan"), float("inf"), 10**1000):
        with pytest.raises(vol.Invalid):
            schema({identity: "phone", field: value})
