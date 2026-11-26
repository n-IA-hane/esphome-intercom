"""Local account redirects retain the PBX session and normal Contact admission."""

import asyncio
import re
import socket
import struct

import pytest
from aiohttp import web, WSMsgType
from homeassistant.core import Context

from custom_components.voip_stack.core import rtp, sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.roster import RosterEntry
from custom_components.voip_stack.sip_client import SipCallClient
from custom_components.voip_stack.sip_registrar import SipAccount
from custom_components.voip_stack.sip_udp_io import SipDatagramQueueProtocol
from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind

from .test_trunk_calls_ha import lab as lab, trunks as trunks
from .test_automation_originate_ha import assert_call_resources_released

pytestmark = pytest.mark.ha


def test_redirect_contact_q_precedes_large_registration_tiers():
    from custom_components.voip_stack.local_redirect import redirect_tier_ranks

    ranks = redirect_tier_ranks([(0, 1001), (1, 0), (0, 0), (0, 1001)])
    assert ranks == {(0, 0): 0, (0, 1001): 1, (1, 0): 2}


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("receiver", ["sip", "ha-only", "mixed"])
@pytest.mark.parametrize("progress", [False, True], ids=["immediate", "after-ringing"])
@pytest.mark.parametrize("group", [False, True], ids=["account", "group-branch"])
async def test_local_302_follows_canonical_account_with_same_source_and_two_way_pcm(trunks, monkeypatch, group, receiver, progress, aiohttp_client, hass_admin_user):
    lab = trunks
    first, second = lab.phones
    listener_port = lab.runtime.transport_config["sip_port"]
    bindings, ws, http = None, None, None
    if receiver != "sip":
        from custom_components.voip_stack.ha_bindings import HaCardBindings
        from custom_components.voip_stack.account_runtime import recompute_account_availability
        from custom_components.voip_stack.directory_runtime import compose_directory_entries
        entry = lab.hass.config_entries.async_entries("voip_stack")[0]
        subentry = next(row for row in entry.subentries.values() if row.data.get("username") == "bedroom1")
        lab.hass.config_entries.async_update_subentry(entry, subentry, data={**subentry.data,
            "ha_bindings": [{"source": "migration", "reference": "legacy_ha_phone:redirect"}]})
        bindings = lab.runtime.ha_bindings = HaCardBindings(lab.hass, entry, lab.runtime, lambda _: None)
        await bindings.async_start()
        from homeassistant.helpers import entity_registry as er
        entity = er.async_get(lab.hass).async_get_or_create("event", "voip_stack",
            "redirect-bedroom1-call", config_entry=entry, suggested_object_id="redirect_bedroom1_call")
        lab.runtime.endpoints.update("bedroom1", entity_ids=(entity.entity_id,))
        lab.runtime.softphone_presence["bedroom1"] = 1
        second.auto_answer = False
        if receiver == "ha-only":
            registrar = lab.calls.component("registrar")
            for registration in registrar.registered_contacts("bedroom1"):
                assert registrar.remove_contact(registration.flow_key)
        recompute_account_availability(lab.hass)
        lab.roster[:] = compose_directory_entries(lab.hass, [], lab.calls.component("registrar").registered_roster_entries())
        lab.runtime.directory.replace(lab.roster)
    _redirect_phone(monkeypatch, first, [f"<sip:bedroom1@127.0.0.1:{listener_port}>"], defer=progress)
    if group:
        lab.roster.append(RosterEntry("Redirect group", metadata={"group_type": "ring",
            "members": ["bedroom0"], "ring_timeout": 5, "step_timeout": .1}))
        lab.runtime.directory.replace(lab.roster)
    lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id="local-origin", username="local-origin",
        name="Source", kind=EndpointKind.SIP_ACCOUNT, availability="available"))
    lab.roster.append(RosterEntry("local-origin", address="127.0.0.2", metadata={"endpoint_id": "local-origin"}))
    registrar = lab.calls.component("registrar")
    account = SipAccount("local-origin", "Source", "fixture-source-password")
    registrar.update_accounts([*registrar.accounts.values(), account])
    audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    audio.bind(("127.0.0.2", 0))
    audio.setblocking(False)
    caller = SipCallClient(local_ip="127.0.0.2", local_name="Source", local_uri_user=account.username,
        username=account.username, password=account.password, local_sip_port=0,
        local_rtp_port=audio.getsockname()[1], supported_formats=[AudioFormat(16000, frame_ms=20)])
    caller.transport, caller.protocol = await lab.hass.loop.create_datagram_endpoint(
        lambda: SipDatagramQueueProtocol(caller.queue), local_addr=("127.0.0.2", 0))
    caller.local_sip_port = caller.transport.get_extra_info("sockname")[1]
    try:
        result = await caller.invite(target="Redirect group" if group else "bedroom0",
            remote_host="127.0.0.1", remote_sip_port=listener_port, timeout=3)
        if progress:
            assert result == "ringing"
            first.redirect_response()
        if receiver != "sip":
            from custom_components.voip_stack import audio_ws_view
            from custom_components.voip_stack.websocket_api import _ha_softphone_state
            async with asyncio.timeout(3):
                while _ha_softphone_state(lab.hass, "bedroom1").get("state") != "ringing":
                    await asyncio.sleep(.005)
            await lab.hass.services.async_call("voip_stack", "answer", {
                "device_id": lab.runtime.endpoints.require("bedroom1").device_id,
                "call_id": caller.dialog_ids.call_id, "media_client_id": "redirect-card-identity"}, blocking=True,
                context=Context(user_id=hass_admin_user.id))
        if result == "ringing":
            result = await caller.wait_for_final(timeout=3)
        assert result == "in_call"
        source_id = caller.dialog_ids.call_id
        session = lab.calls.get_session(source_id)
        assert session is not None and session.live
        assert not lab.runtime.endpoints.require("bedroom0").active_call_id
        assert len(first.invites) == 1
        assert len(second.invites) == (0 if receiver == "ha-only" else 1)
        assert any(request.method == "ACK" for request in first.requests)
        dialog = caller.dialog
        if receiver != "sip":
            from custom_components.voip_stack import audio_ws
            @web.middleware
            async def authenticated(request, handler):
                request["hass_user"] = hass_admin_user
                return await handler(request)
            app = web.Application(middlewares=[authenticated])
            app["hass"] = lab.hass
            app.router.add_get("/api/voip_stack/ws", audio_ws_view.VoipAudioWebSocketView().get)
            http = await aiohttp_client(app)
            ws = await http.ws_connect("/api/voip_stack/ws?endpoint_id=bedroom1&call_id=" + source_id + "&client_id=redirect-card-identity")
            async with asyncio.timeout(3):
                while True:
                    message = await ws.receive()
                    assert message.type == WSMsgType.TEXT, message
                    contract = message.json()
                    if "tx_format" in contract:
                        break
            await ws.send_json({"type": "audio_ready"})
            async with asyncio.timeout(3):
                while (source_id, "bedroom1") not in lab.runtime.media.sessions_for("audio"):
                    await asyncio.sleep(.005)
            async with asyncio.timeout(3):
                for sequence in range(3):
                    packet = rtp.RtpPacket(dialog.send_format.payload_type, sequence, sequence * 320,
                        900, struct.pack("!320h", *([2100] * 320)))
                    await lab.hass.loop.sock_sendto(audio, rtp.build_packet(packet),
                        (dialog.remote_rtp_host, dialog.remote_rtp_port))
                while True:
                    message = await ws.receive()
                    if message.type == WSMsgType.BINARY:
                        pcm = audio_ws.decode_audio_frame(message.data)
                        if any(pcm):
                            samples = struct.unpack(f"<{len(pcm)//2}h", pcm)
                            assert max(abs(value - 2100) for value in samples[64:-64]) <= 2
                            break
                tx_rate = int(contract["tx_format"].split(":")[0])
                tx_samples = round(tx_rate * float(contract["tx_format"].split(":")[3]) / 1000)
                for _ in range(8):
                    pcm = struct.pack(f"<{tx_samples}h", *([3100] * tx_samples))
                    await ws.send_bytes(audio_ws.encode_audio_frame(pcm))
                while True:
                    returned = rtp.parse_packet(await lab.hass.loop.sock_recv(audio, 4096))
                    samples = struct.unpack(f"!{len(returned.payload)//2}h", returned.payload)
                    if max(samples) >= 3000:
                        assert max(abs(value - 3100) for value in samples[64:-64]) <= 2
                        break
            assert not second.answered.is_set()
        else:
            await _sip_pcm(lab, caller, second, audio)
        assert caller.bye()
        await asyncio.wait_for(session.termination_started.wait(), 3)
        barrier = lab.calls.request_termination(source_id, session.termination_intent)
        if barrier is not None:
            await barrier
        assert not lab.calls.sessions
        if ws is not None:
            async with asyncio.timeout(3):
                while (source_id, "bedroom1") in lab.runtime.media.sessions_for("audio"):
                    await asyncio.sleep(.005)
        assert_call_resources_released(lab)
    finally:
        await caller.close()
        if ws is not None:
            await ws.close()
        if http is not None:
            await http.close()
        if bindings is not None:
            await bindings.async_close()
            lab.runtime.ha_bindings = None
        audio.close()


async def _sip_pcm(lab, caller, second, audio):
    dialog = caller.dialog
    async with asyncio.timeout(3):
        for sequence in range(3):
            packet = rtp.RtpPacket(dialog.send_format.payload_type, sequence, sequence * 320,
                900, struct.pack("!320h", *([2100] * 320)))
            await lab.hass.loop.sock_sendto(audio, rtp.build_packet(packet),
                (dialog.remote_rtp_host, dialog.remote_rtp_port))
        received = rtp.parse_packet((await second.audio.get())[0])
        assert set(struct.unpack("!320h", received.payload)) == {2100}
        peer_port = int(re.search(rb"m=audio (\d+)", second.invites[0].body)[1])
        packet = rtp.RtpPacket(second.payload_type, 0, 0, 901, struct.pack("!320h", *([3100] * 320)))
        await lab.hass.loop.sock_sendto(second.rtp_socket, rtp.build_packet(packet), ("127.0.0.1", peer_port))
        returned = rtp.parse_packet(await lab.hass.loop.sock_recv(audio, 4096))
        assert set(struct.unpack("!320h", returned.payload)) == {3100}

async def _source(lab):
    """Open an independent authenticated SIP account outside the trunk ACL."""
    account = SipAccount("local-origin", "Source", "fixture-source-password")
    lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id=account.username, username=account.username,
        name="Source", kind=EndpointKind.SIP_ACCOUNT, availability="available"))
    lab.roster.append(RosterEntry(account.username, address="127.0.0.2", metadata={"endpoint_id": account.username}))
    registrar = lab.calls.component("registrar")
    registrar.update_accounts([*registrar.accounts.values(), account])
    audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    audio.bind(("127.0.0.2", 0))
    audio.setblocking(False)
    caller = SipCallClient(local_ip="127.0.0.2", local_name="Source", local_uri_user=account.username,
        username=account.username, password=account.password, local_sip_port=0,
        local_rtp_port=audio.getsockname()[1], supported_formats=[AudioFormat(16000, frame_ms=20)])
    caller.transport, caller.protocol = await lab.hass.loop.create_datagram_endpoint(
        lambda: SipDatagramQueueProtocol(caller.queue), local_addr=("127.0.0.2", 0))
    caller.local_sip_port = caller.transport.get_extra_info("sockname")[1]
    return caller, audio


def _redirect_phone(monkeypatch, phone, contacts, *, defer=False):
    original = phone.datagram_received

    def receive(raw, address):
        message = sip.parse_message(raw)
        if message.method != "INVITE":
            original(raw, address)
            return
        phone.requests.append(message)
        phone.invites.append(message)
        headers = [(key, message.header(key)) for key in ("Via", "From", "Call-ID", "CSeq")]
        headers.append(("To", message.header("To") + ";tag=redirect"))
        headers.extend(("Contact", contact) for contact in contacts)
        response = sip.build_response(302, "Moved Temporarily", headers)
        if defer:
            phone.respond(message, address, 180)
            phone.redirect_response = lambda: phone.transport.sendto(response, address)
        else:
            phone.transport.sendto(response, address)
        phone.invited.set()

    monkeypatch.setattr(phone, "datagram_received", receive)


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("failure,status", [("loop", 482), ("hop-limit", 483),
    ("unknown", 404), ("disabled", 480), ("dnd", 486),
    ("trunk", 480), ("invalid-q", 400)])
async def test_local_redirect_rejects_loop_limit_foreign_and_policy_without_new_call(trunks, monkeypatch, failure, status):
    lab = trunks
    port = lab.runtime.transport_config["sip_port"]
    target = "bedroom0" if failure == "loop" else "bedroom1"
    host = "192.0.2.55" if failure == "foreign" else "127.0.0.1"
    if failure == "unknown":
        target = "not-a-local-account"
    contact = f"<sip:{target}@{host}:{port}>" + (";q=2" if failure == "invalid-q" else "")
    _redirect_phone(monkeypatch, lab.phones[0], [contact])
    if failure == "hop-limit":
        lab.runtime.transport_config["local_redirect_max_hops"] = 1
        _redirect_phone(monkeypatch, lab.phones[1], [f"<sip:bedroom0@127.0.0.1:{port}>"])
    if failure == "disabled":
        lab.runtime.endpoints.update("bedroom1", availability="unavailable")
    if failure == "dnd":
        lab.runtime.endpoints.update("bedroom1", dnd=True)
    caller, audio = await _source(lab)
    try:
        result = await caller.invite(target="External" if failure == "trunk" else "bedroom0",
            remote_host="127.0.0.1", remote_sip_port=port, timeout=3)
        if result == "ringing":
            result = await caller.wait_for_final(timeout=3)
        assert result != "in_call"
        assert caller.last_sip_status_code == status
        assert len(lab.phones[0].invites) == 1
        assert len(lab.phones[1].invites) == (1 if failure == "hop-limit" else 0)
        async with asyncio.timeout(3):
            while lab.calls.sessions:
                await asyncio.sleep(.005)
        assert any(request.method == "ACK" for request in lab.phones[0].requests)
        assert_call_resources_released(lab)
        assert_call_resources_released(lab)
    finally:
        await caller.close()
        audio.close()


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_rejected_redirect_leaves_other_original_contacts_concurrent(trunks, monkeypatch):
    lab = trunks
    port = lab.runtime.transport_config["sip_port"]
    lab.roster.append(RosterEntry("Concurrent", metadata={"group_type": "ring",
        "members": ["bedroom0", "bedroom1"], "ring_timeout": 5, "step_timeout": .1}))
    lab.runtime.directory.replace(lab.roster)
    _redirect_phone(monkeypatch, lab.phones[0], [f"<sip:bedroom0@127.0.0.1:{port}>"])
    second = lab.phones[1]
    second.auto_answer = False
    caller, audio = await _source(lab)
    try:
        result = await caller.invite(target="Concurrent", remote_host="127.0.0.1", remote_sip_port=port, timeout=3)
        assert result == "ringing"
        await asyncio.wait_for(second.invited.wait(), 3)
        assert not any(request.method == "CANCEL" for request in second.requests)
        second.answer()
        assert await caller.wait_for_final(timeout=3) == "in_call"
        await _sip_pcm(lab, caller, second, audio)
        assert caller.bye()
        async with asyncio.timeout(3):
            while lab.calls.sessions:
                await asyncio.sleep(.005)
        assert_call_resources_released(lab)
    finally:
        await caller.close()
        audio.close()


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_redirect_follows_external_uri_through_normal_dialplan(trunks, monkeypatch):
    lab = trunks
    peer_port = lab.phones[1].transport.get_extra_info("sockname")[1]
    _redirect_phone(monkeypatch, lab.phones[0], [f"<sip:external-peer@127.0.0.1:{peer_port}>"])
    caller, audio = await _source(lab)
    try:
        result = await caller.invite(target="bedroom0", remote_host="127.0.0.1",
            remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3)
        if result == "ringing":
            result = await caller.wait_for_final(timeout=3)
        assert result == "in_call"
        assert sip.parse_sip_uri(lab.phones[1].invites[0].uri).user == "external-peer"
        await _sip_pcm(lab, caller, lab.phones[1], audio)
        assert caller.bye()
        async with asyncio.timeout(3):
            while lab.calls.sessions:
                await asyncio.sleep(.005)
        assert_call_resources_released(lab)
    finally:
        await caller.close()
        audio.close()


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_redirect_preserves_original_caller_external_permission(trunks, monkeypatch):
    lab = trunks
    port = lab.runtime.transport_config["sip_port"]
    _redirect_phone(monkeypatch, lab.phones[0], [f"<sip:12345@127.0.0.1:{port}>"], defer=True)
    caller, audio = await _source(lab)
    try:
        result = await caller.invite(target="bedroom0", remote_host="127.0.0.1", remote_sip_port=port, timeout=3)
        assert result == "ringing"
        lab.runtime.transport_config["external_call_blocked_endpoints"] = ["local-origin"]
        lab.phones[0].redirect_response()
        result = await caller.wait_for_final(timeout=3)
        assert caller.last_sip_status_code == 403
        assert len(lab.phones[0].invites) == 1 and not lab.phones[1].invites
        async with asyncio.timeout(3):
            while lab.calls.sessions:
                await asyncio.sleep(.005)
        assert_call_resources_released(lab)
    finally:
        await caller.close()
        audio.close()


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_cancel_redirect_cleans_late_200_with_ack_bye_without_recreating_call(trunks, monkeypatch):
    lab = trunks
    port = lab.runtime.transport_config["sip_port"]
    _redirect_phone(monkeypatch, lab.phones[0], [f"<sip:bedroom1@127.0.0.1:{port}>"])
    second = lab.phones[1]
    second.auto_answer = False
    second.answer_on_cancel = True
    caller, audio = await _source(lab)
    try:
        result = await caller.invite(target="bedroom0", remote_host="127.0.0.1", remote_sip_port=port, timeout=3)
        assert result == "ringing"
        await asyncio.wait_for(second.invited.wait(), 3)
        source_id = caller.dialog_ids.call_id
        session = lab.calls.get_session(source_id)
        token = session.token
        caller.cancel()
        assert await caller.wait_for_final(timeout=3) == "cancelled"
        async with asyncio.timeout(3):
            while lab.calls.sessions:
                await asyncio.sleep(.005)
        assert [request.method for request in second.requests if request.method in {"CANCEL", "ACK", "BYE"}] == ["CANCEL", "ACK", "BYE"]
        assert not session.owns(token)
        assert_call_resources_released(lab)
    finally:
        await caller.close()
        audio.close()


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_redirected_account_uses_real_registered_contact_priority_and_fallback(trunks, monkeypatch):
    from .test_automation_originate_ha import Phone, PHONE_PASSWORD
    from custom_components.voip_stack.core.sip_auth import build_digest_authorization
    from custom_components.voip_stack.directory_runtime import compose_directory_entries

    lab = trunks
    port = lab.runtime.transport_config["sip_port"]
    remote_audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    remote_audio.bind(("127.0.0.1", 0))
    remote_audio.setblocking(False)
    transport, third = await lab.hass.loop.create_datagram_endpoint(
        lambda: Phone(remote_audio), local_addr=("127.0.0.1", 0))
    responses = asyncio.Queue()
    original = third.datagram_received

    def receive(raw, address):
        message = sip.parse_message(raw)
        if message.is_response and message.header("CSeq").endswith(" REGISTER"):
            responses.put_nowait(message)
        elif message.method == "OPTIONS":
            third.respond(message, address, 200)
        else:
            original(raw, address)
    third.datagram_received = receive
    uri = f"sip:127.0.0.1:{port}"
    address = transport.get_extra_info("sockname")

    async def register(cseq, authorization=""):
        headers = [("Via", f"SIP/2.0/UDP {address[0]}:{address[1]};branch=z9hG4bK-priority-{cseq};rport"),
            ("From", "<sip:bedroom1@127.0.0.1>;tag=priority"), ("To", "<sip:bedroom1@127.0.0.1>"),
            ("Call-ID", "priority-contact"), ("CSeq", f"{cseq} REGISTER"),
            ("Contact", f"<sip:bedroom1@{address[0]}:{address[1]}>;q=0.5"), ("Expires", "120")]
        if authorization:
            headers.append(("Authorization", authorization))
        transport.sendto(sip.build_request("REGISTER", uri, headers), ("127.0.0.1", port))
        return await asyncio.wait_for(responses.get(), 2)

    async def collect():
        while True:
            raw = await lab.hass.loop.sock_recv(remote_audio, 4096)
            third.audio.put_nowait((raw, lab.hass.loop.time()))

    collector = asyncio.create_task(collect())
    caller, audio = None, None
    try:
        challenge = await register(1)
        assert challenge.status_code == 401
        authorization = build_digest_authorization(challenge_header=challenge.header("WWW-Authenticate"),
            username="bedroom1", password=PHONE_PASSWORD, method="REGISTER", uri=uri)
        assert (await register(2, authorization)).status_code == 200
        contacts = lab.calls.component("registrar").registered_contacts("bedroom1")
        assert sorted(contact.q for contact in contacts) == [.5, 1.0]
        lab.roster[:] = compose_directory_entries(lab.hass, [], lab.calls.component("registrar").registered_roster_entries())
        lab.runtime.directory.replace(lab.roster)
        _redirect_phone(monkeypatch, lab.phones[0], [f"<sip:bedroom1@127.0.0.1:{port}>"])
        second = lab.phones[1]
        second.auto_answer = False
        caller, audio = await _source(lab)
        assert await caller.invite(target="bedroom0", remote_host="127.0.0.1", remote_sip_port=port, timeout=3) == "ringing"
        await asyncio.wait_for(second.invited.wait(), 3)
        assert not third.invites, "The lower-q registered Contact must wait for the first tier"
        second.respond(second.last_request, second.remote, 486)
        assert await caller.wait_for_final(timeout=3) == "in_call"
        assert len(third.invites) == 1
        await _sip_pcm(lab, caller, third, audio)
        assert caller.bye()
        async with asyncio.timeout(3):
            while lab.calls.sessions:
                await asyncio.sleep(.005)
        assert_call_resources_released(lab)
    finally:
        if caller is not None:
            await caller.close()
        if audio is not None:
            audio.close()
        collector.cancel()
        await asyncio.gather(collector, return_exceptions=True)
        transport.close()
        remote_audio.close()


@pytest.mark.parametrize("high,low", [
    ('"High, priority" <sip:high@pbx.test>;q=1', '<sip:low@pbx.test>;q=0.5'),
    ('sip:high@pbx.test;q=1', 'sip:low@pbx.test;q=0.5'),
])
def test_redirect_contact_list_preserves_quoted_names_and_standard_preferences(high, low):
    from custom_components.voip_stack.local_redirect import redirect_contacts
    assert [(q, str(uri)) for q, uri in redirect_contacts([
        f'{low}, {high}', '<sip:excluded@pbx.test>;q=0'])] == [
            (1.0, 'sip:high@pbx.test'), (.5, 'sip:low@pbx.test')]
