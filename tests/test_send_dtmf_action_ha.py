"""Public HA action sends through the real relay and browser RTP owners."""

import asyncio

import pytest
from homeassistant.core import Context
from homeassistant.exceptions import ServiceValidationError, Unauthorized

from custom_components import voip_stack
from custom_components.voip_stack import audio_ws_view
from custom_components.voip_stack.browser_phone_runtime import _set_ha_softphone_call_state
from custom_components.voip_stack.core import rtp, sdp, sip
from custom_components.voip_stack.dtmf import RtpDtmfDecoder
from custom_components.voip_stack.dtmf_events import attach_dtmf_event_bridge
from custom_components.voip_stack.sip_client import SipCallClient
from .test_automation_originate_ha import lab as lab
from .test_browser_pcm_edge_ha import WebSocketBoundary
from .test_media_rtp_audit_ha import audio_path, receive_datagram, udp_socket

pytestmark = pytest.mark.ha


async def call_action(lab, endpoint, **values):
    return await lab.hass.services.async_call("voip_stack", "send_dtmf",
        {"device_id": endpoint.ha_device_id, "digits": "5", "duration_ms": 80, **values},
        blocking=True, return_response=True)


def active_call(lab, *, identity="keypad-call", role="source", phone=0):
    endpoint = lab.runtime.endpoints.by_username(f"bedroom{phone}")
    session = lab.calls.upsert(identity, state="in_call", owner="sip_bridge")
    assert lab.calls.claim_endpoint(session.call_id, endpoint.endpoint_id, role=role)
    return endpoint, session


@pytest.mark.parametrize("role,rate", [("source", 8000), ("destination", 48000)])
async def test_service_uses_selected_phone_direction_and_negotiated_rtp_clock(lab, role, rate):
    await voip_stack._async_register_services(lab.hass)
    endpoint, session = active_call(lab, role=role)
    fmt = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
    async with audio_path(fmt, events=True) as (relay, left, right, _observed):
        target = relay.right if role == "source" else relay.left
        target.send_dtmf_clock_rate = rate
        lab.calls.attach_relay(session.call_id, relay)
        peer = right if role == "source" else left
        action = asyncio.create_task(call_action(lab, endpoint, digits="5#a", gap_ms=50,
            expected_generation=session.generation))
        packets = []
        async with asyncio.timeout(3):
            while sum(bool(packet.payload[1] & 128) for packet in packets) < 9:
                packet = rtp.parse_packet(await receive_datagram(peer))
                assert packet.payload_type == 110
                packets.append(packet)
        result = await action
        assert result == {"call_id": session.call_id, "generation": session.generation,
                          "digits": "5#A", "sent": 3}
        final = [packet for packet in packets if packet.payload[1] & 128]
        assert [packet.payload[0] for packet in final] == [5] * 3 + [11] * 3 + [12] * 3
        assert {int.from_bytes(packet.payload[2:4], "big") for packet in final} == {rate * 80 // 1000}
        assert all((b.sequence - a.sequence) & 65535 == 1 for a, b in zip(packets, packets[1:]))
        decoder = RtpDtmfDecoder((sdp.RtpDtmfFormat(110, rate),))
        assert [digit for packet in packets if (digit := decoder.decode(rtp.build_packet(packet)))] == ["5", "#", "A"]


@pytest.mark.parametrize("failure", ["other_phone", "generation", "not_connected", "no_owner", "multiple_calls"])
async def test_service_rejects_an_ambiguous_or_unowned_call_without_sending(lab, failure):
    await voip_stack._async_register_services(lab.hass)
    endpoint, session = active_call(lab)
    data = {"call_id": session.call_id}
    if failure == "other_phone":
        endpoint = lab.runtime.endpoints.by_username("bedroom1")
    elif failure == "generation":
        data["expected_generation"] = session.generation + 1
    elif failure == "not_connected":
        lab.calls.upsert(session.call_id, state="ringing")
    elif failure == "multiple_calls":
        active_call(lab, identity="consultation-call")
        data = {}
    with pytest.raises(ServiceValidationError):
        await call_action(lab, endpoint, **data)


async def test_authenticated_user_still_needs_native_control_permission(lab, hass_read_only_user):
    await voip_stack._async_register_services(lab.hass)
    endpoint, session = active_call(lab)
    with pytest.raises(Unauthorized):
        await lab.hass.services.async_call("voip_stack", "send_dtmf",
            {"device_id": endpoint.ha_device_id, "call_id": session.call_id, "digits": "5"},
            blocking=True, context=Context(user_id=hass_read_only_user.id))


async def test_browser_and_service_share_one_sender_and_teardown_retires_the_command(lab, monkeypatch):
    await voip_stack._async_register_services(lab.hass)
    endpoint, session = active_call(lab)
    _set_ha_softphone_call_state(lab.hass, "in_call", call_id=session.call_id,
        endpoint_id=endpoint.endpoint_id, direction="outgoing")
    peer, bound = udp_socket(), udp_socket()
    fmt = sdp.RtpPcmFormat(0, "PCMU", 8000, 1, 20)
    media = audio_ws_view._SoftphoneMediaSession(call_id=session.call_id,
        local_rtp_port=bound.getsockname()[1], rtp_socket=bound,
        remote_rtp_host="127.0.0.1", remote_rtp_port=peer.getsockname()[1],
        send_format=fmt, recv_format=fmt,
        send_dtmf_payload_type=111, send_dtmf_clock_rate=48000,
        send_dtmf_events=frozenset(range(16)))
    ws = WebSocketBoundary()
    monkeypatch.setattr(audio_ws_view, "_publish_ha_softphone_state", lambda *_args, **_kwargs: None)
    worker = asyncio.create_task(audio_ws_view._run_audio_session(lab.hass, ws, media,
        endpoint_id=endpoint.endpoint_id))
    try:
        async with asyncio.timeout(2):
            while media.schedule_dtmf is None:
                await asyncio.sleep(.001)
        queued = media.schedule_dtmf("1", duration_ms=80)
        assert queued is not None
        action = asyncio.create_task(call_action(lab, endpoint, digits="2"))
        events = []
        async with asyncio.timeout(2):
            while len(events) < 6:
                packet = rtp.parse_packet(await receive_datagram(peer))
                if packet.payload_type == 111 and packet.payload[1] & 128:
                    events.append(packet)
        assert await queued
        assert (await action)["sent"] == 1
        assert [packet.payload[0] for packet in events] == [1] * 3 + [2] * 3
        assert len({packet.ssrc for packet in events}) == 1
        assert {int.from_bytes(packet.payload[2:4], "big") for packet in events} == {3840}
    finally:
        ws.messages.put_nowait(None)
        await asyncio.wait_for(worker, 2)
        peer.close()
    assert media.schedule_dtmf is None
    with pytest.raises(ServiceValidationError, match="media owner"):
        await call_action(lab, endpoint)


async def test_failed_info_fallback_is_an_action_error_and_stops_the_sequence(lab):
    await voip_stack._async_register_services(lab.hass)
    endpoint, session = active_call(lab)
    fmt = sdp.RtpPcmFormat(0, "PCMU", 8000, 1, 20)
    async with audio_path(fmt) as (relay, _left, _right, _observed):
        lab.calls.attach_relay(session.call_id, relay)
        observed = []
        async def info(side, digit, duration):
            observed.append((side, digit, duration))
            return False
        relay.on_dtmf_fallback = info
        with pytest.raises(ServiceValidationError, match="could not send"):
            await call_action(lab, endpoint, digits="123", duration_ms=320)
        assert observed == [("left", "1", 320)]


async def test_info_action_reuses_the_established_sip_dialog_and_requested_duration(lab):
    await voip_stack._async_register_services(lab.hass)
    endpoint, session = active_call(lab)
    phone = lab.phones[1]
    received = []
    previous = phone.datagram_received

    def on_message(raw, address):
        message = sip.parse_message(raw)
        if message.method == "INFO":
            received.append(message)
            phone.respond(message, address, 200)
        else:
            previous(raw, address)

    phone.datagram_received = on_message
    fmt = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
    async with audio_path(fmt) as (relay, _left, _right, _observed):
        client = SipCallClient(local_ip="127.0.0.1", local_name=endpoint.username,
            local_sip_port=0, local_rtp_port=relay.right_port)
        try:
            progress = await client.invite(target="bedroom1", remote_host="127.0.0.1",
                remote_sip_port=phone.transport.get_extra_info("sockname")[1], timeout=2)
            assert progress in {"ringing", "in_call"}
            assert await client.wait_for_final(timeout=2) == "in_call"
            lab.calls.attach_relay(session.call_id, relay)
            attach_dtmf_event_bridge(lab.hass, relay, call_id=session.call_id,
                dest_call_id=client.dialog.call_id, caller="bedroom0", callee="bedroom1", client=client)
            result = await call_action(lab, endpoint, digits="5#", duration_ms=320, gap_ms=50)
            assert result["sent"] == 2
            assert [request.body for request in received] == [
                b"Signal=5\r\nDuration=320\r\n", b"Signal=#\r\nDuration=320\r\n"]
            assert all(request.header("Call-ID") == client.dialog.call_id for request in received)
            assert all(request.header("Content-Type") == "application/dtmf-relay" for request in received)
            assert len(phone.invites) == 1
        finally:
            await client.close()


async def test_call_termination_cancels_the_owned_sequence_before_another_digit(lab):
    await voip_stack._async_register_services(lab.hass)
    endpoint, session = active_call(lab)
    fmt = sdp.RtpPcmFormat(0, "PCMU", 8000, 1, 20)
    async with audio_path(fmt, events=True) as (relay, _left, right, _observed):
        lab.calls.attach_relay(session.call_id, relay)
        action = asyncio.create_task(call_action(lab, endpoint, digits="12", duration_ms=80, gap_ms=500))
        first = rtp.parse_packet(await receive_datagram(right))
        assert first.payload[0] == 1
        await lab.calls.terminate_call_wait(session.call_id, reason="remote_hangup")
        with pytest.raises(ServiceValidationError, match="call ended"):
            await action
        assert not relay._dtmf_tasks
        assert not session.named_tasks
        while True:
            try:
                raw = right.recv(4096)
            except BlockingIOError:
                break
            assert rtp.parse_packet(raw).payload[0] != 2


@pytest.mark.parametrize("explicit", [False, True])
async def test_automation_originates_then_sends_info_on_its_actual_dialog(lab, explicit):
    """Issue 149: an Automation contact is the caller, not a phone selector."""
    from .test_automation_originate_ha import dial

    await voip_stack._async_register_services(lab.hass)
    phone = lab.phones[0]
    received = []
    previous = phone.datagram_received

    def on_message(raw, address):
        request = sip.parse_message(raw)
        if request.method == "INFO":
            received.append(request)
            phone.respond(request, address, 200)
        else:
            previous(raw, address)

    phone.datagram_received = on_message
    context = Context()
    token = await dial(lab, context)
    session = lab.calls.get_session(token["call_id"])
    media = lab.calls.resource_for(session.call_id, "relay")
    transport = media.transport
    response = await lab.hass.services.async_call("voip_stack", "send_dtmf",
        {"digits": "5#a", "duration_ms": 320, "gap_ms": 50,
         **({"call_id": token["call_id"], "expected_generation": token["generation"]} if explicit else {})},
        blocking=True, return_response=True, context=context)
    assert response == {**token, "digits": "5#A", "sent": 3}
    assert [request.body for request in received] == [
        b"Signal=5\r\nDuration=320\r\n", b"Signal=#\r\nDuration=320\r\n",
        b"Signal=A\r\nDuration=320\r\n"]
    assert {request.header("Call-ID") for request in received} == {token["call_id"]}
    assert media.transport is transport and not transport.is_closing()
    assert len(phone.invites) == 1


def negotiate_phone_events(phone, monkeypatch, rate):
    """An independent peer accepts only one offered telephone-event clock."""
    import re

    respond = phone.respond

    def with_events(request, address, status, body=b""):
        if status == 200 and request.method == "INVITE" and body:
            match = re.search(rb"a=rtpmap:(\d+) (?:telephone-event|TELEPHONE-EVENT)/" + str(rate).encode() + rb"\r", request.body)
            assert match is not None, request.body
            payload = match[1].decode()
            text = body.decode()
            text = re.sub(r"(m=audio [^\r]+)(\r\n)", rf"\g<1> {payload}\2", text)
            body = (text + f"a=rtpmap:{payload} telephone-event/{rate}\r\na=fmtp:{payload} 0-15\r\n").encode()
        respond(request, address, status, body)

    monkeypatch.setattr(phone, "respond", with_events)


@pytest.mark.parametrize("rate", [8000, 16000, 48000])
async def test_outgoing_automation_shares_audio_owner_and_receives_reply_after_rtp_digits(lab, monkeypatch, rate):
    from .test_automation_originate_ha import dial, tts_fixture
    from custom_components.voip_stack.dtmf import build_telephone_event_payload

    await voip_stack._async_register_services(lab.hass)
    phone = lab.phones[0]
    negotiate_phone_events(phone, monkeypatch, rate)
    stream = tts_fixture(monkeypatch, lab)
    context = Context()
    token = await dial(lab, context)
    app = lab.calls.resource_for(token["call_id"], "automation")
    media, transport = app.media, app.media.transport
    event_format = media.invite.dtmf_direction.send
    assert event_format.sample_rate == rate
    assert media.encoder.fmt.rtp_clock_rate == 16000
    await lab.hass.services.async_call("voip_stack", "tts_say", {
        "tts_entity_id": "tts.test", "message": "Wake up"}, blocking=True, context=context)
    response = await lab.hass.services.async_call("voip_stack", "send_dtmf",
        {"digits": "5#", "duration_ms": 80, "gap_ms": 50},
        blocking=True, return_response=True, context=context)
    assert response == {**token, "digits": "5#", "sent": 2}
    assert media.transport is transport and len(phone.invites) == 1
    packets = []
    async with asyncio.timeout(2):
        while sum(packet.payload_type == event_format.payload_type and bool(packet.payload[1] & 128)
                  for packet in packets) < 6:
            packets.append(rtp.parse_packet((await phone.audio.get())[0]))
    audio = [packet for packet in packets if packet.payload_type == phone.payload_type]
    events = [packet for packet in packets if packet.payload_type == event_format.payload_type]
    final = [packet for packet in events if packet.payload[1] & 128]
    assert [packet.payload[0] for packet in final] == [5] * 3 + [11] * 3
    assert {int.from_bytes(packet.payload[2:4], "big") for packet in final} == {rate * 80 // 1000}
    assert b"".join(packet.payload for packet in audio if any(packet.payload)) == b"\x01\x80" * 960
    assert len({packet.ssrc for packet in audio}) == len({packet.ssrc for packet in events}) == 1
    assert (audio[0].ssrc == events[0].ssrc) is (rate == 16000)
    if rate == 16000:
        assert all((b.sequence - a.sequence) & 65535 == 1 for a, b in zip(packets, packets[1:]))
    else:
        assert all((b.sequence - a.sequence) & 65535 == 1 for a, b in zip(events, events[1:]))
    decoder = RtpDtmfDecoder((event_format,))
    assert [digit for packet in events if (digit := decoder.decode(rtp.build_packet(packet)))] == ["5", "#"]

    waiting = asyncio.create_task(lab.hass.services.async_call("voip_stack", "wait_for_dtmf", {
        "timeout": 1}, blocking=True, return_response=True, context=context))
    async with asyncio.timeout(1):
        while not app.lock.locked():
            await asyncio.sleep(0)
    await asyncio.sleep(0)
    received_format = media.invite.dtmf_direction.recv
    raw = rtp.build_packet(rtp.RtpPacket(received_format.payload_type, 7, 1000, 123,
        build_telephone_event_payload("8", duration=received_format.sample_rate // 10, end=True)))
    await lab.hass.loop.sock_sendto(phone.rtp_socket, raw, ("127.0.0.1", media.local_rtp_port))
    assert await waiting == {"status": "received", "digits": "8"}
    assert media.transport is transport and len(stream.calls) == 1


async def test_outgoing_info_uses_retargeted_dialog_not_the_canonical_call_id(lab):
    from functools import partial
    from types import SimpleNamespace
    from .test_automation_originate_ha import dial
    from custom_components.voip_stack.call_forwarder import async_forward_existing_call
    from custom_components.voip_stack.phone_policy import PhonePolicy

    await voip_stack._async_register_services(lab.hass)
    lab.runtime.endpoints.update("bedroom0", policy=PhonePolicy.from_data({
        "ringing_timeout": 1, "no_answer": {"kind": "contact", "target": "bedroom1"}}))
    lab.calls.forward_call = partial(async_forward_existing_call, SimpleNamespace(
        hass=lab.hass, config=lab.runtime.transport_config, local_ip="127.0.0.1"))
    first, second = lab.phones
    first.auto_answer = False
    received = []
    previous = second.datagram_received

    def on_message(raw, address):
        request = sip.parse_message(raw)
        if request.method == "INFO":
            received.append(request)
            second.respond(request, address, 200)
        else:
            previous(raw, address)

    second.datagram_received = on_message
    context = Context()
    token = await dial(lab, context, answer_timeout=3)
    assert token["call_id"] == first.invites[0].header("Call-ID")
    assert token["call_id"] != second.invites[0].header("Call-ID")
    result = await lab.hass.services.async_call("voip_stack", "send_dtmf", {"digits": "5"},
        blocking=True, return_response=True, context=context)
    assert result["call_id"] == token["call_id"]
    assert len(received) == 1 and received[0].header("Call-ID") == second.invites[0].header("Call-ID")
    assert not [request for request in first.requests if request.method == "INFO"]


@pytest.mark.parametrize("mode", ["rtp", "info"])
async def test_incoming_automation_trigger_answers_and_sends_without_phone_selector(lab, mode):
    from .test_automation_originate_ha import PHONE_PASSWORD
    from .support.sip_invite_auth import send_authenticated_invite
    from custom_components.voip_stack.automation_context import CallExecution, current_execution

    await voip_stack._async_register_services(lab.hass)
    received, infos = asyncio.Queue(), []
    peer_audio = udp_socket()

    class CallingPhone(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, raw, address):
            message = sip.parse_message(raw)
            if message.method == "INFO":
                infos.append(message)
                self.transport.sendto(sip.build_uas_response(message, 200, "OK"), address)
            elif message.method == "BYE":
                self.transport.sendto(sip.build_uas_response(message, 200, "OK"), address)
            else:
                received.put_nowait((message, address))
                if message.status_code == 200 and message.header("CSeq").endswith(" INVITE"):
                    headers = [(name, message.header(name)) for name in ("From", "To", "Call-ID")]
                    headers += [("Via", f"SIP/2.0/UDP 127.0.0.1:{self.transport.get_extra_info('sockname')[1]};branch=z9hG4bK-ack"),
                        ("CSeq", message.header("CSeq").replace("INVITE", "ACK"))]
                    self.transport.sendto(sip.build_request("ACK", sip.contact_target_uri(message), headers), address)

    transport, phone = await lab.hass.loop.create_datagram_endpoint(CallingPhone, local_addr=("127.0.0.1", 0))
    signal_port = transport.get_extra_info("sockname")[1]
    listener = ("127.0.0.1", lab.runtime.transport_config["sip_port"])
    uri = f"sip:Wakeup-Caller@127.0.0.1:{listener[1]}"
    call_id = "incoming-automation-dtmf-" + mode
    offer = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Independent phone\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
        f"m=audio {peer_audio.getsockname()[1]} RTP/AVP 96{' 110' if mode == 'rtp' else ''}\r\n"
        "a=rtpmap:96 L16/16000\r\na=ptime:20\r\na=sendrecv\r\n"
        + ("a=rtpmap:110 telephone-event/8000\r\na=fmtp:110 0-15\r\n" if mode == "rtp" else ""))
    headers = [("Via", f"SIP/2.0/UDP 127.0.0.1:{signal_port};branch=z9hG4bK-inbound;rport"),
        ("From", f"<sip:bedroom0@127.0.0.1:{signal_port}>;tag=caller"), ("To", f"<{uri}>"),
        ("Contact", f"<sip:bedroom0@127.0.0.1:{signal_port}>"), ("Call-ID", call_id),
        ("CSeq", "1 INVITE"), ("Content-Type", "application/sdp")]
    binding = None
    try:
        await send_authenticated_invite(transport, listener, sip.build_request("INVITE", uri, headers, offer.encode()),
            received.get, "bedroom0", PHONE_PASSWORD)
        async with asyncio.timeout(2):
            while True:
                progress, _ = await received.get()
                if progress.status_code == 180:
                    break
        session = lab.calls.get_session(call_id)
        assert session.owner == "automation" and session.state == "ringing"
        token = session.token
        binding = current_execution.set(CallExecution(token, {"automation_control": "ha_anchored"}))
        result = await lab.hass.services.async_call("voip_stack", "send_dtmf",
            {"digits": "5#", "duration_ms": 80, "gap_ms": 50}, blocking=True, return_response=True)
        assert result == {"call_id": call_id, "generation": token.generation, "digits": "5#", "sent": 2}
        assert session.state == "in_call" and session.token == token
        if mode == "info":
            assert [request.body for request in infos] == [b"Signal=5\r\nDuration=80\r\n", b"Signal=#\r\nDuration=80\r\n"]
            assert {request.header("Call-ID") for request in infos} == {call_id}
        else:
            assert not infos
            events = []
            while True:
                try:
                    raw = peer_audio.recv(4096)
                except BlockingIOError:
                    break
                packet = rtp.parse_packet(raw)
                if packet.payload_type == 110:
                    events.append(packet)
            final = [packet for packet in events if packet.payload[1] & 128]
            assert [packet.payload[0] for packet in final] == [5] * 3 + [11] * 3
            assert {int.from_bytes(packet.payload[2:4], "big") for packet in final} == {640}
    finally:
        if binding is not None:
            current_execution.reset(binding)
        await lab.calls.terminate_call_wait(call_id, reason="test_done")
        transport.close()
        peer_audio.close()


@pytest.mark.parametrize("rejection", ["generation", "controller", "trigger_call", "ambiguous", "user", "phone"])
async def test_automation_digits_reject_stale_cross_call_or_unauthorized_selection(lab, monkeypatch, hass_read_only_user, rejection):
    from .test_automation_originate_ha import dial
    from custom_components.voip_stack.automation_context import CallExecution, current_execution

    await voip_stack._async_register_services(lab.hass)
    for phone in lab.phones:
        negotiate_phone_events(phone, monkeypatch, 8000)
    context = Context()
    token = await dial(lab, context)
    app = lab.calls.resource_for(token["call_id"], "automation")
    data = {"call_id": token["call_id"], "expected_generation": token["generation"], "digits": "5"}
    binding = None
    expected = ServiceValidationError
    if rejection == "generation":
        data["expected_generation"] += 1
    elif rejection == "controller":
        context = Context()
    elif rejection == "trigger_call":
        other = await dial(lab, Context(), destination="bedroom1")
        binding = current_execution.set(CallExecution(app.session.token, {"automation_control": "ha_anchored"}))
        data["call_id"], data["expected_generation"] = other["call_id"], other["generation"]
    elif rejection == "ambiguous":
        await dial(lab, context, destination="bedroom1")
        data = {"digits": "5"}
    elif rejection == "user":
        from homeassistant.auth.const import GROUP_ID_USER

        await lab.hass.auth.async_update_user(hass_read_only_user, group_ids=[GROUP_ID_USER])
        assert not hass_read_only_user.is_admin
        context = Context(user_id=hass_read_only_user.id)
        expected = Unauthorized
    elif rejection == "phone":
        data["device_id"] = lab.runtime.endpoints.by_username("bedroom1").ha_device_id
    try:
        with pytest.raises(expected):
            await lab.hass.services.async_call("voip_stack", "send_dtmf", data,
                blocking=True, context=context)
        assert app.session.live and not app.media._dtmf_tasks
        for phone in lab.phones:
            while not phone.audio.empty():
                packet = rtp.parse_packet(phone.audio.get_nowait()[0])
                assert packet.payload_type == phone.payload_type
    finally:
        if binding is not None:
            current_execution.reset(binding)


@pytest.mark.parametrize("ending", ["remote_bye", "cancel_action"])
async def test_automation_hangup_or_stop_cancels_digits_and_releases_existing_owner(lab, monkeypatch, ending):
    from .test_automation_originate_ha import dial, assert_call_resources_released

    await voip_stack._async_register_services(lab.hass)
    phone = lab.phones[0]
    negotiate_phone_events(phone, monkeypatch, 8000)
    context = Context()
    token = await dial(lab, context)
    app = lab.calls.resource_for(token["call_id"], "automation")
    media, transport = app.media, app.media.transport
    event_type = media.invite.dtmf_direction.send.payload_type
    action = asyncio.create_task(lab.hass.services.async_call("voip_stack", "send_dtmf",
        {"digits": "12", "duration_ms": 1000, "gap_ms": 1000},
        blocking=True, return_response=True, context=context))
    async with asyncio.timeout(2):
        while True:
            packet = rtp.parse_packet((await phone.audio.get())[0])
            if packet.payload_type == event_type:
                assert packet.payload[0] == 1
                break
    if ending == "remote_bye":
        phone.remote_hangup()
        with pytest.raises(ServiceValidationError, match="call ended"):
            await asyncio.wait_for(action, 2)
    else:
        action.cancel()
        with pytest.raises(asyncio.CancelledError):
            await action
        await asyncio.wait_for(phone.bye.wait(), 2)
    async with asyncio.timeout(2):
        await media._cleanup_done.wait()
    assert transport.is_closing() and not media._dtmf_tasks
    assert_call_resources_released(lab)
    while not phone.audio.empty():
        packet = rtp.parse_packet(phone.audio.get_nowait()[0])
        assert packet.payload_type != event_type or packet.payload[0] != 2


async def test_automation_speech_and_digits_use_one_application_operation(lab, monkeypatch):
    from .test_automation_originate_ha import dial, tts_fixture

    await voip_stack._async_register_services(lab.hass)
    phone = lab.phones[0]
    negotiate_phone_events(phone, monkeypatch, 8000)
    context = Context()
    token = await dial(lab, context)
    app = lab.calls.resource_for(token["call_id"], "automation")
    stream = tts_fixture(monkeypatch, lab, block=True)
    speaking = asyncio.create_task(lab.hass.services.async_call("voip_stack", "tts_say", {
        "tts_entity_id": "tts.test", "message": "Wake up"}, blocking=True, context=context))
    try:
        await asyncio.wait_for(stream.started.wait(), 2)
        with pytest.raises(ServiceValidationError, match="action is already running"):
            await lab.hass.services.async_call("voip_stack", "send_dtmf", {"digits": "5"},
                blocking=True, context=context)
        assert app.session.live and not app.media._dtmf_tasks and not speaking.done()
    finally:
        speaking.cancel()
        await asyncio.gather(speaking, return_exceptions=True)


async def test_native_phone_trigger_sends_from_its_phone_without_taking_over_an_established_relay(lab):
    from custom_components.voip_stack.automation_context import CallExecution, current_execution

    await voip_stack._async_register_services(lab.hass)
    endpoint, session = active_call(lab)
    fmt = sdp.RtpPcmFormat(0, "PCMU", 8000, 1, 20)
    async with audio_path(fmt, events=True) as (relay, _left, right, _observed):
        lab.calls.attach_relay(session.call_id, relay)
        binding = current_execution.set(CallExecution(session.token, {
            "automation_control": "ha_anchored", "device_id": endpoint.ha_device_id}))
        try:
            result = await lab.hass.services.async_call("voip_stack", "send_dtmf", {
                "digits": "5", "duration_ms": 80}, blocking=True, return_response=True)
            assert result["sent"] == 1 and result["call_id"] == session.call_id
            assert session.owner == "sip_bridge" and lab.calls.resource_for(session.call_id, "relay") is relay
            assert rtp.parse_packet(await receive_datagram(right)).payload[0] == 5
        finally:
            current_execution.reset(binding)


@pytest.mark.parametrize("mode", ["rtp", "info"])
async def test_outgoing_automation_wait_receives_the_actual_peer_without_a_second_receiver(lab, monkeypatch, mode):
    from .test_automation_originate_ha import dial
    from custom_components.voip_stack.dtmf import build_telephone_event_payload

    await voip_stack._async_register_services(lab.hass)
    phone = lab.phones[0]
    if mode == "rtp":
        negotiate_phone_events(phone, monkeypatch, 8000)
    context = Context()
    token = await dial(lab, context)
    app = lab.calls.resource_for(token["call_id"], "automation")
    transport = app.media.transport
    waiting = asyncio.create_task(lab.hass.services.async_call("voip_stack", "wait_for_dtmf", {
        "timeout": .2}, blocking=True, return_response=True, context=context))
    async with asyncio.timeout(1):
        while not app.lock.locked():
            await asyncio.sleep(0)
    await asyncio.sleep(0)
    if mode == "rtp":
        # Only the accepted peer's socket sends the event, with negotiated PT.
        fmt = app.media.invite.recv_dtmf_formats[0]
        await lab.hass.loop.sock_sendto(phone.rtp_socket, rtp.build_packet(rtp.RtpPacket(
            fmt.payload_type, 1, 1000, 123, build_telephone_event_payload("8", duration=800, end=True))),
            ("127.0.0.1", app.media.local_rtp_port))
    else:
        request = phone.last_request
        phone.transport.sendto(sip.build_request("INFO", sip.contact_target_uri(request), [
            ("Via", f"SIP/2.0/UDP 127.0.0.1:{phone.transport.get_extra_info('sockname')[1]};branch=z9hG4bK-reply"),
            ("From", request.header("To") + ";tag=bedroom"), ("To", request.header("From")),
            ("Call-ID", request.header("Call-ID")), ("CSeq", "2 INFO"),
            ("Content-Type", "application/dtmf-relay")], b"Signal=8\r\nDuration=100\r\n"), phone.remote)
    assert await waiting == {"status": "received", "digits": "8"}
    assert app.media.transport is transport


async def test_failed_automation_info_is_not_reported_as_success_and_stops_following_digits(lab):
    from .test_automation_originate_ha import dial, assert_call_resources_released

    await voip_stack._async_register_services(lab.hass)
    phone = lab.phones[0]
    infos = []
    previous = phone.datagram_received

    def receive(raw, address):
        request = sip.parse_message(raw)
        if request.method == "INFO":
            infos.append(request)
            phone.respond(request, address, 503)
        else:
            previous(raw, address)

    phone.datagram_received = receive
    context = Context()
    await dial(lab, context)
    with pytest.raises(ServiceValidationError, match="could not send"):
        await lab.hass.services.async_call("voip_stack", "send_dtmf", {"digits": "12"},
            blocking=True, return_response=True, context=context)
    await asyncio.wait_for(phone.bye.wait(), 2)
    assert len(infos) == 1 and infos[0].body == b"Signal=1\r\nDuration=160\r\n"
    assert_call_resources_released(lab)
