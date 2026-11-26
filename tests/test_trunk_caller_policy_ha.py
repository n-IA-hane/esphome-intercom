"""Caller policy changes the wire identity without changing authentication."""

import asyncio
import hashlib
import re
import socket

import pytest
from homeassistant.core import Context

from custom_components.voip_stack.config import normalized_trunk_config
from custom_components.voip_stack.core import sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
from custom_components.voip_stack.phone_policy import PhonePolicy
from custom_components.voip_stack.roster import RosterEntry
from custom_components.voip_stack.sip_client import SipCallClient
from custom_components.voip_stack.sip_udp_io import SipDatagramQueueProtocol

from .test_trunk_calls_ha import trunks as trunks, sip_test_account
from .test_automation_originate_ha import lab as lab, dial

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("mode", ["caller", "account", "trunk"])
def test_trunk_identity_settings_survive_normalization(mode):
    selected = normalized_trunk_config({
        "trunk_caller_id_policy": mode,
        "trunk_caller_id": "+3906123400",
        "trunk_caller_name": "Office",
        "trunk_inbound_account": "peer-main",
        "trunk_inbound_unknown_target": "reject",
    })
    assert selected["trunk_caller_id_policy"] == mode
    assert selected["trunk_caller_id"] == "+3906123400"
    assert selected["trunk_caller_name"] == "Office"
    assert selected["trunk_inbound_account"] == "peer-main"
    assert selected["trunk_inbound_unknown_target"] == "reject"


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("mode,public_id,trunk_id,expected", [
    ("caller", "", "", "101"),
    ("account", "", "", "101"),
    ("trunk", "", "", "work"),
    ("caller", "+39061234101", "", "+39061234101"),
    ("account", "+39061234101", "", "101"),
    ("trunk", "+39061234101", "+3906123400", "+3906123400"),
    ("caller", "+39061234101", "+3906123400", "+39061234101"),
])
async def test_registered_phone_caller_policy_is_applied_on_real_trunk_wire(trunks, monkeypatch, mode, public_id, trunk_id, expected):
    lab = trunks
    lab.runtime.trunk_configs["work"]["trunk_caller_id_policy"] = mode
    lab.runtime.trunk_configs["work"]["trunk_caller_id"] = trunk_id
    lab.runtime.trunk_configs["work"]["trunk_auth_username"] = "provider-login"
    provider = lab.phones[1]
    receive = provider.datagram_received
    authenticated = []

    def provider_receive(raw, address):
        request = sip.parse_message(raw)
        if request.method == "INVITE":
            authorization = request.header("Authorization")
            if not authorization:
                provider.transport.sendto(sip.build_uas_response(request, 401, "Unauthorized",
                    extra_headers=(("WWW-Authenticate", 'Digest realm="provider", nonce="wire-nonce", algorithm=MD5, qop="auth"'),)), address)
                return
            fields = {key: value.strip('"') for key, value in re.findall(r'(\w+)=("[^"]*"|[^, ]+)', authorization)}
            def md5(value):
                return hashlib.md5(value.encode()).hexdigest()
            expected_digest = md5(f'{md5("provider-login:provider:test-only")}:{fields["nonce"]}:{fields["nc"]}:{fields["cnonce"]}:auth:{md5("INVITE:" + request.uri)}')
            if fields.get("username") != "provider-login" or fields.get("response") != expected_digest:
                provider.transport.sendto(sip.build_uas_response(request, 403, "Forbidden"), address)
                return
            authenticated.append(request)
        receive(raw, address)

    monkeypatch.setattr(provider, "datagram_received", provider_receive)
    lab.runtime.endpoints.register(PhoneEndpoint(
        endpoint_id="101", username="101", name="Reception", kind=EndpointKind.SIP_ACCOUNT,
        availability="available", policy=PhonePolicy(outbound_trunk_id="work", outbound_caller_id=public_id),
    ))
    lab.roster.append(RosterEntry("101", address="127.0.0.2", metadata={"endpoint_id": "101"}))
    account = sip_test_account(lab, "101")
    audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    audio.bind(("127.0.0.2", 0))
    client = SipCallClient(
        local_ip="127.0.0.2", local_name="Reception", local_uri_user="101",
        local_sip_port=0, local_rtp_port=audio.getsockname()[1],
        username=account.username, password=account.password,
        supported_formats=[AudioFormat(sample_rate=16000, frame_ms=20)],
    )
    client.transport, client.protocol = await lab.hass.loop.create_datagram_endpoint(
        lambda: SipDatagramQueueProtocol(client.queue), local_addr=("127.0.0.2", 0),
    )
    client.local_sip_port = client.transport.get_extra_info("sockname")[1]
    try:
        outcome = await client.invite(
            target="External", remote_host="127.0.0.1",
            remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3,
        )
        if outcome == "ringing":
            outcome = await client.wait_for_final(timeout=3)
        assert outcome == "in_call"
        assert len(authenticated) == 1
        request = lab.phones[1].invites[0]
        assert sip.parse_sip_uri(sip.name_addr_uri(request.header("From"))).user == expected
        assert sip.parse_sip_uri(request.uri).user == "12345"
        assert sip.parse_sip_uri(sip.name_addr_uri(request.header("Contact"))).user == "work"
        await client.terminate()
        async with asyncio.timeout(3):
            while lab.calls.sessions:
                await asyncio.sleep(0.005)
    finally:
        await client.close()
        audio.close()


@pytest.mark.parametrize("mode,expected", [("caller", "+39061234101"), ("account", "101"), ("trunk", "work")])
async def test_account_card_uses_its_account_caller_policy(trunks, monkeypatch, mode, expected):
    from unittest.mock import AsyncMock
    from homeassistant.core import ServiceCall
    from custom_components.voip_stack import softphone_originate
    from custom_components.voip_stack.phone_control import CallControlRequest, PhoneOperation
    from custom_components.voip_stack.roster import dump_roster_json

    lab = trunks
    lab.runtime.trunk_configs["work"]["trunk_caller_id_policy"] = mode
    phone = PhoneEndpoint(endpoint_id="101", username="101", name="Reception",
        kind=EndpointKind.SIP_ACCOUNT, device_id="card-device", availability="available",
        policy=PhonePolicy(outbound_trunk_id="work", outbound_caller_id="+39061234101"))
    lab.runtime.endpoints.register(phone)
    lab.hass.states.async_set("sensor.voip_phonebook", "3", {"roster_json": dump_roster_json(lab.roster)})
    monkeypatch.setattr(softphone_originate, "_ha_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(softphone_originate, "_resolve_target_device", AsyncMock(return_value=None))
    call = ServiceCall(lab.hass, "voip_stack", "call", {"device_id": phone.device_id, "destination": "External"})
    try:
        await softphone_originate.async_originate_browser_call(call, endpoint_id=phone.endpoint_id, browser_endpoint=phone)
        async with asyncio.timeout(3):
            while not lab.phones[1].invites or not any(session.state == "in_call" for session in lab.calls.sessions.values()):
                await asyncio.sleep(0.005)
        request = lab.phones[1].invites[0]
        assert sip.parse_sip_uri(sip.name_addr_uri(request.header("From"))).user == expected
        assert sip.parse_sip_uri(sip.name_addr_uri(request.header("Contact"))).user == "work"
    finally:
        current = lab.runtime.endpoints.require(phone.endpoint_id)
        if current.active_call_id:
            await lab.runtime.phones.control(
                ServiceCall(lab.hass, "voip_stack", "hangup", {"device_id": phone.device_id, "call_id": current.active_call_id}),
                PhoneOperation.HANGUP, CallControlRequest(call_id=current.active_call_id),
            )


@pytest.mark.parametrize("mode,expected", [("caller", "Wakeup-Caller"), ("account", "Wakeup-Caller"), ("trunk", "home")])
async def test_automation_uses_the_same_trunk_caller_policy(trunks, mode, expected):
    lab = trunks
    lab.runtime.trunk_configs["home"]["trunk_caller_id_policy"] = mode
    context = Context()
    try:
        await dial(lab, context, destination="External", trunk_id="home")
        request = lab.phones[0].invites[0]
        assert sip.parse_sip_uri(sip.name_addr_uri(request.header("From"))).user == expected
        assert sip.parse_sip_uri(sip.name_addr_uri(request.header("Contact"))).user == "home"
    finally:
        if lab.calls.sessions:
            await lab.hass.services.async_call("voip_stack", "hangup", {}, blocking=True, context=context)
