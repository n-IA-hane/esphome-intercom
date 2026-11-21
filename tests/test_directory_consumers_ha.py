"""Routing consumers preserve the public directory resolution contract."""

import pytest

from custom_components.voip_stack import endpoint_routing, pbx_routing
from custom_components.voip_stack.peer import Peer
from custom_components.voip_stack.roster import RosterEntry, RosterError

pytestmark = pytest.mark.ha


def test_peer_and_roster_consumers_select_exact_public_spelling():
    peers = [Peer(name="Desk one", host="192.0.2.1", endpoint_id="first"),
             Peer(name="Desk.one", host="192.0.2.2", endpoint_id="second")]
    rows = [RosterEntry("desk_one", name="Desk one"), RosterEntry("desk_two", name="Desk.one")]
    assert endpoint_routing.peer_for_target("Desk.one", peers) is peers[1]
    assert pbx_routing.roster_entry_for_target("Desk.one", rows) is rows[1]
    with pytest.raises(RosterError, match="Ambiguous"):
        endpoint_routing.peer_for_target("Desk%20one", peers)
    with pytest.raises(RosterError, match="Ambiguous"):
        pbx_routing.roster_entry_for_target("Desk%20one", rows)


def test_route_name_uses_the_shared_compatibility_normalization():
    assert endpoint_routing.same_route_name("Front%20door", "Front door")


def test_logical_dial_target_never_uses_an_internal_endpoint_reference(monkeypatch):
    from custom_components.voip_stack.endpoint_registry import EndpointRegistry
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint

    registry = EndpointRegistry()
    endpoint = registry.register(PhoneEndpoint("a52832282eca4f82a000cfacaca56d0c", "Desk", "browser"))
    rows = [RosterEntry("desk-user", name="Desk", metadata={"endpoint_id": endpoint.endpoint_id})]
    monkeypatch.setattr(endpoint_routing, "endpoint_directory", lambda hass: registry)
    monkeypatch.setattr(endpoint_routing, "is_ha_target", lambda hass, target: False)
    assert endpoint_routing.logical_endpoint_for_member(None, "Desk", [], rows) is endpoint
    assert endpoint_routing.logical_endpoint_for_member(None, endpoint.endpoint_id, [], rows) is None
