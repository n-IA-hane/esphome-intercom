"""A real incoming REFER retains the existing browser call and media owner."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from custom_components.voip_stack import softphone_originate, call_forwarder
from custom_components.voip_stack.core import sip
from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
from custom_components.voip_stack.roster import RosterEntry
from .test_trunk_calls_ha import lab as lab, trunks as trunks
from .test_automation_originate_ha import assert_call_resources_released
from .test_sip_phone_transfers_ha import registered_phones as registered_phones
from .test_phone_state_ha import phones as phones

pytestmark = pytest.mark.ha


async def wait_for(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


async def browser_call_setup(lab, monkeypatch):
    from custom_components.voip_stack.local_softphone_runtime import async_setup_local_softphone_bridge

    async_setup_local_softphone_bridge(lab.hass)
    for name in ("retained", "destination"):
        endpoint = PhoneEndpoint(name, name, EndpointKind.BROWSER, device_id="device-" + name,
                                 availability="available", capabilities=frozenset({"audio", "dtmf"}))
        lab.runtime.endpoints.register(endpoint)
        lab.roster.append(RosterEntry(name, metadata={"local_ha": True, "endpoint_id": name}))
    lab.runtime.directory.replace(lab.roster)
    monkeypatch.setattr(call_forwarder, "_roster_from_peers", lambda *_args: lab.roster)
    monkeypatch.setattr(call_forwarder, "_async_build_peer_snapshot", AsyncMock(return_value=[]))
    for phone in ("sip:bedroom0", "sip:bedroom1"):
        lab.runtime.endpoints.update(phone, availability="available")
    monkeypatch.setattr(softphone_originate, "_ha_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(softphone_originate, "_resolve_target_device", AsyncMock(return_value=None))
    return lab


async def browser_call(lab, monkeypatch, *, destination="bedroom0"):
    await browser_call_setup(lab, monkeypatch)
    await lab.hass.services.async_call("voip_stack", "call", {
        "device_id": "device-retained", "destination": destination, "media_client_id": "retained-owner",
    }, blocking=True)
    call_id = lab.runtime.endpoints.require("retained").active_call_id
    await wait_for(lambda: lab.calls.get_session(call_id).state == "in_call")
    return lab.calls.get_session(call_id)


def send_refer(lab, target):
    peer = lab.phones[0]
    invite = peer.last_request
    port = peer.transport.get_extra_info("sockname")[1]
    peer.transport.sendto(sip.build_request("REFER", sip.parse_sip_uri(invite.header("Contact")), [
        ("Via", f"SIP/2.0/UDP 127.0.0.1:{port};branch=z9hG4bK-retained-refer"),
        ("From", invite.header("To") + ";tag=bedroom"), ("To", invite.header("From")),
        ("Call-ID", invite.header("Call-ID")), ("CSeq", "42 REFER"),
        ("Refer-To", f"<{target}>"), ("Contact", f"<sip:bedroom0@127.0.0.1:{port}>"),
    ]), peer.remote)


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("target_kind", ["browser", "sip"])
@pytest.mark.parametrize("outcome", ["answer", "decline", "cancel"])
async def test_blind_refer_keeps_browser_and_waits_for_native_destination_answer(trunks, monkeypatch, target_kind, outcome):
    lab = trunks
    session = await browser_call(lab, monkeypatch)
    source = lab.calls.sip_client_for(session.call_id)
    original = lab.phones[0].datagram_received

    def receive(raw, address):
        message = sip.parse_message(raw)
        original(raw, address)
        if message.method == "NOTIFY":
            lab.phones[0].respond(message, address, 200)

    monkeypatch.setattr(lab.phones[0], "datagram_received", receive)
    lab.phones[1].auto_answer = False
    target = "destination" if target_kind == "browser" else "bedroom1"
    send_refer(lab, f"sip:{target}@127.0.0.1:{lab.runtime.transport_config['sip_port']}")
    await wait_for(lambda: any(m.method == "NOTIFY" and b"SIP/2.0 100" in m.body for m in lab.phones[0].requests))
    await wait_for(lambda: lab.runtime.endpoints.require("destination").active_call_id
                   if target_kind == "browser" else lab.phones[1].invited.is_set())
    assert session.live and not lab.phones[0].bye.is_set()
    assert lab.calls.sip_client_for(session.call_id) is source
    if outcome == "cancel":
        transaction = source._incoming_refer_task
        transaction.cancel()
        await asyncio.gather(transaction, return_exceptions=True)
    elif target_kind == "browser":
        await lab.hass.services.async_call("voip_stack", "answer" if outcome == "answer" else "decline", {
            "device_id": "device-destination", "call_id": session.call_id,
            **({"media_client_id": "destination-owner"} if outcome == "answer" else {}),
        }, blocking=True)
    else:
        lab.phones[1].final_status = 200 if outcome == "answer" else 486
        lab.phones[1].answer()
    if outcome != "answer":
        await wait_for(lambda: source._incoming_refer_task is None)
        assert session.live and session.state == "in_call" and session.owner == "ha_softphone"
        assert source.dialog is not None and not lab.phones[0].bye.is_set()
        assert lab.calls.sip_client_for(session.call_id) is source
        assert not lab.runtime.endpoints.require("destination").active_call_id
        await lab.hass.services.async_call("voip_stack", "hangup", {
            "device_id": "device-retained", "call_id": session.call_id,
        }, blocking=True)
        await wait_for(lambda: not lab.calls.sessions)
        assert_call_resources_released(lab)
        return
    await wait_for(lambda: any(m.method == "NOTIFY" and b"SIP/2.0 200" in m.body for m in lab.phones[0].requests))
    await wait_for(lab.phones[0].bye.is_set)
    assert lab.calls.get_session(session.call_id) is session and session.live
    assert lab.runtime.endpoints.require("retained").active_call_id == session.call_id
    assert lab.runtime.endpoints.require("destination" if target_kind == "browser" else "sip:bedroom1").active_call_id == session.call_id
    assert source._incoming_refer_task is None
    await lab.hass.services.async_call("voip_stack", "hangup", {
        "device_id": "device-retained", "call_id": session.call_id,
    }, blocking=True)
    await wait_for(lambda: not lab.calls.sessions)
    assert_call_resources_released(lab)
    await lab.hass.services.async_call("voip_stack", "call", {
        "device_id": "device-retained", "destination": "bedroom0", "media_client_id": "retained-owner",
    }, blocking=True)
    reused_id = lab.runtime.endpoints.require("retained").active_call_id
    assert reused_id != session.call_id
    await wait_for(lambda: lab.calls.get_session(reused_id).state == "in_call")
    await lab.hass.services.async_call("voip_stack", "hangup", {
        "device_id": "device-retained", "call_id": reused_id,
    }, blocking=True)
    await wait_for(lambda: not lab.calls.sessions)
    assert_call_resources_released(lab)


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("origin,target_kind", [("browser", "browser"), ("browser", "sip"), ("sip", "browser")])
async def test_attended_refer_reuses_answered_consultation(registered_phones, trunks, monkeypatch, origin, target_kind):
    lab = trunks
    operator = registered_phones[0]
    if origin == "browser":
        # The independent registered operator receives the retained browser call.
        session = await browser_call(lab, monkeypatch, destination="transfer_a")
    else:
        await browser_call_setup(lab, monkeypatch)
        await asyncio.to_thread(registered_phones[1].dial, "transfer_a")
        await wait_for(lambda: lab.runtime.endpoints.require("transfer_a").active_call_id)
        session = lab.calls.get_session(lab.runtime.endpoints.require("transfer_a").active_call_id)
        await wait_for(lambda: session.state == "in_call")
    await asyncio.to_thread(operator.wait_for, "Call established", 5)
    from custom_components.voip_stack import browser_transfer
    results = []
    execute = browser_transfer.transfer_browser_target
    async def observe(hass, runtime, current, referrer, request):
        result = await execute(hass, runtime, current, referrer, request)
        consult = runtime.sip.get_session(request.replaces.call_id) if request.replaces else None
        results.append((request, result, None if consult is None else (dict(consult.metadata),
            runtime.sip.bridge_link_for(consult.call_id),
            type(runtime.sip.resource_for(consult.call_id, "relay")).__name__)))
        return result
    monkeypatch.setattr(browser_transfer, "transfer_browser_target", observe)
    original_id = session.call_id
    target = "destination" if target_kind == "browser" else "transfer_c"
    before = len(operator.read())
    await asyncio.to_thread(operator.command, f"/atransferstart sip:{target}@127.0.0.1:{lab.runtime.transport_config['sip_port']}")
    if target_kind == "browser":
        await wait_for(lambda: lab.runtime.endpoints.require("destination").active_call_id)
        consult_id = lab.runtime.endpoints.require("destination").active_call_id
        await lab.hass.services.async_call("voip_stack", "answer", {
            "device_id": "device-destination", "call_id": consult_id, "media_client_id": "consult-owner",
        }, blocking=True)
    else:
        await wait_for(lambda: lab.runtime.endpoints.require("transfer_c").active_call_id)
        consult_id = lab.runtime.endpoints.require("transfer_c").active_call_id
    await wait_for(lambda: "Call established" in operator.read()[before:])
    destination_wire_id = lab.calls.bridge_link_for(consult_id)
    destination_client = lab.calls.sip_client_for(destination_wire_id)
    destination_relay = lab.calls.resource_for(consult_id, "relay")
    if origin == "sip":
        from dataclasses import replace
        from custom_components.voip_stack.call_replaces import prepare_replacement, prepare_replacement_grant
        from custom_components.voip_stack.core.sip_transfer import SipReferTarget, SipReplaces
        consultation = lab.calls.get_session(consult_id)
        manager = lab.calls.component("udp_listener")
        dialog = next(endpoint.active_dialogs[consult_id] for endpoint in manager._dialog_endpoints()
                      if consult_id in endpoint.active_dialogs)
        key = SipReplaces(consult_id, dialog.to_tag, sip.extract_tag(dialog.request.header("From")))
        referrer = lab.calls.sip_client_for(lab.calls.bridge_link_for(original_id))
        grant = prepare_replacement_grant(lab.runtime, original_id, SipReferTarget("sip:destination@127.0.0.1", key), referrer)
        original_media = lab.calls.resource_for(consult_id, "softphone_media")
        prepared = await prepare_replacement(lab.hass, replace(dialog.invite,
            call_id="aborted-browser-replacement", replaces=key, caller="transfer_b",
            identified_source_endpoint_id="transfer_b"), "127.0.0.1")
        assert prepared.status == 200
        await prepared.rollback()
        await prepared.rollback()
        assert session.live and consultation.live
        assert lab.calls.resource_for(consult_id, "softphone_media") is original_media
        assert lab.runtime.endpoints.require("destination").active_call_id == consult_id
        assert lab.runtime.endpoints.require("transfer_b").active_call_id == original_id
        assert not grant.current() and not lab.calls.get_session("aborted-browser-replacement")
    from custom_components.voip_stack.websocket_api import CALL_EVENT
    events = []
    remove = lab.hass.bus.async_listen(CALL_EVENT, lambda event: events.append(dict(event.data)))
    try:
        await asyncio.to_thread(operator.command, "/atransferexec")
        try:
            await wait_for(lambda: len(lab.calls.sessions) == 1
                           and not lab.runtime.endpoints.require("transfer_a").active_call_id)
        except TimeoutError:
            pytest.fail(f"REFER results={results}; operator={operator.read()[-2200:]}")
        remaining = next(iter(lab.calls.sessions.values()))
        assert remaining.live and remaining.state == "in_call"
        assert lab.runtime.endpoints.require(target).active_call_id == remaining.call_id
        if origin == "browser":
            assert remaining is session and remaining.call_id == original_id
            assert lab.runtime.endpoints.require("retained").active_call_id == original_id
        else:
            assert remaining.call_id not in {original_id, consult_id}
            assert lab.runtime.endpoints.require("transfer_b").active_call_id == remaining.call_id
        if target_kind == "sip":
            assert lab.calls.sip_client_for(destination_wire_id) is destination_client
            assert lab.calls.resource_for(remaining.call_id, "relay") is destination_relay
            assert registered_phones[2].read().count("Call established") == 1
        else:
            assert not [event for event in events if event.get("endpoint_id") == "destination"
                        and event.get("state") in {"ringing", "remote_ringing"}]
        assert not list(lab.calls.resource_items("replacement_grant"))
        if origin == "browser":
            await lab.hass.services.async_call("voip_stack", "hangup", {
                "device_id": "device-retained", "call_id": remaining.call_id,
            }, blocking=True)
        else:
            await asyncio.to_thread(registered_phones[1].hangup)
        await wait_for(lambda: not lab.calls.sessions)
        assert_call_resources_released(lab)
    finally:
        remove()
        for item in list(lab.calls.sessions.values()):
            await lab.calls.terminate_call_wait(item.call_id, reason="test_done")


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_blind_refer_preserves_active_browser_audio_socket_and_duplex_pcm(trunks, monkeypatch):
    import struct
    from types import SimpleNamespace
    from aiohttp import WSMsgType
    from custom_components.voip_stack import audio_ws, audio_ws_view
    from custom_components.voip_stack.core import rtp
    from .test_browser_paging_ha import Browser

    lab = trunks
    session = await browser_call(lab, monkeypatch)
    source = lab.calls.sip_client_for(session.call_id)
    active = audio_ws_view._active_softphone_media_session(lab.hass, "retained")
    browser = Browser()
    from custom_components.voip_stack.websocket_api import CALL_EVENT
    events = []
    detach = lab.hass.bus.async_listen(CALL_EVENT, lambda event: events.append(dict(event.data)))
    source.audio_rtp_socket = None  # The HTTP view transfers this exact fd to its worker.
    worker = asyncio.create_task(audio_ws_view._run_audio_session(lab.hass, browser, active, endpoint_id="retained"))
    receive = lab.phones[0].datagram_received
    def acknowledge(raw, address):
        message = sip.parse_message(raw)
        receive(raw, address)
        if message.method == "NOTIFY":
            lab.phones[0].respond(message, address, 200)
    monkeypatch.setattr(lab.phones[0], "datagram_received", acknowledge)
    try:
        await wait_for(lambda: bool(browser.contracts))
        original_port = active.local_rtp_port
        send_refer(lab, f"sip:bedroom1@127.0.0.1:{lab.runtime.transport_config['sip_port']}")
        await wait_for(lab.phones[0].bye.is_set)
        assert not worker.done(), [(e.get("endpoint_id"), e.get("state"), e.get("last_sip_event")) for e in events]
        assert lab.runtime.media.sessions_for("audio")[(session.call_id, "retained")] is active
        assert active.local_rtp_port == original_port and active.media_generation == 1
        relay = lab.calls.resource_for(session.call_id, "relay")
        fmt = active.send_format.audio_format
        for index in range(20):
            browser.incoming.put_nowait(SimpleNamespace(type=WSMsgType.BINARY,
                data=audio_ws.encode_audio_frame(struct.pack(f"<{fmt.nominal_frame_samples}h",
                    *([3911] * fmt.nominal_frame_samples)))))
            wire = relay.right.inbound_rtp_format
            samples = wire.audio_format.nominal_frame_samples
            packet = rtp.build_packet(rtp.RtpPacket(wire.payload_type, 100 + index,
                index * samples, 517, struct.pack(f">{samples}h", *([7711] * samples))))
            await lab.hass.loop.sock_sendto(lab.phones[1].rtp_socket, packet, ("127.0.0.1", relay.right_port))
            await asyncio.sleep(fmt.frame_ms / 1000)
        await wait_for(lambda: len(browser.frames) >= 2 and lab.phones[1].audio.qsize() >= 2)
        received = struct.unpack(f"<{len(browser.frames[-1]) // 2}h", browser.frames[-1])
        assert any(abs(value - 7711) <= 2 for value in received), "Actual SIP PCM must reach the retained browser"
        packets = []
        while not lab.phones[1].audio.empty():
            packets.append(rtp.parse_packet(lab.phones[1].audio.get_nowait()[0]))
        decoded = [value for packet in packets for value in struct.unpack(f">{len(packet.payload) // 2}h", packet.payload)]
        assert any(abs(value - 3911) <= 2 for value in decoded), "Actual browser PCM must reach the selected SIP peer"
        await lab.hass.services.async_call("voip_stack", "hangup", {
            "device_id": "device-retained", "call_id": session.call_id,
        }, blocking=True)
        await asyncio.wait_for(worker, 2)
        assert_call_resources_released(lab)
    finally:
        detach()
        browser.force_close()
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_overlapping_route_publication_coalesces_without_cancelling_browser_media(trunks, monkeypatch):
    from types import SimpleNamespace
    from custom_components.voip_stack import audio_ws_view, call_projection, device_resolver, esphome_actions
    from .test_browser_paging_ha import Browser

    lab = trunks
    session = await browser_call(lab, monkeypatch)
    client = lab.calls.sip_client_for(session.call_id)
    active = audio_ws_view._active_softphone_media_session(lab.hass, "retained")
    client.audio_rtp_socket = None
    browser = Browser()
    media = asyncio.create_task(audio_ws_view._run_audio_session(lab.hass, browser, active, endpoint_id="retained"))
    entered, old_cancelled, release = asyncio.Queue(), asyncio.Event(), asyncio.Event()
    sent = []
    async def devices():
        entered.put_nowait(True)
        try:
            await release.wait()
        except asyncio.CancelledError:
            old_cancelled.set()
            raise
        return [{"device_id": "device-bedroom0"}]
    async def deliver(hass, device, action, data):
        sent.append((device["device_id"], action, data))
    monkeypatch.setattr(device_resolver, "get_resolver", lambda _hass: SimpleNamespace(list_devices=devices))
    monkeypatch.setattr(esphome_actions, "has_action", lambda *_args: True)
    monkeypatch.setattr(esphome_actions, "async_call_action", deliver)
    try:
        await wait_for(lambda: bool(browser.contracts))
        # The initial attachment can publish the same route too; wait for its
        # real notification, then supersede it while discovery is still pending.
        await asyncio.wait_for(entered.get(), 2)
        first = lab.calls.task_for(session.call_id, f"voip-media-route-{session.call_id}")
        call_projection.publish_esp_media_route(lab.hass, session.call_id, "ha_transcoding")
        latest = lab.calls.task_for(session.call_id, f"voip-media-route-{session.call_id}")
        await asyncio.wait_for(entered.get(), 2)
        await asyncio.wait_for(old_cancelled.wait(), 2)
        assert first is not latest and first.cancelled()
        assert not media.done() and client.dialog is not None
        release.set()
        await asyncio.wait_for(latest, 2)
        assert sent == [("device-bedroom0", "set_media_route", {"call_id": session.call_id, "route": "ha_transcoding"})]
        assert lab.runtime.media.sessions_for("audio")[(session.call_id, "retained")] is active
        await lab.hass.services.async_call("voip_stack", "hangup", {
            "device_id": "device-retained", "call_id": session.call_id,
        }, blocking=True)
        await asyncio.wait_for(media, 2)
        assert_call_resources_released(lab)
        assert first.done() and latest.done()
    finally:
        release.set()
        browser.force_close()
        media.cancel()
        await asyncio.gather(media, return_exceptions=True)


@pytest.fixture
async def browser_replacement_owner(hass, phones, socket_enabled, request):
    from types import SimpleNamespace
    from custom_components.voip_stack.core import sdp
    from custom_components.voip_stack.core.sip_transfer import SipReplaces
    from custom_components.voip_stack.sip_listener import SipInvite

    calls = phones.calls
    phones.runtime.transport_config.update(sip_port=5060, rtp_port=18000)
    fmt = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
    previous = SipInvite(source_host="127.0.0.1", source_port=5060,
        request_uri=sip.parse_sip_uri("sip:kitchen@127.0.0.1"),
        caller_uri=sip.parse_sip_uri("sip:office@127.0.0.1"), target="Kitchen", caller="Office",
        call_id="answered-browser", cseq="1 INVITE", remote_sdp=(
            "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Owned peer\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
            "m=audio 19000 RTP/AVP 96\r\na=rtpmap:96 L16/16000\r\na=ptime:20\r\n").encode(),
        send_format=fmt, recv_format=fmt, remote_rtp_host="127.0.0.1", remote_rtp_port=19000,
        identified_source_endpoint_id="office")
    initial = "ringing" if getattr(request.node, "callspec", None) and request.node.callspec.params.get("failure") == "owner" else "in_call"
    old = calls.upsert(previous.call_id, state=initial, owner="ha_softphone",
        caller="Office", callee="Kitchen", source_endpoint_id="office", dest_endpoint_id="kitchen")
    calls.claim_endpoint(old.call_id, "office", role="source")
    calls.claim_endpoint(old.call_id, "kitchen", role="destination")
    media = {"invite": previous, "local_rtp_port": 18000, "local_video_rtp_port": 0,
             "endpoint_id": "kitchen", "media_client_id": "kitchen-owner"}
    calls.attach_media(old.call_id, media)
    from dataclasses import replace
    invite = replace(previous, call_id="browser-successor",
                     replaces=SipReplaces(old.call_id, "local-tag", "remote-tag"))
    return SimpleNamespace(hass=hass, calls=calls, runtime=phones.runtime, old=old,
                           previous=previous, invite=invite, media=media)


@pytest.mark.parametrize("failure", ["owner", "claim", "answer", "upsert", "publish"])
async def test_rejected_browser_replacement_releases_automatic_grant_and_allows_retry(browser_replacement_owner, monkeypatch, failure):
    from custom_components.voip_stack import browser_replacement, call_replaces
    from custom_components.voip_stack.core.sip_transfer import SipReferTarget

    lab = browser_replacement_owner
    candidate = lab.invite
    if failure == "claim":
        lab.calls.release_endpoint_claim(lab.old.call_id, "kitchen")
    with monkeypatch.context() as inject:
        if failure in {"answer", "upsert", "publish"}:
            def fail(*_args, **_kwargs):
                raise ValueError("injected preparation failure")
            if failure == "publish":
                snapshot = lab.calls._state_snapshot
                fired = False
                def fail_publication(session):
                    nonlocal fired
                    if session.call_id == lab.invite.call_id and not fired:
                        fired = True
                        raise ValueError("injected preparation failure")
                    return snapshot(session)
                inject.setattr(lab.calls, "_state_snapshot", fail_publication)
            else:
                inject.setattr(browser_replacement if failure == "answer" else lab.calls,
                               "build_answer_directional" if failure == "answer" else "upsert", fail)
            with pytest.raises(ValueError, match="injected preparation failure"):
                await call_replaces._prepare_replacement_locked(lab.hass, candidate, "127.0.0.1", lab.previous)
        else:
            result = await call_replaces._prepare_replacement_locked(lab.hass, candidate, "127.0.0.1", lab.previous)
            assert result.status == 488
    assert lab.calls.resource_for(lab.old.call_id, "replacement_grant") is None
    assert lab.old.live and list(lab.calls.sessions.values()) == [lab.old]
    assert lab.calls.resource_for(lab.old.call_id, "softphone_media") is lab.media
    assert not lab.runtime.rtp_port_pool.get("used")
    lab.calls.transition(lab.old.call_id, state="in_call")
    lab.calls.claim_endpoint(lab.old.call_id, "kitchen", role="destination")
    monkeypatch.setattr(call_replaces, "replacement_invite", lambda _runtime, key:
                        lab.previous if key == lab.invite.replaces else None)
    retry = call_replaces.prepare_replacement_grant(lab.runtime, lab.old.call_id,
        SipReferTarget("sip:kitchen@127.0.0.1", lab.invite.replaces), None)
    assert retry.current(), "The rejected attempt must not block a new legitimate REFER with 491"
    retry.release()
    result = await call_replaces._prepare_replacement_locked(lab.hass, lab.invite, "127.0.0.1", lab.previous)
    assert result.status == 200
    await result.rollback()
    assert lab.old.live and list(lab.calls.sessions.values()) == [lab.old]


async def test_browser_replacement_rollback_cancellation_never_cancels_owned_cleanup(browser_replacement_owner):
    from custom_components.voip_stack.call_replaces import _prepare_replacement_locked
    from custom_components.voip_stack.endpoint_session import CleanupStage, TerminationIntent

    lab = browser_replacement_owner
    result = await _prepare_replacement_locked(lab.hass, lab.invite, "127.0.0.1", lab.previous)
    assert result.status == 200
    new = lab.calls.get_session(lab.invite.call_id)
    entered, release = asyncio.Event(), asyncio.Event()
    closed = []
    async def close(reason):
        closed.append(reason)
        entered.set()
        await release.wait()
    new.add_resource("blocked-replacement-cleanup", release, close, stage=CleanupStage.MEDIA)
    rollback = asyncio.create_task(result.rollback())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        barrier = lab.calls.request_termination(new.call_id, TerminationIntent("replacement_failed"))
        rollback.cancel()
        await asyncio.sleep(0)
        rollback.cancel()
        await asyncio.sleep(0)
        assert not rollback.done() and barrier.cancelling() == 0
        release.set()
        completed = (await asyncio.gather(rollback, return_exceptions=True))[0]
        assert isinstance(completed, asyncio.CancelledError)
        assert new.terminated.is_set() and closed == ["replacement_failed"]
        await result.rollback()
        assert lab.old.live and list(lab.calls.sessions.values()) == [lab.old]
        assert lab.calls.resource_for(lab.old.call_id, "softphone_media") is lab.media
        assert lab.calls.resource_for(lab.old.call_id, "replacement_grant") is None
    finally:
        release.set()
        await asyncio.gather(rollback, return_exceptions=True)


async def test_consecutive_browser_transfer_retirements_keep_distinct_owned_tasks(hass, phones):
    from custom_components.voip_stack.browser_transfer import RetainedBrowserSource
    from custom_components.voip_stack.endpoint_session import TerminationIntent
    from .test_ha_call_transfer import _established

    calls = phones.calls
    session = calls.upsert("retained-browser", state="in_call", owner="ha_softphone",
        caller="Office", callee="Kitchen", source_endpoint_id="office", target_endpoint_id="kitchen")
    calls.claim_endpoint(session.call_id, "office", role="source")
    gates = [asyncio.Event(), asyncio.Event()]
    entered = [asyncio.Event(), asyncio.Event()]
    closed, clients = [], []
    try:
        for index in range(2):
            wire = f"retired-operator-{index}"
            client = _established(wire, f"remote-{index}", "kitchen")
            clients.append(client)
            session.update_metadata(target_endpoint_id="kitchen")
            calls.add_leg(session.call_id, wire, role="callee", state="in_call")
            calls.attach_sip_client(session.call_id, wire, client, role="callee", state="in_call")
            async def close(_reason, index=index):
                closed.append(index)
                entered[index].set()
                await gates[index].wait()
            session.legs[wire].closer = close
            source = RetainedBrowserSource(hass, session, client, "127.0.0.1", 5060)
            source.transaction = None
            source.commit()
            source.after_refer(source.close_referrer)
            await asyncio.wait_for(entered[index].wait(), 2)
        tasks = tuple(task for name, task in session.named_tasks.items()
                      if name.startswith("browser-transfer-retirement"))
        assert len(tasks) == 2 and len({task.get_name() for task in tasks}) == 2
        teardown = calls.request_termination(session.call_id, TerminationIntent("test_done"))
        await asyncio.sleep(0)
        assert not teardown.done() and sorted(closed) == [0, 1]
        for gate in gates:
            gate.set()
        await asyncio.wait_for(teardown, 2)
        assert all(task.done() for task in tasks) and sorted(closed) == [0, 1]
        assert not calls.sessions and not calls.endpoint_claims_snapshot()
    finally:
        for gate in gates:
            gate.set()
        leftovers = [task for task in asyncio.all_tasks() if task.get_name().startswith("browser-transfer-retirement")]
        if leftovers:
            await asyncio.gather(*leftovers, return_exceptions=True)
        for client in clients:
            await client.close()


@pytest.mark.parametrize("camera", [False, True])
@pytest.mark.parametrize("settle", ["rollback", "commit"])
async def test_browser_replaces_video_uses_standard_contract_and_owned_reservation(
    browser_replacement_owner, camera, settle,
):
    from dataclasses import replace
    from custom_components.voip_stack.browser_replacement import prepare_browser_replacement
    from custom_components.voip_stack.call_replaces import ReplacementGrant
    from custom_components.voip_stack.const import CONF_VIDEO_CAMERA_SEND
    from custom_components.voip_stack.core import sdp
    from custom_components.voip_stack.endpoint_session import TerminationIntent
    from custom_components.voip_stack.media_ports import bind_sip_video_sockets

    lab = browser_replacement_owner
    lab.runtime.endpoints.update("kitchen", capabilities=frozenset({"audio", "video", "dtmf"}))
    lab.runtime.transport_config[CONF_VIDEO_CAMERA_SEND] = camera
    video = sdp.RtpVideoFormat(payload_type=103, encoding="VP8", direction="sendrecv")
    offer = lab.previous.remote_sdp + (
        "m=video 19002 RTP/AVP 103\r\na=rtpmap:103 VP8/90000\r\na=sendrecv\r\n").encode()
    previous = replace(lab.previous, remote_sdp=offer, video_format=video,
        remote_video_rtp_host="127.0.0.1", remote_video_rtp_port=19002)
    old_rtp, old_rtcp = bind_sip_video_sockets(18002)
    lab.media.update(invite=previous, local_video_rtp_port=18002,
        video_rtp_socket=old_rtp, video_rtcp_socket=old_rtcp, camera_send_authorized=camera)
    # This fixture seeds an already authorized grant. The independent wire
    # tests cover REFER authorization and the Replaces tag matching itself.
    lab.calls.release_endpoint_claim(lab.old.call_id, "office")
    lab.calls.observe_leg(lab.old.call_id, "browser:kitchen", role="ha_softphone",
        endpoint_id="kitchen", state="in_call", generation=lab.old.generation)
    grant = ReplacementGrant(lab.old, lab.old, "office", lab.invite.replaces)
    grant.attach()
    incoming = replace(previous, call_id=lab.invite.call_id, replaces=lab.invite.replaces)
    result = await prepare_browser_replacement(lab.hass, lab.runtime, incoming, "127.0.0.1", lab.old, grant)
    assert result.status == 200
    assert "a=x-" not in result.answer_sdp
    assert "a=rtpmap:103 VP8/90000" in result.answer_sdp
    assert ("a=sendrecv" if camera else "a=recvonly") in result.answer_sdp.split("m=video", 1)[1]
    assert lab.runtime.rtp_port_pool.get("used")
    if settle == "rollback":
        await result.rollback()
        assert lab.old.live and lab.calls.resource_for(lab.old.call_id, "softphone_media") is lab.media
        assert old_rtp.fileno() >= 0 and old_rtcp.fileno() >= 0
        assert not lab.runtime.rtp_port_pool.get("used")
    else:
        await result.commit()
        current = lab.calls.get_session(incoming.call_id)
        descriptor = lab.calls.resource_for(incoming.call_id, "softphone_media")
        assert current.live and lab.runtime.endpoints.require("kitchen").active_call_id == incoming.call_id
        assert descriptor["video_direction"] == ("sendrecv" if camera else "recvonly")
        assert descriptor["rtp_reservation"] is not None and not descriptor["rtp_reservation"].released
        assert descriptor["media_handoff"] == {"previous_call_id": lab.old.call_id, "endpoint_id": "kitchen"}
        assert "browser:kitchen" not in lab.old.legs
        await lab.calls.terminate_call_wait(current.call_id, intent=TerminationIntent("test_complete"))
        assert not lab.runtime.rtp_port_pool.get("used")
    if lab.old.live:
        await lab.calls.terminate_call_wait(lab.old.call_id, intent=TerminationIntent("test_complete"))


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.timeout(90)
async def test_browser_refer_uses_destination_no_answer_policy_beyond_thirty_seconds(trunks, monkeypatch):
    from custom_components.voip_stack.phone_policy import PhonePolicy, PhoneDestination

    lab = trunks
    session = await browser_call(lab, monkeypatch)
    lab.runtime.endpoints.update("sip:bedroom1", policy=PhonePolicy(
        ringing_timeout=45, no_answer=PhoneDestination("contact", "bedroom0")))
    phone, target = lab.phones
    target.auto_answer = False
    receive = phone.datagram_received
    def dispatch(raw, address):
        message = sip.parse_message(raw)
        receive(raw, address)
        if message.method == "NOTIFY":
            phone.respond(message, address, 200)
    monkeypatch.setattr(phone, "datagram_received", dispatch)
    send_refer(lab, f'sip:bedroom1@127.0.0.1:{lab.runtime.transport_config["sip_port"]}')
    await asyncio.wait_for(target.invited.wait(), 4)
    source = lab.calls.sip_client_for(session.call_id)
    await asyncio.sleep(30.2)
    assert session.live and source._incoming_refer_task is not None
    assert not source._incoming_refer_task.done() and source.dialog is not None
    target.answer()
    await wait_for(lambda: session.state == "in_call" and "sip:bedroom1" in session.endpoint_claims)
    await lab.hass.services.async_call("voip_stack", "hangup", {
        "device_id": "device-retained", "call_id": session.call_id,
    }, blocking=True)
    await wait_for(lambda: not lab.calls.sessions)
    assert_call_resources_released(lab)
