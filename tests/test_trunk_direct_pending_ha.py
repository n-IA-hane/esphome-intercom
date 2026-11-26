"""Caller cancellation owns the pending legacy direct-URI trunk attempt."""

import asyncio
import socket
import struct
import re

import pytest

from custom_components.voip_stack.config import normalized_trunk_config
from custom_components.voip_stack.core import rtp, sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.roster import RosterEntry, dump_roster_json
from custom_components.voip_stack.sip_client import SipCallClient
from custom_components.voip_stack.sip_trunk import SipTrunkClient
from custom_components.voip_stack.sip_udp_io import SipDatagramQueueProtocol
from custom_components.voip_stack.trunk_runtime import trunk_client_config
from .test_trunk_calls_ha import trunks as trunks, lab as lab
from .test_browser_logical_availability_ha import resources

pytestmark = pytest.mark.ha


async def _register_target(lab, target, monkeypatch, username="bedroom0"):
    from custom_components.voip_stack.sip_registrar import SipAccount
    from .test_sip_capabilities_ha import register_peer
    target.messages = asyncio.Queue()
    receive = target.datagram_received
    def receive_registration(raw, address):
        message = sip.parse_message(raw)
        if message.is_response:
            target.messages.put_nowait((message, address))
        else:
            receive(raw, address)
    monkeypatch.setattr(target, "datagram_received", receive_registration)
    registrar = lab.calls.component("registrar")
    registrar.enabled = True
    registrar.update_accounts([*registrar.accounts.values(), SipAccount(username, username, "fixture-only")])
    await register_peer(lab, target, username)


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize(("target_kind", "end"), [
    (kind, end) for kind in ("contact", "account")
    for end in ("cancel", "timeout", "answer", "busy", "decline", "failure")
] + [("mobility", "answer")])
async def test_direct_uri_trunk_cancel_closes_pending_peer_and_can_ring_again(trunks, monkeypatch, end, target_kind):
    lab = trunks
    target = lab.phones[0]
    target.auto_answer = end in {"answer", "busy", "decline", "failure"}
    target.final_status = {"busy": 486, "decline": 603, "failure": 503}.get(end, 200)
    port = target.transport.get_extra_info('sockname')[1]
    from custom_components.voip_stack.contact_config import replace_contacts
    from custom_components.voip_stack.store import config_entry
    replace_contacts(lab.hass, config_entry(lab.hass), [{"id": "External contact", "name": "External contact",
        "sip_uri": f"sip:external-contact@127.0.0.1:{port}", "ha_bridge": True}])
    lab.roster.append(RosterEntry('External contact', name='External contact',
        sip_uri=f'sip:external-contact@127.0.0.1:{port}', ha_bridge=True,
        metadata={'endpoint_kind': 'contact'}))
    lab.runtime.directory.replace(lab.roster)
    lab.hass.states.async_set('sensor.voip_phonebook', 'ready', {'roster_json': dump_roster_json(lab.roster)})
    destination = 'External contact'
    if target_kind == "account":
        await _register_target(lab, target, monkeypatch)
        destination = "bedroom0"
    elif target_kind == "mobility":
        from homeassistant.config_entries import ConfigSubentry
        from custom_components.voip_stack.contact_config import contact_dicts
        from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
        from custom_components.voip_stack.phone_policy import PhonePolicy
        from custom_components.voip_stack.sip_registrar import SipAccount

        destination = "Unregistered office line"
        policy = {"unavailable": {"kind": "contact", "target": "External"}}
        entry = config_entry(lab.hass)
        replace_contacts(lab.hass, entry, [*contact_dicts(entry), {
            "id": "External", "name": "External", "number": "12345",
        }])
        lab.hass.config_entries.async_add_subentry(entry, ConfigSubentry(
            subentry_type="phone", title=destination, unique_id="phone:away",
            data={"endpoint_id": "away", "name": destination, "username": "away",
                  "kind": "sip_account", "call_policy": policy},
        ))
        lab.runtime.endpoints.register(PhoneEndpoint(
            "away", destination, EndpointKind.SIP_ACCOUNT, username="away",
            policy=PhonePolicy.from_data(policy),
        ))
        registrar = lab.calls.component("registrar")
        registrar.enabled = True
        registrar.update_accounts([
            *registrar.accounts.values(), SipAccount("away", destination, "fixture-only"),
        ])
        assert not registrar.registered_contacts("away")
        from custom_components.voip_stack.directory_runtime import compose_directory_entries
        automation = [row for row in lab.roster if row.metadata.get("virtual_endpoint") == "automation"]
        lab.roster[:] = [*compose_directory_entries(lab.hass, [], registrar.registered_roster_entries()), *automation]
        lab.runtime.directory.replace(lab.roster)
    listener = ('127.0.0.1', lab.runtime.transport_config['sip_port'])
    config = normalized_trunk_config({"trunk_options_interval": 0, 'trunk_enabled': True, 'trunk_register': False,
        'trunk_server': '127.0.0.42', 'trunk_allowed_ips': ['127.0.0.42/32'],
        'trunk_inbound_mode': 'dtmf', 'trunk_dtmf_enabled': True,
        'trunk_inbound_default_target': destination, 'trunk_dtmf_timeout_ms': 100})
    provider = SipTrunkClient(config=trunk_client_config(config), local_ip=listener[0],
        local_sip_port=listener[1], trunk_id='direct-provider')
    lab.runtime.trunk_configs['direct-provider'] = config
    lab.calls.adopt_component('trunk:direct-provider', provider, closer=provider.stop)
    clients, readers = [], []
    media = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    media.bind(("127.0.0.42", 0))
    media.setblocking(False)
    try:
        for cycle in range(2):
            target.invited.clear()
            before = len(target.requests)
            client = SipCallClient(local_ip='127.0.0.42', local_name='Provider source', local_uri_user='provider',
                local_sip_port=0, local_rtp_port=media.getsockname()[1],
                supported_formats=[AudioFormat(16000, frame_ms=20)])
            clients.append(client)
            client.transport, client.protocol = await lab.hass.loop.create_datagram_endpoint(
                lambda: SipDatagramQueueProtocol(client.queue), local_addr=('127.0.0.42', 0))
            client.local_sip_port = client.transport.get_extra_info('sockname')[1]
            assert await client.invite(target='450', remote_host=listener[0], remote_sip_port=listener[1], timeout=3) == 'ringing'
            await asyncio.wait_for(target.invited.wait(), 3)
            session = lab.calls.get_session(client.dialog_ids.call_id)
            invite = target.invites[-1]
            expected_target = {"account": "bedroom0", "contact": "external-contact", "mobility": "12345"}[target_kind]
            assert sip.parse_sip_uri(invite.uri).user == expected_target
            if target_kind == "mobility":
                assert session.metadata["policy_endpoint_ids"] == ("away",)
                assert session.metadata["outbound_trunk_id"] == "home"
                assert not registrar.registered_contacts("away")
                assert not lab.phones[1].invites
            if end == 'answer':
                assert await client.wait_for_final(timeout=3) == 'in_call'
                readers.append(asyncio.create_task(client.wait_for_dialog_termination()))
                await asyncio.sleep(0)
                while True:
                    try:
                        media.recv(4096)
                    except BlockingIOError:
                        break
                forward = struct.pack('!320h', *([2100] * 320))
                await lab.hass.loop.sock_sendto(media, rtp.build_packet(rtp.RtpPacket(
                    client.dialog.send_format.payload_type, 0, 0, 901, forward)),
                    (client.dialog.remote_rtp_host, client.dialog.remote_rtp_port))
                accumulated = bytearray()
                async with asyncio.timeout(2):
                    while len(accumulated) < len(forward):
                        packet = rtp.parse_packet((await target.audio.get())[0])
                        if any(packet.payload):
                            accumulated.extend(packet.payload)
                assert bytes(accumulated) == forward
                reverse = struct.pack('!320h', *([-3100] * 320))
                target_port = int(re.search(rb'm=audio (\d+)', invite.body)[1])
                await lab.hass.loop.sock_sendto(target.rtp_socket, rtp.build_packet(rtp.RtpPacket(
                    target.payload_type, 0, 0, 902, reverse)), ('127.0.0.1', target_port))
                accumulated.clear()
                async with asyncio.timeout(2):
                    while len(accumulated) < len(reverse):
                        packet = rtp.parse_packet(await lab.hass.loop.sock_recv(media, 4096))
                        if any(packet.payload):
                            accumulated.extend(packet.payload)
                assert bytes(accumulated) == reverse
                await client.terminate(timeout=2)
                await asyncio.wait_for(session.terminated.wait(), 3)
                assert any(request.method == 'BYE' for request in target.requests[before:])
                assert resources(lab)['call_scoped_quiescent']
                await client.close()
                continue
            if end in {'busy', 'decline', 'failure'}:
                assert await client.wait_for_final(timeout=3) not in {'in_call', 'ringing', 'timeout'}
                assert client.last_sip_status_code == target.final_status
                await asyncio.wait_for(session.terminated.wait(), 3)
                assert resources(lab)['call_scoped_quiescent']
                await client.close()
                continue
            if end == 'timeout':
                assert await client.wait_for_final(timeout=.1) == 'timeout'
            assert await client.terminate(timeout=2) == 'cancelled'
            await asyncio.wait_for(session.terminated.wait(), 3)
            async with asyncio.timeout(2):
                while not any(request.method == 'CANCEL' for request in target.requests[before:]):
                    await asyncio.sleep(.01)
            requests = target.requests[before:]
            cancel = next(request for request in requests if request.method == 'CANCEL')
            assert cancel.header('Call-ID') == invite.header('Call-ID')
            assert cancel.header('CSeq').split()[0] == invite.header('CSeq').split()[0]
            assert not any(request.method == 'BYE' for request in requests)
            assert resources(lab)['call_scoped_quiescent']
            await client.close()
    finally:
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        for client in clients:
            await client.close()
        media.close()


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("status", [486, 603, 503])
@pytest.mark.parametrize("target_kind", ["contact", "account"])
async def test_answered_ivr_contact_failure_sends_bye_and_reuses_the_next_call(
    trunks, monkeypatch, unused_udp_port_factory, tmp_path, status, target_kind,
):
    from custom_components.voip_stack.contact_config import replace_contacts
    from custom_components.voip_stack.store import config_entry
    from .test_shared_account_fork_ha import no_digits_trunk

    lab = trunks
    target = lab.phones[0]
    target.final_status = status
    port = target.transport.get_extra_info('sockname')[1]
    replace_contacts(lab.hass, config_entry(lab.hass), [{"id": "External contact", "name": "External contact",
        "sip_uri": f"sip:external-contact@127.0.0.1:{port}", "ha_bridge": True}])
    destination = "External contact"
    if target_kind == "account":
        await _register_target(lab, target, monkeypatch)
        destination = "bedroom0"
    # An Automation contact owns answering before the transfer. Automatic
    # trunk digit collection no longer answers every incoming call.
    from homeassistant.helpers.script import Script
    from homeassistant.helpers.trigger import TriggerConfig
    from custom_components import voip_stack
    from custom_components.voip_stack import automation_call, services, trigger
    await services.async_register_services(lab.hass, {"wait_for_dtmf": automation_call.async_wait_for_dtmf,
        "forward": voip_stack._handle_sip_forward_service})
    lab.roster.append(RosterEntry("Welcome", metadata={"virtual_endpoint": "automation", "automation_timeout": 30}))
    lab.runtime.directory.replace(lab.roster)
    script = Script(lab.hass, [
        {"action": "voip_stack.wait_for_dtmf", "data": {"timeout": 0.1}},
        {"action": "voip_stack.forward", "data": {"destination": destination, "on_failure": "terminate"}},
    ], "Answered IVR failure", "automation")
    tasks = []
    def runner(payload, description, context):
        task = lab.hass.async_create_task(script.async_run({"trigger": payload}, context))
        tasks.append(task)
        return task
    subject = trigger.VoipCallTrigger(lab.hass, TriggerConfig("voip_stack.call_received", options={"destination": "Welcome"}))
    detach = await subject.async_attach_runner(runner)
    async with no_digits_trunk(lab, monkeypatch, unused_udp_port_factory, tmp_path,
                               "Welcome", calls=2) as caller:
        # The independent trace proves both answered dialogs and server BYEs.
        await asyncio.wait_for(caller.wait(), 5)
        wire = (tmp_path / "trunk-sip.log").read_text()
        assert wire.count("BYE sip:sipp@127.0.0.2") >= 2
        assert len(target.invites) == 2
        assert "SIP/2.0 200 OK" in wire
        assert "SIP/2.0 486" not in wire and "SIP/2.0 603" not in wire and "SIP/2.0 503" not in wire
        async with asyncio.timeout(3):
            while lab.calls.sessions:
                await asyncio.sleep(.01)
        assert resources(lab)['call_scoped_quiescent']

    detach()
    await asyncio.gather(*tasks, return_exceptions=True)
