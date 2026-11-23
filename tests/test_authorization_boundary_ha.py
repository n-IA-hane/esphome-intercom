"""Native HA permission policies protect account and associated hardware controls."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.auth.models import Group, User
from homeassistant.auth.permissions.models import PermissionLookup
from homeassistant.core import Context, ServiceCall
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from .test_managed_phones_ha import custom_integration_path as custom_integration_path

pytestmark = pytest.mark.ha


def native_user(hass, grants):
    return User(name='Scoped phone user', is_active=True,
        perm_lookup=PermissionLookup(er.async_get(hass), dr.async_get(hass)),
        groups=[Group(name='Phone policy', policy={'entities': {'entity_ids': grants}})])


async def test_logical_phone_permissions_are_scoped_per_endpoint(hass, monkeypatch):
    from custom_components.voip_stack import authorization
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint
    entities = er.async_get(hass)
    own = entities.async_get_or_create('sensor', 'voip_stack', 'scope_kitchen')
    other = entities.async_get_or_create('sensor', 'voip_stack', 'scope_hall')
    shared = entities.async_get_or_create('event', 'voip_stack', 'voip_stack_call_event')
    user = native_user(hass, {shared.entity_id: {'control': True}, own.entity_id: {'read': True, 'control': True}})
    monkeypatch.setattr(hass.auth, 'async_get_user', AsyncMock(return_value=user))
    kitchen = PhoneEndpoint('kitchen', 'Kitchen', 'sip_account', username='kitchen', entity_ids={own.entity_id})
    hall = PhoneEndpoint('hall', 'Hall', 'sip_account', username='hall', entity_ids={other.entity_id})
    call = ServiceCall(hass, 'voip_stack', 'call', {}, context=Context(user_id=user.id))
    await authorization.async_require_service_endpoint_control(hass, call, kitchen)
    with pytest.raises(Unauthorized):
        await authorization.async_require_service_endpoint_control(hass, call, hall)
    connection = SimpleNamespace(user=user)
    assert authorization.require_websocket_endpoint_read(hass, connection, kitchen) == user.id
    with pytest.raises(Unauthorized):
        authorization.require_websocket_endpoint_read(hass, connection, hall)


async def test_read_only_websocket_cannot_advertise_phone_presence(hass):
    from custom_components.voip_stack import authorization
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint
    state = er.async_get(hass).async_get_or_create('sensor', 'voip_stack', 'scope_presence')
    endpoint = PhoneEndpoint('kitchen', 'Kitchen', 'sip_account', username='kitchen', entity_ids={state.entity_id})
    reader = native_user(hass, {state.entity_id: {'read': True}})
    controller = native_user(hass, {state.entity_id: {'read': True, 'control': True}})
    assert not authorization.websocket_can_control_endpoint(hass, SimpleNamespace(user=reader), endpoint)
    assert authorization.websocket_can_control_endpoint(hass, SimpleNamespace(user=controller), endpoint)


async def test_esp_phone_permission_does_not_expand_to_unrelated_device_entities(hass):
    from custom_components.voip_stack import authorization
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint
    native = MockConfigEntry(domain='esphome')
    native.add_to_hass(hass)
    account = MockConfigEntry(domain='voip_stack')
    account.add_to_hass(hass)
    devices = dr.async_get(hass)
    hardware = devices.async_get_or_create(config_entry_id=native.entry_id, identifiers={('esphome', 'permission-phone')})
    entities = er.async_get(hass)
    state = entities.async_get_or_create('sensor', 'esphome', 'permission-phone-voip_state', config_entry=native, device_id=hardware.id)
    backlight = entities.async_get_or_create('switch', 'esphome', 'permission-phone-backlight', config_entry=native, device_id=hardware.id)
    endpoint = PhoneEndpoint('phone', 'Phone', 'esphome', username='phone', device_id=hardware.id,
                             entity_ids={state.entity_id})
    unrelated = native_user(hass, {backlight.entity_id: {'control': True}})
    allowed = native_user(hass, {state.entity_id: {'control': True}})
    with pytest.raises(Unauthorized):
        authorization.require_websocket_endpoint_control(hass, SimpleNamespace(user=unrelated), endpoint)
    assert authorization.require_websocket_endpoint_control(hass, SimpleNamespace(user=allowed), endpoint) == allowed.id
