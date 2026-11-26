"""Authenticated PBX peers retain caller identity through the production listener."""

import asyncio
from dataclasses import replace
import hashlib
import re
import socket

import pytest

from custom_components.voip_stack.config import normalized_trunk_config
from custom_components.voip_stack.core import sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.sip_client import SipCallClient
from custom_components.voip_stack.sip_registrar import SipAccount

from .test_automation_originate_ha import lab as lab


pytestmark = pytest.mark.ha
PASSWORD = "peer-fixture-only"


def bind_peer(lab, *, enabled=True):
    registrar = lab.calls.component("registrar")
    registrar.enabled = True
    registrar.update_accounts([*registrar.accounts.values(), SipAccount("remote_pbx", "Remote PBX", PASSWORD)])
    # This fixture configures the runtime consumer directly. Form persistence
    # and validation are covered by the native trunk-flow tests.
    lab.runtime.trunk_configs["remote"] = {
        **normalized_trunk_config({"trunk_enabled": enabled, "trunk_server": "127.0.0.7",
                                   "trunk_inbound_default_target": "bedroom1"}),
        "trunk_inbound_account": "remote_pbx",
        "trunk_inbound_unknown_target": "reject",
    }


def peer_client(transport, *, caller="101", username="remote_pbx", password=PASSWORD, host="127.0.0.7"):
    return SipCallClient(
        local_ip=host, local_name="Remote extension " + caller, local_uri_user=caller,
        local_sip_port=0, local_rtp_port=36540,
        supported_formats=[AudioFormat(16000, frame_ms=20)], signaling_transport=transport,
        username=username, password=password,
    )


@pytest.mark.parametrize("transport", ["UDP", "TCP"])
async def test_authenticated_peer_calls_local_phone_without_replacing_caller(lab, transport):
    bind_peer(lab)
    caller = peer_client(transport)
    try:
        state = await caller.invite(target="bedroom0", remote_host="127.0.0.1",
            remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3)
        assert state in {"ringing", "in_call"}, caller.last_sip_status_code
        if state == "ringing":
            assert await caller.wait_for_final(timeout=3) == "in_call"
        session = lab.calls.get_session(caller.dialog_ids.call_id)
        assert session is not None and session.metadata["inbound_trunk_id"] == "remote"
        assert not session.metadata.get("registration_only_caller")
        assert not session.metadata.get("source_endpoint_id")
        received = lab.phones[0].invites[-1]
        assert sip.parse_sip_uri(sip.name_addr_uri(received.header("From"))).user == "101"
        await caller.terminate()
        async with asyncio.timeout(3):
            while lab.calls.sessions:
                await asyncio.sleep(.01)
        assert not lab.runtime.rtp_port_pool.get("used")
    finally:
        await caller.close()


@pytest.mark.parametrize("transport", ["UDP", "TCP"])
async def test_peer_unknown_extension_rejects_without_using_default_destination(lab, transport):
    bind_peer(lab)
    caller = peer_client(transport)
    try:
        await caller.invite(target="299", remote_host="127.0.0.1",
            remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3)
        assert caller.last_sip_status_code == 404
        assert not any(phone.invites for phone in lab.phones)
        assert not lab.calls.sessions and not lab.runtime.rtp_port_pool.get("used")
    finally:
        await caller.close()


@pytest.mark.parametrize("transport", ["UDP", "TCP"])
async def test_ordinary_account_still_cannot_present_another_from_identity(lab, transport):
    from .test_automation_originate_ha import PHONE_PASSWORD

    caller = peer_client(transport, username="bedroom0", password=PHONE_PASSWORD)
    try:
        await caller.invite(target="bedroom1", remote_host="127.0.0.1",
            remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3)
        assert caller.last_sip_status_code == 403
        assert not any(phone.invites for phone in lab.phones)
        assert not lab.calls.sessions
    finally:
        await caller.close()


@pytest.mark.parametrize("transport", ["UDP", "TCP"])
@pytest.mark.parametrize("valid_password", [False, True])
async def test_bound_static_peer_requires_digest_without_an_ip_allowlist(lab, transport, valid_password):
    from custom_components.voip_stack.sip_trunk import SipTrunkClient
    from custom_components.voip_stack.trunk_runtime import trunk_client_config

    bind_peer(lab)
    config = lab.runtime.trunk_configs["remote"]
    config.update(trunk_register=False, trunk_transport=transport.lower(), trunk_allowed_ips=[])
    provider = SipTrunkClient(config=trunk_client_config(config), local_ip="127.0.0.1",
        local_sip_port=lab.runtime.transport_config["sip_port"], trunk_id="remote")
    lab.calls.adopt_component("trunk:remote", provider, closer=provider.stop)
    assert provider.ready and not provider.accepts_inbound_source("127.0.0.1", 6000, transport)
    caller = peer_client(transport, password=PASSWORD if valid_password else "wrong-fixture-secret")
    try:
        state = await caller.invite(target="bedroom0", remote_host="127.0.0.1",
            remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3)
        if valid_password:
            assert state in {"ringing", "in_call"}
            if state == "ringing":
                assert await caller.wait_for_final(timeout=3) == "in_call"
            registrar = lab.calls.component("registrar")
            assert any(principal == "remote_pbx" for _nonce, principal, _cnonce in registrar.nonce_uses)
            await caller.terminate()
        else:
            assert caller.last_sip_status_code == 401
            assert not any(phone.invites for phone in lab.phones)
        async with asyncio.timeout(3):
            while lab.calls.sessions:
                await asyncio.sleep(.01)
    finally:
        await caller.close()


@pytest.mark.parametrize("invalid", ["disabled", "duplicate"])
async def test_invalid_peer_binding_never_falls_back_to_an_ordinary_account(lab, invalid):
    bind_peer(lab, enabled=invalid != "disabled")
    if invalid == "duplicate":
        lab.runtime.trunk_configs["another"] = dict(lab.runtime.trunk_configs["remote"])
    caller = peer_client("UDP", caller="remote_pbx")
    try:
        await caller.invite(target="bedroom0", remote_host="127.0.0.1",
            remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3)
        assert caller.last_sip_status_code == 403
        assert not any(phone.invites for phone in lab.phones)
        assert not lab.calls.sessions
    finally:
        await caller.close()


async def test_disabled_replacement_does_not_hide_the_enabled_peer_binding(lab):
    bind_peer(lab)
    lab.runtime.trunk_configs["replacement"] = {
        **lab.runtime.trunk_configs["remote"], "trunk_enabled": False,
    }
    caller = peer_client("UDP")
    try:
        state = await caller.invite(target="bedroom0", remote_host="127.0.0.1",
            remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3)
        if state == "ringing":
            state = await caller.wait_for_final(timeout=3)
        assert state == "in_call"
        assert lab.calls.get_session(caller.dialog_ids.call_id).metadata["inbound_trunk_id"] == "remote"
        await caller.terminate()
    finally:
        await caller.close()


@pytest.mark.parametrize("answer", [False, True])
async def test_peer_unknown_target_keeps_native_route_window_and_explicit_timeout(lab, answer):
    from homeassistant.helpers.trigger import TriggerConfig
    from custom_components import voip_stack
    from custom_components.voip_stack import services
    from custom_components.voip_stack.trigger import VoipCallTrigger

    bind_peer(lab)
    await services.async_register_services(lab.hass, {
        "select_inbound_destination": voip_stack._handle_select_inbound_destination_service,
    })
    snapshots, tasks = [], []

    async def choose(context):
        if answer:
            await lab.hass.services.async_call("voip_stack", "select_inbound_destination",
                {"destination": "bedroom0"}, blocking=True, context=context)

    def run(payload, _description, context):
        snapshots.append(payload["call"])
        task = lab.hass.async_create_task(choose(context))
        tasks.append(task)
        return task

    native = VoipCallTrigger(lab.hass, TriggerConfig("voip_stack.route_requested",
        options={"trunk": "remote", "destination": "299"}))
    detach = await native.async_attach_runner(run)
    caller = peer_client("UDP")
    try:
        state = await caller.invite(target="299", remote_host="127.0.0.1",
            remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3)
        await asyncio.gather(*tasks)
        assert len(snapshots) == 1
        assert snapshots[0]["caller_number"] == "101"
        assert snapshots[0]["called_number"] == "299"
        assert snapshots[0]["inbound_trunk_id"] == "remote"
        if answer:
            assert state in {"ringing", "in_call"}, caller.last_sip_status_code
            if state == "ringing":
                assert await caller.wait_for_final(timeout=3) == "in_call"
            assert len(lab.phones[0].invites) == 1 and not lab.phones[1].invites
            await caller.terminate()
        else:
            assert caller.last_sip_status_code == 504
            assert not any(phone.invites for phone in lab.phones)
        async with asyncio.timeout(3):
            while lab.calls.sessions:
                await asyncio.sleep(.01)
        assert not lab.runtime.rtp_port_pool.get("used")
    finally:
        detach()
        await caller.close()


async def test_legacy_unknown_target_policy_keeps_configured_fallback(lab):
    bind_peer(lab)
    del lab.runtime.trunk_configs["remote"]["trunk_inbound_unknown_target"]
    caller = peer_client("UDP")
    try:
        state = await caller.invite(target="299", remote_host="127.0.0.1",
            remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3)
        assert state in {"ringing", "in_call"}
        if state == "ringing":
            assert await caller.wait_for_final(timeout=3) == "in_call"
        assert not lab.phones[0].invites and len(lab.phones[1].invites) == 1
        await caller.terminate()
    finally:
        await caller.close()


@pytest.mark.parametrize("bound,local_digest", [(True, False), (False, True), (False, False)])
def test_owned_trunk_flow_keeps_explicit_account_admission(lab, bound, local_digest):
    from custom_components.voip_stack.sip_ingress import SipIngressPolicy
    from custom_components.voip_stack.sip_trunk import SipTrunkClient
    from custom_components.voip_stack.trunk_runtime import trunk_client_config
    from .test_invite_router_behavior import _invite

    bind_peer(lab)
    config = lab.runtime.trunk_configs["remote"]
    if not bound:
        del config["trunk_inbound_account"]
    config.update(trunk_register=False, trunk_allowed_ips=["127.0.0.1/32"])
    provider = SipTrunkClient(config=trunk_client_config(config), local_ip="127.0.0.1",
        local_sip_port=lab.runtime.transport_config["sip_port"], trunk_id="remote")
    lab.calls.adopt_component("trunk:remote", provider, closer=provider.stop)
    invite = replace(_invite(), identified_source_endpoint_id="", received_via_trunk=True,
                     trunk_id="remote", source_host="127.0.0.1")
    headers = [
        ("Via", "SIP/2.0/TCP 127.0.0.1:5060;branch=z9hG4bK-owned-peer"),
        ("From", "<sip:101@peer.example>;tag=peer"), ("To", "<sip:202@home.example>"),
        ("Call-ID", "owned-peer"), ("CSeq", "1 INVITE"),
    ]
    if local_digest:
        headers.append(("Authorization", 'Digest username="bedroom0", realm="voip_stack", nonce="invalid"'))
    request = sip.parse_message(sip.build_request("INVITE", str(invite.request_uri), headers))
    policy = SipIngressPolicy(lab.hass, lab.runtime.transport_config, "127.0.0.1",
                              lab.calls.component("registrar"))
    result = policy.authorize_initial_invite(request, invite, ("127.0.0.1", 5060), "TCP")
    if bound or local_digest:
        assert result.status == 401 and dict(result.headers)["WWW-Authenticate"]
    else:
        assert result.received_via_trunk and result.trunk_id == "remote"


@pytest.mark.parametrize("transport", ["UDP", "TCP"])
async def test_independent_wire_peer_with_invalid_digest_cannot_present_a_caller(lab, transport):
    from custom_components.voip_stack.sip_tcp_io import read_sip_stream_message

    bind_peer(lab)
    listener = ("127.0.0.1", lab.runtime.transport_config["sip_port"])
    stream = datagram = None
    if transport == "TCP":
        reader, stream = await asyncio.open_connection(*listener)
        port = stream.get_extra_info("sockname")[1]
        async def send(raw):
            stream.write(raw)
            await stream.drain()
        async def receive():
            return sip.parse_message(await read_sip_stream_message(reader))
    else:
        datagram = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        datagram.bind(("127.0.0.1", 0))
        datagram.setblocking(False)
        port = datagram.getsockname()[1]
        async def send(raw):
            await lab.hass.loop.sock_sendto(datagram, raw, listener)
        async def receive():
            return sip.parse_message(await lab.hass.loop.sock_recv(datagram, 65535))
    uri = f"sip:bedroom0@{listener[0]}:{listener[1]}"
    body = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Independent peer\r\n"
            "c=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio 36544 RTP/AVP 96\r\n"
            "a=rtpmap:96 L16/16000/1\r\na=ptime:20\r\na=sendrecv\r\n").encode()
    headers = [("Via", f"SIP/2.0/{transport} 127.0.0.1:{port};branch=z9hG4bK-independent-peer"),
        ("From", '"Remote 101" <sip:101@peer.example>;tag=wire-peer'),
        ("To", f"<{uri}>"), ("Call-ID", "independent-peer"), ("CSeq", "1 INVITE"),
        ("Contact", f"<sip:peer@127.0.0.1:{port}>"), ("Content-Type", "application/sdp")]
    try:
        await send(sip.build_request("INVITE", uri, headers, body))
        async with asyncio.timeout(3):
            challenge = await receive()
            while challenge.status_code < 200:
                challenge = await receive()
        assert challenge.status_code == 401
        fields = dict(re.findall(r'(\w+)="([^"]*)"', challenge.header("WWW-Authenticate")))
        nonce, realm = fields["nonce"], fields["realm"]
        md5 = lambda value: hashlib.md5(value.encode()).hexdigest()
        digest = md5(f'{md5("remote_pbx:" + realm + ":incorrect-secret")}:{nonce}:00000001:wire:auth:{md5("INVITE:" + uri)}')
        auth = (f'Digest username="remote_pbx", realm="{realm}", nonce="{nonce}", uri="{uri}", '
                f'response="{digest}", algorithm=MD5, qop=auth, nc=00000001, cnonce="wire"')
        retry = [(key, "2 INVITE" if key == "CSeq" else value + "-retry" if key == "Via" else value)
                 for key, value in headers]
        await send(sip.build_request("INVITE", uri, [*retry, ("Authorization", auth)], body))
        async with asyncio.timeout(3):
            rejected = await receive()
            while rejected.status_code < 200:
                rejected = await receive()
        assert rejected.status_code == 401
        assert not any(phone.invites for phone in lab.phones)
        assert not lab.calls.sessions and not lab.runtime.rtp_port_pool.get("used")
    finally:
        if stream is not None:
            stream.close()
            await stream.wait_closed()
        if datagram is not None:
            datagram.close()
