"""Configured SIP addresses retain phonebook policy and normal controller access."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import Context, ServiceCall
from homeassistant.exceptions import Unauthorized

from custom_components.voip_stack.roster import RosterEntry
from custom_components.voip_stack.router import resolve_ha_router, RouteAction, RouteReason
from .test_managed_phones_ha import (
    custom_integration_path as custom_integration_path,
    phone_lab as phone_lab,
)

pytestmark = pytest.mark.ha


def saved_contact():
    return RosterEntry('saved-door', name='Door', sip_uri='sip:door@192.0.2.10',
                       metadata={'sip_profile': 'dahua'})


def test_exact_saved_uri_keeps_the_route_and_policy_of_the_saved_name():
    contact = saved_contact()
    baseline = resolve_ha_router(contact.name, [contact])
    selected = resolve_ha_router(contact.sip_uri, [contact])
    assert baseline.action is RouteAction.FORWARD
    assert selected == baseline
    assert selected.entry is contact
    assert selected.reason is not RouteReason.DIRECT_URI
    assert selected.entry.metadata['sip_profile'] == 'dahua'
    generated = RosterEntry('alice', name='Kitchen', address='192.0.2.20', port=5070,
                            metadata={'endpoint_kind': 'sip_account', 'sip_transport': 'tcp'})
    assert resolve_ha_router('sip:alice@192.0.2.20:5070;transport=tcp', [generated]) == resolve_ha_router('alice', [generated])


@pytest.mark.parametrize('target', [
    'sip:door@192.0.2.11', 'sip:another@192.0.2.10',
    'sip:door@192.0.2.10:5070', 'sip:door@192.0.2.10;transport=tcp',
    'sip:door@192.0.2.10?Subject=altered', 'sips:door@192.0.2.10',
])
def test_unconfigured_or_modified_uri_remains_an_ad_hoc_destination(target):
    route = resolve_ha_router(target, [saved_contact()])
    assert route.action is RouteAction.DIRECT
    assert route.reason is RouteReason.DIRECT_URI
    assert route.entry is None
    assert route.sip_uri == target


def test_saved_uri_enforces_disabled_and_ambiguous_contact_policy():
    contact = saved_contact()
    disabled = resolve_ha_router(contact.sip_uri, [replace(contact, enabled=False)])
    assert disabled.status == 403
    assert disabled.reason is RouteReason.TARGET_DISABLED
    other = replace(contact, id='another-door', name='Another door', enabled=False,
                    metadata={'sip_profile': 'different-policy'})
    for rows in ([contact, other], [other, contact]):
        ambiguous = resolve_ha_router(contact.sip_uri, rows)
        assert ambiguous.status == 485
        assert ambiguous.reason is RouteReason.AMBIGUOUS_DESTINATION


@pytest.mark.parametrize('target,known', [
    ('Door', True), ('sip:door@192.0.2.10', True),
    ('sip:door@192.0.2.11', False), ('sip:door@192.0.2.10;transport=tcp', False),
])
@pytest.mark.parametrize('admin', [False, True])
async def test_real_browser_dispatch_keeps_known_contact_access_and_ad_hoc_admin_gate(
    phone_lab, monkeypatch, target, known, admin,
):
    from custom_components.voip_stack import softphone_originate
    from custom_components.voip_stack.trunk_selection import TrunkSelection

    lab = phone_lab
    contact = saved_contact()
    route = resolve_ha_router(target, [contact])
    monkeypatch.setattr(lab.hass.auth, 'async_get_user', AsyncMock(return_value=SimpleNamespace(
        id='controller', is_admin=admin)))
    monkeypatch.setattr(softphone_originate, '_ha_advertise_host', AsyncMock(return_value='127.0.0.1'))
    call = ServiceCall(lab.hass, 'voip_stack', 'call', {'destination': target},
                       context=Context(user_id='controller'))
    resolved = softphone_originate._BrowserCallResolution(
        target=target, dialed_target=target, local_name='Browser', source_device_id='source',
        config={'sip_port': 5060}, contacts=[contact], destination_device=None,
        trunk_selection=TrunkSelection('', {}, None), trunk_ready=False, route=route,
        browser_destination=None, routing_call_id=None, policy_endpoint_ids=(),
    )
    dispatch = softphone_originate._async_dispatch_browser_call_route(
        call, resolved, endpoint_id='caller', browser_endpoint=None, force_ha_bridge=False)
    if not known and not admin:
        with pytest.raises(Unauthorized):
            await dispatch
    else:
        destination = await dispatch
        assert destination.route_uri == (contact.sip_uri if known else target)
        assert destination.route.entry is (contact if known else None)
    assert not lab.runtime.sip.sessions

