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
