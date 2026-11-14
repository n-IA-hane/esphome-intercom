"""Native device-entity lookup retains the existing permission boundary."""

from types import SimpleNamespace

import pytest
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .test_managed_phones_ha import custom_integration_path as custom_integration_path, phone_lab as phone_lab

pytestmark = pytest.mark.ha


async def test_disabled_account_entity_still_carries_its_device_permission(phone_lab):
    from custom_components.voip_stack.authorization import require_websocket_endpoint_control
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint

    lab = phone_lab
    device = dr.async_get(lab.hass).async_get_or_create(config_entry_id=lab.entry.entry_id,
        identifiers={("voip_stack", "phone_endpoint:sip:desk")})
    entity = er.async_get(lab.hass).async_get_or_create("switch", "voip_stack", "desk_dnd",
        config_entry=lab.entry, device_id=device.id, disabled_by=er.RegistryEntryDisabler.USER)
    endpoint = PhoneEndpoint(endpoint_id="sip:desk", kind="sip_account", username="desk",
                             name="Desk", device_id=device.id)
    permitted = SimpleNamespace(user=SimpleNamespace(id="permitted", permissions=SimpleNamespace(
        check_entity=lambda identity, policy: identity == entity.entity_id and policy == "control")))
    denied = SimpleNamespace(user=SimpleNamespace(id="denied", permissions=SimpleNamespace(
        check_entity=lambda identity, policy: False)))
    assert require_websocket_endpoint_control(lab.hass, permitted, endpoint) == "permitted"
    with pytest.raises(Unauthorized):
        require_websocket_endpoint_control(lab.hass, denied, endpoint)
