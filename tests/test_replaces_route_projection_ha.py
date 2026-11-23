"""Real Replaces handoff retains one route observer for its retained SIP leg."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_managed_phones_ha import custom_integration_path as custom_integration_path
from tests.test_phone_state_ha import phones as phones

pytestmark = pytest.mark.ha


class PacketSink:
    def __init__(self):
        self.packets = []

    def sendto(self, packet, address):
        self.packets.append((packet, address))

    def close(self):
        pass


@pytest.fixture
async def replacement_route(hass, phones, monkeypatch):
    from custom_components.voip_stack import call_projection, call_transfer, device_resolver, esphome_actions, media_renegotiation
    from custom_components.voip_stack.core import sdp, sip
    from custom_components.voip_stack.core.sip_transfer import SipReplaces
    from custom_components.voip_stack.sip_client import SipCallClient, SipDialog
    from custom_components.voip_stack.sip_listener import SipInvite, SipInviteResult
    from custom_components.voip_stack.sip_rtp_bridge import RtpPeer, SipRtpRelay

    calls = phones.calls
    fmt = sdp.RtpPcmFormat(8, "PCMA", 8000, 1, 20)
    alternate = sdp.RtpPcmFormat(0, "PCMU", 8000, 1, 20)
    client = SipCallClient(local_ip="192.0.2.1", local_name="Test", local_sip_port=5060,
                          local_rtp_port=42000, call_id="retained-kitchen-wire")
    client.dialog = SipDialog(target="Kitchen", remote_host="192.0.2.20", remote_sip_port=5060,
        remote_rtp_host="192.0.2.20", remote_rtp_port=41000, local_rtp_port=42000,
        call_id=client.dialog_ids.call_id, local_uri="sip:HA@192.0.2.1", remote_uri="sip:kitchen@192.0.2.20",
        send_format=fmt, recv_format=fmt)
    relay = SipRtpRelay(
        left=RtpPeer("192.0.2.10", 40000, fmt.payload_type, fmt.audio_format,
            rtp_format=fmt, send_rtp_format=fmt, inbound_rtp_formats=(fmt, alternate)),
        right=RtpPeer("192.0.2.20", 41000, fmt.payload_type, fmt.audio_format,
            rtp_format=fmt, send_rtp_format=fmt), left_port=42000, right_port=42002)
    relay.left_transport = PacketSink()
    relay.right_transport = PacketSink()
    with calls.state_operation():
        old = calls.upsert("replaced-office-wire", state="in_call", owner="bridge",
            caller="Office", callee="Kitchen", source_endpoint_id="office", dest_endpoint_id="kitchen")
        watcher = asyncio.create_task(asyncio.Event().wait())
        calls.register_bridge(source_call_id=old.call_id, dest_call_id=client.dialog_ids.call_id,
            client=client, lifecycle_task=watcher, state="in_call", caller="Office", callee="Kitchen")
        calls.claim_endpoint(old.call_id, "kitchen", role="destination")
        calls.attach_relay(old.call_id, relay)
    previous = SipInvite(source_host="192.0.2.10", source_port=5060,
        request_uri=sip.parse_sip_uri("sip:kitchen@192.0.2.1"),
        caller_uri=sip.parse_sip_uri("sip:office@192.0.2.10"), target="Kitchen", caller="Office",
        call_id=old.call_id, cseq="1 INVITE", remote_sdp=b"", send_format=fmt, recv_format=fmt,
        remote_rtp_host="192.0.2.10", remote_rtp_port=40000, identified_source_endpoint_id="office")
    releases = []
    async def acquire(_key):
        return previous, lambda: releases.append(True)
    server = SimpleNamespace(acquire_replacement_dialog=acquire)
    monkeypatch.setattr(call_transfer, "_inbound_server", lambda *_args: (server, "sip:HA@192.0.2.1"))
    monkeypatch.setattr(media_renegotiation, "async_prepare_media_update",
        AsyncMock(return_value=SipInviteResult(200, "OK")))
    devices = [{"device_id": "office-device"}, {"device_id": "kitchen-device"}]
    sink = AsyncMock()
    monkeypatch.setattr(device_resolver, "get_resolver", lambda _hass: SimpleNamespace(
        list_devices=AsyncMock(return_value=devices)))
    monkeypatch.setattr(esphome_actions, "async_call_action", sink)
    monkeypatch.setattr(esphome_actions, "has_action", lambda *_args: True)
    call_projection.publish_media_route(hass, old.call_id, relay.media_route)
    await asyncio.gather(*tuple(task for task in old.tasks if task is not watcher))
    old_callback = relay.on_media_route_changed
    assert callable(old_callback)
    invite = replace(previous, call_id="replacement-office-wire",
        replaces=SipReplaces(old.call_id, "local-tag", "remote-tag"))
    yield SimpleNamespace(calls=calls, old=old, client=client, relay=relay, invite=invite,
        sink=sink, old_callback=old_callback, releases=releases, fmt=fmt, alternate=alternate,
        watcher=watcher)
    await calls.shutdown()
    await client.close()
    await relay.stop()


async def test_replaces_rebinds_exact_successor_and_retained_wire_call_id(hass, replacement_route):
    from custom_components.voip_stack.call_replaces import prepare_replacement
    from custom_components.voip_stack.core import rtp
    from custom_components.voip_stack.endpoint_session import TerminationIntent

    lab = replacement_route
    result = await prepare_replacement(hass, lab.invite, "192.0.2.1")
    assert result.status == 200
    await result.commit()
    await hass.async_block_till_done()
    successor = lab.calls.get_session(lab.invite.call_id)
    assert successor.live and successor.answer_committed
    assert lab.calls.resource_for(successor.call_id, "relay") is lab.relay
    assert lab.relay.on_media_route_changed is not lab.old_callback
    callback = lab.relay.on_media_route_changed
    assert callable(callback)
    assert lab.old.termination_intent.reason == "forwarded"
    await lab.old.terminate(TerminationIntent("forwarded"))
    assert lab.old.terminated.is_set()
    assert lab.releases == [True]
    assert successor.metadata["committed_media_route"] == lab.relay.media_route
    assert "esp_media_routes" not in successor.metadata
    lab.sink.reset_mock()
    lab.old_callback("direct")
    await hass.async_block_till_done()
    lab.sink.assert_not_awaited()

    # Actual RTP inputs switch the production relay's payload relay/converter
    # decision; no test replaces the route callback or media state machine.
    for index, fmt in enumerate((lab.fmt, lab.alternate, lab.fmt)):
        payload = bytes([0xD5 if fmt.encoding == "PCMA" else 0xFF]) * 160
        packet = rtp.build_packet(rtp.RtpPacket(payload_type=fmt.payload_type,
            sequence=index, timestamp=160 * index, ssrc=1234, payload=payload))
        lab.relay.handle_packet("left", packet, ("192.0.2.10", 40000))
        await hass.async_block_till_done()
        assert lab.relay.media_route == ("ha_transcoding" if index == 1 else "direct")
        assert successor.metadata["committed_media_route"] == lab.relay.media_route
        assert lab.relay.on_media_route_changed is callback
    lab.sink.assert_not_awaited()
    assert lab.client.dialog_ids.call_id == "retained-kitchen-wire"
    await successor.terminate(TerminationIntent("test_done"))
    assert lab.relay.on_media_route_changed is None
    callback("ha_transcoding")
    assert not successor.tasks
    assert not lab.calls.sessions and not lab.calls.endpoint_claims_snapshot()


async def test_replaces_route_binding_rolls_back_with_handoff(hass, replacement_route, monkeypatch):
    from custom_components.voip_stack import call_projection
    from custom_components.voip_stack.call_replaces import prepare_replacement

    lab = replacement_route
    publish = call_projection.publish_media_route
    def fail_after_binding(hass, call_id, route):
        publish(hass, call_id, route)
        assert lab.relay.on_media_route_changed is not lab.old_callback
        raise RuntimeError("injected projection handoff failure")
    monkeypatch.setattr(call_projection, "publish_media_route", fail_after_binding)
    result = await prepare_replacement(hass, lab.invite, "192.0.2.1")
    assert result.status == 200
    with pytest.raises(RuntimeError, match="injected projection"):
        await result.commit()
    await result.rollback()
    assert lab.old.live and not lab.watcher.done()
    assert lab.calls.resource_for(lab.old.call_id, "relay") is lab.relay
    assert lab.calls.sip_client_for(lab.client.dialog_ids.call_id) is lab.client
    assert lab.relay.on_media_route_changed is lab.old_callback
    assert lab.calls.get_session(lab.invite.call_id) is None
    monkeypatch.setattr(call_projection, "publish_media_route", publish)
    before = lab.sink.await_count
    lab.relay.left_to_right_passthrough = lab.relay.right_to_left_passthrough = True
    lab.old_callback(lab.relay.media_route)
    await hass.async_block_till_done()
    assert lab.sink.await_count == before == 0
    assert lab.old.metadata["committed_media_route"] == "direct"
    assert lab.calls.endpoint_claims_snapshot() == {lab.old.call_id: {"kitchen": "destination"}}


async def test_replaces_suppresses_queued_old_route_and_stale_generation(hass, replacement_route):
    from custom_components.voip_stack import call_projection
    from custom_components.voip_stack.call_replaces import prepare_replacement
    from custom_components.voip_stack.core import rtp
    from custom_components.voip_stack.endpoint_session import TerminationIntent
    from custom_components.voip_stack.sip_rtp_bridge import SipRtpRelay

    lab = replacement_route
    lab.sink.reset_mock()
    packet = rtp.build_packet(rtp.RtpPacket(payload_type=lab.fmt.payload_type,
        sequence=1, timestamp=160, ssrc=1234, payload=bytes([0xD5]) * 160))
    lab.relay.handle_packet("left", packet, ("192.0.2.10", 40000))
    result = await prepare_replacement(hass, lab.invite, "192.0.2.1")
    try:
        assert result.status == 200
        await result.commit()
        hass.loop.call_soon(lab.old_callback, "ha_transcoding")
        await lab.old.terminate(TerminationIntent("forwarded"))
        await hass.async_block_till_done()
        successor = lab.calls.get_session(lab.invite.call_id)
        lab.sink.assert_not_awaited()
        assert successor.metadata["committed_media_route"] == lab.relay.media_route
        assert lab.client.dialog_ids.call_id == "retained-kitchen-wire"
        stale_callback = lab.relay.on_media_route_changed
        old_generation = successor.generation
        await successor.terminate(TerminationIntent("test_done"))
        reused = lab.calls.upsert(successor.call_id, state="in_call", owner="bridge",
            source_endpoint_id="office", dest_endpoint_id="kitchen")
        fresh = SipRtpRelay(left=lab.relay.left, right=lab.relay.right, left_port=43000, right_port=43002)
        lab.calls.attach_relay(reused.call_id, fresh)
        assert reused.generation > old_generation
        call_projection.publish_media_route(hass, reused.call_id, fresh.media_route)
        await hass.async_block_till_done()
        current_callback = fresh.on_media_route_changed
        before = lab.sink.await_count
        stale_callback("ha_transcoding")
        lab.old_callback("ha_transcoding")
        await hass.async_block_till_done()
        assert fresh.on_media_route_changed is current_callback
        assert lab.sink.await_count == before
        assert reused.metadata["committed_media_route"] == fresh.media_route
        await reused.terminate(TerminationIntent("test_done"))
        assert fresh.on_media_route_changed is None
        assert not lab.calls.sessions and not lab.calls.endpoint_claims_snapshot()
    finally:
        lab.sink.assert_not_awaited()
