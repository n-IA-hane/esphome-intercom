"""Compare existing trunk target construction for the same dialed contact."""

import pytest
from homeassistant.core import Context

from custom_components.voip_stack.core.sip import parse_sip_uri
from custom_components.voip_stack.endpoint_dialing import EndpointDialer, OutboundLegPolicy
from custom_components.voip_stack.endpoint_routing import EndpointRouteResolver
from custom_components.voip_stack.outbound_attempts import async_close_outbound_leg
from custom_components.voip_stack.sip_runtime import enable_reused_tcp_connection, uri_transport
from .test_trunk_calls_ha import trunks as trunks, lab as lab, dial

pytestmark = pytest.mark.ha


async def test_same_named_trunk_constructs_same_called_domain_for_group_and_automation(trunks):
    lab = trunks
    # Registration and next hop remain the real local UDP peer; this is the
    # separately configured logical SIP domain used by production builders.
    lab.runtime.trunk_configs["home"] = {
        **lab.runtime.trunk_configs["home"], "trunk_domain": "voice.example.invalid"}
    context = Context()
    await dial(lab, context, destination="External", trunk_id="home")
    automation_target = parse_sip_uri(lab.phones[0].invites[-1].uri)
    await lab.hass.services.async_call("voip_stack", "hangup", {}, blocking=True, context=context)
    dialer = EndpointDialer(hass=lab.hass, local_ip="127.0.0.1", config=lab.runtime.transport_config,
        route_resolver=EndpointRouteResolver(lab.hass, "127.0.0.1", lab.runtime.transport_config["sip_port"], trunk_id="home"),
        sip_uri_transport=uri_transport, enable_reused_tcp_connection=enable_reused_tcp_connection)
    leg = dialer.prepare_outbound_leg(member="External", peers=[], roster_entries=lab.roster,
        local_name="Test", local_rtp_port_index=0,
        policy=OutboundLegPolicy(trunk_id="home", allow_video=False))
    assert leg is not None
    try:
        print({"automation_wire_request_uri": str(automation_target), "group_candidate_uri": str(leg.uri),
            "configured_server": lab.runtime.trunk_configs["home"]["trunk_server"],
            "configured_domain": lab.runtime.trunk_configs["home"]["trunk_domain"]})
        assert automation_target.host == leg.uri.host, "Caller type changed the called SIP domain"
    finally:
        await async_close_outbound_leg(leg)
