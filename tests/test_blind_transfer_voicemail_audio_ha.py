"""Blind REFER adopts a mailbox or phone and retains the opposite wire leg."""

import asyncio
import math
import re
import socket
import struct
import wave
from types import SimpleNamespace

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import callback

from custom_components.voip_stack.core import rtp, sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.core.sip_transfer import SipReferTarget
from custom_components.voip_stack.mailbox import EVENT_VOICEMAIL_RECEIVED, get_archive
from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
from custom_components.voip_stack.phone_policy import PhonePolicy
from custom_components.voip_stack.sip_client import SipCallClient, SipDatagramQueueProtocol
from custom_components.voip_stack.sip_registrar import SipAccount
from custom_components.voip_stack.store import config_entry
from custom_components.voip_stack.websocket_api import CALL_EVENT

from .test_browser_logical_availability_ha import resources
from .test_native_blind_transfer_handoff_ha import until
from .test_trunk_calls_ha import lab as lab, trunks as trunks
from .test_udp_outbound_flow_ha import PASSWORD, register_phone
from .test_voicemail_ha import configured_tts_provider as configured_tts_provider

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("transferor", ["source", "destination"])
@pytest.mark.parametrize("destination", ["voicemail", "phone"])
async def test_accepted_refer_immediate_bye_keeps_opposite_audio_and_application(
    trunks, monkeypatch, tmp_path, transferor, destination,
):
    lab, registry = trunks, trunks.calls
    registrar = registry.component("registrar")
    registrar.enabled = True
    account = SipAccount("transfer-source", "Transfer source", PASSWORD)
    registrar.update_accounts([*registrar.accounts.values(), account])
    lab.runtime.endpoints.register(PhoneEndpoint(account.username, account.display_name,
        EndpointKind.SIP_ACCOUNT, username=account.username))
    lab.hass.config.media_dirs = {"local": str(tmp_path / "media")}
    entry = config_entry(lab.hass)
    mailbox_id = "sip:bedroom1"
    if destination == "voicemail":
        lab.hass.config_entries.async_add_subentry(entry, ConfigSubentry(
            subentry_type="phone", title="Bedroom mailbox", unique_id="phone:bedroom1",
            data={"name": "bedroom1", "endpoint_id": mailbox_id, "username": "bedroom1",
                  "kind": "sip_account", "voicemail": {"enabled": True, "max_seconds": 5}},
        ))
        lab.runtime.endpoints.update(mailbox_id, policy=PhonePolicy.from_data({
            "always": {"kind": "voicemail", "target": mailbox_id},
        }))
    recording, accepted = asyncio.Event(), asyncio.Event()
    saved = asyncio.Queue()

    @callback
    def state(event):
        if event.data.get("event_type") == "voicemail_recording":
            recording.set()

    @callback
    def received(event):
        saved.put_nowait(event.data)

    remove_state = lab.hass.bus.async_listen(CALL_EVENT, state)
    remove_received = lab.hass.bus.async_listen(EVENT_VOICEMAIL_RECEIVED, received)
    archive = await get_archive(lab.hass)
    media = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    media.bind(("127.0.0.2", 0))
    media.setblocking(False)
    caller = SipCallClient(local_ip="127.0.0.2", local_name=account.username,
        local_uri_user=account.username, local_sip_port=0, local_rtp_port=media.getsockname()[1],
        supported_formats=[AudioFormat(16000, frame_ms=20)],
        username=account.username, password=account.password)
    transfer = None
    delegated = []
    phone, target = lab.phones
    for peer in lab.phones:
        receive = peer.datagram_received

        def dispatch(raw, address, peer=peer, receive=receive):
            message = sip.parse_message(raw)
            if message.method == "REFER":
                delegated.append(message)
                headers = [(name, message.header(name)) for name in ("Via", "From", "To", "Call-ID", "CSeq")]
                peer.transport.sendto(sip.build_response(405, "Method Not Allowed", headers), address)
                return
            receive(raw, address)
            if message.method == "NOTIFY":
                peer.respond(message, address, 200)
            if peer is phone and message.status_code == 200 and message.header("CSeq") == "42 REFER":
                accepted.set()
                peer.remote_hangup(cseq=43)

        monkeypatch.setattr(peer, "datagram_received", dispatch)
    try:
        caller.transport, caller.protocol = await lab.hass.loop.create_datagram_endpoint(
            lambda: SipDatagramQueueProtocol(caller.queue), local_addr=("127.0.0.2", 0))
        caller.local_sip_port = caller.transport.get_extra_info("sockname")[1]

        async def registration_response(_method=None, *, status=None):
            async with asyncio.timeout(2):
                while True:
                    raw, address = await caller.queue.get()
                    message = sip.parse_message(raw)
                    if message.method == "OPTIONS":
                        caller._send_response_to_request(message, *address, 200, "OK")
                    elif message.is_response and (status is None or message.status_code == status):
                        return message, address

        registering = SimpleNamespace(address=("127.0.0.2", caller.local_sip_port),
            transport=caller.transport, receive=registration_response)
        listener = ("127.0.0.1", lab.runtime.transport_config["sip_port"])
        await register_phone(SimpleNamespace(a=registering, registrar=registrar, lab=lab, listener=listener),
            username=account.username, outbound=False, discover=False)
        result = await caller.invite(target="bedroom0", remote_host=listener[0],
            remote_sip_port=listener[1], timeout=3)
        if result == "ringing":
            result = await caller.wait_for_final(timeout=3)
        assert result == "in_call"
        await until(lambda: bool(registry.sessions))
        original = registry.get_session(caller.dialog_ids.call_id)
        old_destination = registry.bridge_link_for(original.call_id)
        original_relay = registry.resource_for(original.call_id, "relay")
        assert original_relay is not None
        # These targets and PTs come from each peer's actual accepted SIP SDP,
        # independently of the registry's later ownership or side reversal.
        if transferor == "source":
            retained_socket = phone.rtp_socket
            retained_target = ("127.0.0.1", int(re.search(rb"m=audio (\d+)", phone.last_request.body)[1]))
            retained_pt = phone.payload_type
        else:
            retained_socket = media
            retained_target = (caller.dialog.remote_rtp_host, caller.dialog.remote_rtp_port)
            retained_pt = caller.dialog.send_format.payload_type
        refer_to = f"sip:bedroom1@{listener[0]}:{listener[1]}"
        if transferor == "source":
            send_request = caller._send_in_dialog_request

            async def send(method, **kwargs):
                response = await send_request(method, **kwargs)
                if method == "REFER" and response is not None and response.status_code == 200:
                    accepted.set()
                return response

            monkeypatch.setattr(caller, "_send_in_dialog_request", send)
            transfer = asyncio.create_task(caller.refer(SipReferTarget(refer_to)))
            await asyncio.wait_for(accepted.wait(), 3)
            await caller.terminate(timeout=2)
        else:
            invite = phone.last_request
            port = phone.transport.get_extra_info("sockname")[1]
            phone.transport.sendto(sip.build_request("REFER", str(sip.parse_sip_uri(invite.header("Contact"))), [
                ("Via", f"SIP/2.0/UDP 127.0.0.1:{port};branch=z9hG4bK-mailbox-transfer"),
                ("From", invite.header("To") + ";tag=bedroom"), ("To", invite.header("From")),
                ("Call-ID", invite.header("Call-ID")), ("CSeq", "42 REFER"),
                ("Refer-To", f"<{refer_to}>"), ("Contact", f"<sip:bedroom0@127.0.0.1:{port}>")]), phone.remote)
            await asyncio.wait_for(accepted.wait(), 3)
        if destination == "voicemail":
            await asyncio.wait_for(recording.wait(), 4)
            current = next(session for session in registry.sessions.values() if session.live and session.owner == "voicemail")
            assert not target.invites
        else:
            await asyncio.wait_for(target.answered.wait(), 4)
            await until(lambda: any(session.live and session.state == "in_call" and
                registry.bridge_link_for(session.call_id) not in {"", old_destination} for session in registry.sessions.values()))
            current = next(session for session in registry.sessions.values() if session.live and
                registry.bridge_link_for(session.call_id) not in {"", old_destination})
            assert registry.resource_for(current.call_id, "relay") is original_relay
        if transfer is not None:
            assert (await asyncio.wait_for(transfer, 4)).accepted
        assert (current.call_id != original.call_id) is (transferor == "source")
        assert not delegated, "An internal destination must not depend on provider REFER support"
        samples = [round(8000 * math.sin(2 * math.pi * 450 * n / 16000)) for n in range(320)]
        network_pcm, local_pcm = struct.pack(">320h", *samples), struct.pack("<320h", *samples)
        for n in range(24):
            packet = rtp.RtpPacket(retained_pt, 100 + n, 32000 + 320 * n, 0x61626364, network_pcm)
            await lab.hass.loop.sock_sendto(retained_socket, rtp.build_packet(packet), retained_target)
            await asyncio.sleep(.02)
        if destination == "phone":
            async with asyncio.timeout(3):
                while True:
                    raw, _ = await target.audio.get()
                    if rtp.parse_packet(raw).payload == network_pcm:
                        break
            # The control call must retain the reverse wire path too.
            reverse_target = ("127.0.0.1", int(re.search(rb"m=audio (\d+)", target.last_request.body)[1]))
            for n in range(4):
                packet = rtp.RtpPacket(target.payload_type, 200 + n, 64000 + 320 * n, 0x71727374, network_pcm)
                await lab.hass.loop.sock_sendto(target.rtp_socket, rtp.build_packet(packet), reverse_target)
                await asyncio.sleep(.02)
            async with asyncio.timeout(3):
                while True:
                    raw = ((await phone.audio.get())[0] if transferor == "source"
                           else await lab.hass.loop.sock_recv(media, 2048))
                    if rtp.parse_packet(raw).payload == network_pcm:
                        break
            target.remote_hangup(cseq=7)
        elif transferor == "source":
            phone.remote_hangup(cseq=7)
        else:
            await caller.terminate(timeout=2)
        if destination == "voicemail":
            deposited = await asyncio.wait_for(saved.get(), 4)
            assert deposited["mailbox_id"] == mailbox_id
            messages = archive.list(mailbox_id)
            assert len(messages) == 1 and messages[0]["id"] == deposited["message_id"]
            with wave.open(str(archive.segment_path(deposited["message_id"], mailbox_id)), "rb") as audio:
                assert (audio.getframerate(), audio.getsampwidth(), audio.getnchannels()) == (16000, 2, 1)
                assert local_pcm * 8 in audio.readframes(audio.getnframes())
        await until(lambda: not registry.sessions)
        assert resources(lab)["call_scoped_quiescent"]
        assert not registry.endpoint_claims_snapshot() and not lab.runtime.rtp_port_pool.get("used")
    finally:
        remove_state()
        remove_received()
        if transfer is not None:
            if not transfer.done():
                transfer.cancel()
            await asyncio.gather(transfer, return_exceptions=True)
        await caller.close()
        media.close()
