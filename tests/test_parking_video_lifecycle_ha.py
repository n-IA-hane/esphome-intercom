"""Slots share the call session's cleanup and arbitrate pickup atomically."""

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.voip_stack.parking_lot import ParkingLot, ParkingError
from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
from custom_components.voip_stack.endpoint_session import TerminationIntent
from .test_sip_phone_transfers_ha import registered_phones as registered_phones
from .test_trunk_calls_ha import trunks as trunks, lab as lab
from .test_voicemail_ha import custom_integrations as custom_integrations

pytestmark = pytest.mark.ha


@pytest.fixture
async def av_transaction_owner(monkeypatch):
    from unittest.mock import AsyncMock
    from custom_components.voip_stack import parking, call_transfer, local_call_media
    from custom_components.voip_stack.sip_rtp_bridge import SipRtpRelay, RtpPeer
    from custom_components.voip_stack.sip_video_relay import SipVideoRtpRelay, VideoRtpPeer
    from custom_components.voip_stack.core.audio_format import AudioFormat
    from custom_components.voip_stack.core.sdp import RtpVideoFormat
    owner = SipEndpointRuntime()
    owner.activate()
    original = owner.upsert("transaction-original", state="in_call", owner="bridge", caller="Caller", callee="Receiver",
        source_endpoint_id="caller", dest_endpoint_id="callee")
    for wire_id, role in ((original.call_id, "caller"), ("transaction-destination", "callee")):
        client = SimpleNamespace(dialog_ids=SimpleNamespace(call_id=wire_id), local_ip="127.0.0.1",
            close=AsyncMock(), terminate=AsyncMock(), on_refer=None)
        owner.add_leg(original.call_id, wire_id, role=role, state="in_call").dialog = client
    owner.set_bridge_link(original.call_id, "transaction-destination")
    audio = AudioFormat(8000, "s16le", 1, 20)
    relay = SipRtpRelay(left=RtpPeer("127.0.0.1", 41000, 96, audio), right=RtpPeer("127.0.0.1", 42000, 96, audio),
        left_port=43000, right_port=43002)
    video = SipVideoRtpRelay(
        left=VideoRtpPeer("127.0.0.1", 44000, 44001, RtpVideoFormat(payload_type=103, encoding="H264")),
        right=VideoRtpPeer("127.0.0.1", 45000, 45001, RtpVideoFormat(payload_type=26, encoding="JPEG")),
        left_port=46000, right_port=46002)
    relay.attach_video_relay(video)
    owner.attach_relay(original.call_id, relay)
    runtime = SimpleNamespace(sip=owner, rtp_port_pool={"used": {43000, 43002}})
    lot = ParkingLot(("701",))
    monkeypatch.setattr(parking, "require_runtime_data", lambda _: runtime)
    monkeypatch.setattr(parking, "call_registry", lambda _: owner)
    monkeypatch.setattr(parking, "get_parking_lot", lambda _: lot)
    monkeypatch.setattr(parking, "parking_settings", lambda _: {})
    monkeypatch.setattr(call_transfer, "_inbound_server", lambda *_: (None, None))
    monkeypatch.setattr(local_call_media.ApplicationCallSource, "from_client", lambda *_args, **_kwargs: object())
    try:
        yield SimpleNamespace(owner=owner, original=original, relay=relay, video=video, lot=lot)
    finally:
        for session in tuple(owner.sessions.values()):
            await session.terminate(TerminationIntent("fixture_cleanup"))


@pytest.mark.parametrize("origin", ["caller", "callee"])
async def test_parking_goal_and_slot_are_released_when_video_rollback_raises(av_transaction_owner, monkeypatch, origin):
    from unittest.mock import AsyncMock
    from custom_components.voip_stack import parking
    state = av_transaction_owner
    update = SimpleNamespace(commit=AsyncMock(side_effect=RuntimeError("cutover failed")),
                             rollback=AsyncMock(side_effect=RuntimeError("restore failed")))
    prepare = AsyncMock(return_value=update)
    monkeypatch.setattr(parking, "_prepare_video_handoff", prepare)
    with pytest.raises(RuntimeError, match="restore failed"):
        await parking.park_established_sip_call(object(), state.original.call_id, origin)
    assert prepare.await_args.args[1] is state.original
    assert set(state.owner.sessions) == {state.original.call_id}
    assert not state.lot.calls and state.original.live
    assert state.owner.resource_for(state.original.call_id, "relay") is state.relay


async def test_irrecoverable_video_restore_terminates_original_not_only_temporary_goal(av_transaction_owner, monkeypatch):
    from unittest.mock import AsyncMock, Mock
    from custom_components.voip_stack import parking, sip_video_relay as vr, bridge_media_updates, sip_bridge, local_media_bridge
    from custom_components.voip_stack.core import sdp
    state = av_transaction_owner
    video = state.video
    video.configure_transcoding(SimpleNamespace(data={}), "actual-owner")
    video.started, video._transcode_slot_claimed = True, True
    video._maintain_nat = Mock()
    alive = set()
    class Worker:
        def __init__(self, side):
            self.side = side
            alive.add(side)
        async def async_close(self): alive.discard(self.side)
    video._transcode = vr._TranscodeGeneration({"left", "right"}, {s: Worker(s) for s in ("left", "right")}, {}, {})
    async def source_update(*_args, **kwargs):
        return SimpleNamespace(candidate=parking._video_peer_direction(video.right,
            sdp.local_direction_for_remote(kwargs["video_direction"])), commit=lambda: True,
            restore=AsyncMock(return_value=False))
    monkeypatch.setattr(bridge_media_updates, "prepare_source_video_update", source_update)
    monkeypatch.setattr(sip_bridge, "dialog_video_rtp_peer", lambda candidate: candidate)
    monkeypatch.setattr(local_media_bridge, "adopt_relay_for_application", Mock(side_effect=RuntimeError("adoption failed")))
    monkeypatch.setattr(video, "_build_transcode_generation", AsyncMock(side_effect=RuntimeError("encoder restart failed")))
    monkeypatch.setattr(vr, "_claim_transcoder_slot", AsyncMock())
    monkeypatch.setattr(vr, "_release_transcoder_slot", AsyncMock())
    requested = Mock(wraps=state.owner.request_termination)
    monkeypatch.setattr(state.owner, "request_termination", requested)
    with pytest.raises(ParkingError, match="could not be restored"):
        await parking.park_established_sip_call(object(), state.original.call_id, "caller")
    assert not state.original.live
    assert state.original.call_id in [call.args[0] for call in requested.call_args_list]
    await asyncio.shield(state.original._termination_task)
    assert not state.owner.sessions and not state.lot.calls and not alive


async def test_obsolete_video_handoff_rollback_does_not_touch_new_owner(av_transaction_owner, monkeypatch):
    from unittest.mock import AsyncMock, Mock
    from custom_components.voip_stack import parking, bridge_media_updates, sip_bridge
    from custom_components.voip_stack.sip_rtp_bridge import SipRtpRelay
    state = av_transaction_owner
    video = state.video
    video.started = True
    video._maintain_nat = Mock()
    prepared = SimpleNamespace(candidate=parking._video_peer_direction(video.left, "inactive"),
        commit=lambda: True, restore=AsyncMock(return_value=False))
    monkeypatch.setattr(bridge_media_updates, "prepare_source_video_update", AsyncMock(return_value=prepared))
    monkeypatch.setattr(sip_bridge, "invite_video_rtp_peer", lambda candidate: candidate)
    update = await parking._prepare_video_handoff(object(), state.original, video,
        parking._video_peer_direction(video.left, "inactive"), parking._video_peer_direction(video.right, "inactive"))
    replacement = SipRtpRelay(left=state.relay.left, right=state.relay.right, left_port=43000, right_port=43002)
    replacement.attach_video_relay(video)
    state.relay.video_relay = None
    state.owner.release_resource(state.original.call_id, f"relay:{state.original.call_id}", value=state.relay)
    state.owner.attach_relay(state.original.call_id, replacement)
    await update.rollback()
    prepared.restore.assert_not_awaited()
    assert state.original.live and state.owner.resource_for(state.original.call_id, "relay") is replacement
    assert not video._staged_peers
    await state.relay.stop()


async def test_cancelled_video_hold_finishes_settlement_and_compensates_sip_and_workers(monkeypatch):
    from unittest.mock import AsyncMock, Mock
    from custom_components.voip_stack import parking, sip_video_relay as vr, bridge_media_updates, sip_bridge
    from custom_components.voip_stack.core import sdp
    left = vr.VideoRtpPeer("127.0.0.1", 41000, 41001, sdp.RtpVideoFormat(payload_type=103, encoding="H264"))
    right = vr.VideoRtpPeer("127.0.0.1", 42000, 42001, sdp.RtpVideoFormat(payload_type=26, encoding="JPEG"))
    video = vr.SipVideoRtpRelay(left=left, right=right, left_port=43000, right_port=43002)
    video.configure_transcoding(SimpleNamespace(data={}), "cancel-hold")
    video.started, video._transcode_slot_claimed = True, True
    video._maintain_nat = Mock()
    entered, release = asyncio.Event(), asyncio.Event()
    alive, peak, offers = set(), {"left": 0, "right": 0}, []

    class Worker:
        def __init__(self, side):
            self.side = side
            alive.add(self)
            peak[side] = max(peak[side], sum(worker.side == side for worker in alive))
        async def async_close(self):
            entered.set()
            await release.wait()
            alive.discard(self)

    async def build(_loop, new_left, new_right, *, directions=None, **_kwargs):
        selected = video.transcode_directions_for(new_left, new_right) if directions is None else set(directions)
        return vr._TranscodeGeneration(selected, {side: Worker(side) for side in selected}, {}, {})

    video._transcode = await build(None, left, right)
    wire = {"direction": "sendrecv"}
    async def source_update(*_args, **kwargs):
        previous = wire["direction"]
        wire["direction"] = kwargs["video_direction"]
        offers.append(wire["direction"])
        async def restore(**_kwargs):
            wire["direction"] = previous
            return True
        return SimpleNamespace(candidate=parking._video_peer_direction(left,
            sdp.local_direction_for_remote(wire["direction"])), commit=lambda: True, restore=restore)

    session = SimpleNamespace(call_id="cancel-hold", generation=7, live=True)
    owner = SimpleNamespace(video_relay=video)
    registry = SimpleNamespace(resource_for=lambda *_: owner, is_generation_current=lambda *_: True,
                               request_termination=Mock())
    monkeypatch.setattr(parking, "call_registry", lambda _: registry)
    monkeypatch.setattr(bridge_media_updates, "prepare_source_video_update", source_update)
    monkeypatch.setattr(sip_bridge, "invite_video_rtp_peer", lambda candidate: candidate)
    monkeypatch.setattr(video, "_build_transcode_generation", build)
    monkeypatch.setattr(vr, "_claim_transcoder_slot", AsyncMock())
    monkeypatch.setattr(vr, "_release_transcoder_slot", AsyncMock())
    try:
        update = await parking._prepare_video_handoff(object(), session, video,
            parking._video_peer_direction(left, "inactive"), parking._video_peer_direction(right, "inactive"))
        committing = asyncio.create_task(update.commit())
        await asyncio.wait_for(entered.wait(), 1)
        committing.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await committing
        await update.rollback()
        assert wire["direction"] == "sendrecv"
        assert video.left.video_format.direction == video.right.video_format.direction == "sendrecv"
        assert set(video._transcode.transcoders) == {"left", "right"}
        assert {worker.side for worker in alive} == {"left", "right"}
        assert video._transcode_slot_claimed and not video._staged_peers
        assert offers == ["inactive", "sendrecv"] and peak == {"left": 1, "right": 1}
    finally:
        release.set()
        await video.stop()
    assert not alive


def test_video_owner_validation_precedes_audio_transport_cutover(monkeypatch):
    from unittest.mock import Mock
    from custom_components.voip_stack import local_media_bridge
    original_protocol = object()
    transport = SimpleNamespace(protocol=original_protocol, is_closing=lambda: False)
    media = SimpleNamespace(adopt_transport=lambda stream: setattr(stream, "protocol", "application"))
    monkeypatch.setattr(local_media_bridge, "LocalCallMedia", lambda *args, **kwargs: media)
    peer = SimpleNamespace(port=41000, rx_ssrc=1, sequence=2, timestamp=3, ssrc=4, clock_rate=8000)
    video = SimpleNamespace(transcoding=False,
        left=SimpleNamespace(video_format=SimpleNamespace(direction="inactive")),
        right=SimpleNamespace(video_format=SimpleNamespace(direction="inactive")),
        reverse_held_sides=Mock(side_effect=RuntimeError("video owner changed")))
    relay = SimpleNamespace(_stop_requested=False, _start_task=None, video_relay=video,
        right=peer, right_encoder=None, right_transport=transport, right_port=42000)
    with pytest.raises(RuntimeError, match="video owner changed"):
        local_media_bridge.adopt_relay_for_application(None, relay, "right", object(), object(), lambda _: None)
    assert transport.protocol is original_protocol
    assert relay.right_transport is transport and relay.video_relay is video


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"video": ("transfer_a", "transfer_b", "transfer_c")}], indirect=True)
@pytest.mark.parametrize("park_origin", ["callee", "caller"])
@pytest.mark.parametrize("pickup_fault", [False, True], ids=["pickup", "rollback-retry"])
@pytest.mark.parametrize("pickup_method", ["sip", "service"])
async def test_established_video_parking_reuses_source_and_resumes_on_another_phone(
    registered_phones, trunks, monkeypatch, tmp_path, park_origin, pickup_fault, pickup_method
):
    from collections import defaultdict
    from custom_components.voip_stack.parking import get_parking_lot
    from custom_components.voip_stack.sip_video_relay import SipVideoRtpRelay
    from custom_components.voip_stack.store import config_entry
    from .test_sip_phone_transfers_ha import assert_h264_color_bars

    runtime = trunks
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(entry, data={**entry.data, "parking": {"enabled": True}})
    captured = defaultdict(list)
    original_start = SipVideoRtpRelay.start

    class CaptureTransport:
        def __init__(self, transport, key):
            self.transport, self.key = transport, key

        def sendto(self, raw, address):
            if len(captured[self.key]) < 512:
                captured[self.key].append(bytes(raw))
            self.transport.sendto(raw, address)

        def __getattr__(self, name):
            return getattr(self.transport, name)

    async def observe_start(relay):
        await original_start(relay)
        for key, transport in tuple(relay._transports.items()):
            if not key[1] and not isinstance(transport, CaptureTransport):
                relay._transports[key] = CaptureTransport(transport, (id(relay), key[0]))

    monkeypatch.setattr(SipVideoRtpRelay, "start", observe_start)
    caller, operator, picker = registered_phones

    async def wait_for(predicate):
        async with asyncio.timeout(6):
            while not predicate():
                for peer in registered_phones:
                    peer.read()
                await asyncio.sleep(.01)

    await asyncio.to_thread(caller.dial, "transfer_b")
    await wait_for(lambda: any(s.state == "in_call" for s in runtime.calls.sessions.values()))
    session = next(iter(runtime.calls.sessions.values()))
    source_id = session.call_id
    relay = runtime.calls.resource_for(source_id, "relay")
    video = relay.video_relay
    assert video is not None
    await wait_for(lambda: video.left_tx_packets > 3 and video.right_tx_packets > 3)
    retained = caller if park_origin == "callee" else operator
    parker_id = "transfer_b" if park_origin == "callee" else "transfer_a"
    source_audio = relay.left_transport if park_origin == "callee" else relay.right_transport
    video_ports = ((video.left_port, video.right_port) if park_origin == "callee"
                   else (video.right_port, video.left_port))
    runtime.runtime.endpoints.update(parker_id, device_id="operator-video-device")
    response = await runtime.hass.services.async_call("voip_stack", "park",
        {"device_id": "operator-video-device"}, blocking=True, return_response=True)
    assert response["slot"] == "701"
    if park_origin == "callee":
        assert response["call_id"] == source_id
    source_id = response["call_id"]
    lot = get_parking_lot(runtime.hass)
    await wait_for(lambda: "701" in lot.calls and not runtime.runtime.endpoints.get(parker_id).active_call_id)
    parked_media = runtime.calls.resource_for(source_id, "relay")
    assert parked_media.transport is source_audio and parked_media.video_relay is video
    assert video.left.video_format.direction == video.right.video_format.direction == "inactive"
    assert all(transport.get_protocol().side == side
               for (side, _rtcp), transport in video._transports.items())
    assert retained.read().count("Call established") == 1
    await asyncio.to_thread(retained.command, "/hold")
    await wait_for(lambda: not parked_media.can_send)
    assert video.left.video_format.direction == video.right.video_format.direction == "inactive"
    await asyncio.to_thread(retained.command, "/resume")
    await wait_for(lambda: parked_media.can_send)
    assert video.left.video_format.direction == video.right.video_format.direction == "inactive"

    if pickup_fault:
        original_set = source_audio.set_protocol
        original_protocol = source_audio.get_protocol()
        failed = []

        def fail_once(protocol):
            if not failed:
                failed.append(True)
                raise RuntimeError("injected AV ownership failure")
            original_set(protocol)

        monkeypatch.setattr(source_audio, "set_protocol", fail_once)
        await asyncio.to_thread(picker.command, "/dial 701")
        await wait_for(lambda: failed and lot.calls["701"].claim is None
                       and not runtime.runtime.endpoints.get("transfer_c").active_call_id)
        assert runtime.calls.resource_for(source_id, "relay") is parked_media
        assert parked_media.transport is source_audio and source_audio.get_protocol() is original_protocol
        assert parked_media.video_relay is video and not video._staged_peers
        assert video.left.video_format.direction == video.right.video_format.direction == "inactive"
        assert retained.read().count("Call established") == 1

    before = {side: len(captured[(id(video), side)]) for side in ("left", "right")}
    if pickup_method == "service":
        import socket
        from custom_components.voip_stack import media_ports
        from custom_components.voip_stack import sip_client, sip_listener
        from custom_components.voip_stack.core import sip, sdp
        from custom_components.voip_stack.local_media_bridge import LocalMediaBridge
        from custom_components.voip_stack.sip_client import SipCallClient
        from custom_components.voip_stack.sip_rtp_bridge import SipRtpRelay
        bound, expected_first, received = [], [], []
        resume_invites = {}
        application = runtime.calls.resource_for(source_id, "parking")
        source_wire_id = (application.source_client.dialog.call_id if application.source_client is not None else source_id)
        native_send = sip_client.send_sip_datagram

        def observe_wire(transport, raw, address):
            result = native_send(transport, raw, address)
            message = sip.parse_message(raw)
            if message.method == "INVITE" and message.header("Call-ID") == source_wire_id:
                resume_invites[message.header("CSeq")] = sdp.offered_video_formats(message.body)[0].direction
            return result

        monkeypatch.setattr(sip_client, "send_sip_datagram", observe_wire)
        monkeypatch.setattr(sip_listener, "send_sip_datagram", observe_wire)
        native_bind, native_invite = media_ports.bind_sip_audio_socket, SipCallClient.invite
        native_prepare, native_receive = LocalMediaBridge.prepare, SipRtpRelay.handle_packet

        def bind_before_offer(port):
            sock = native_bind(port)
            bound.append(sock)
            return sock

        async def invite_after_bind(client, **kwargs):
            assert len(bound) == 1 and bound[0].getsockname()[1] == client.local_rtp_port
            return await native_invite(client, **kwargs)

        async def prepare_with_queued_first(cls, media, remote, **kwargs):
            sock = kwargs["right_socket"]
            assert sock is bound[0]
            async with asyncio.timeout(1):
                while True:
                    try:
                        expected_first.append(sock.recvfrom(2048, socket.MSG_PEEK)[0])
                        break
                    except BlockingIOError:
                        await asyncio.sleep(.001)
            return await native_prepare(media, remote, **kwargs)

        def receive(relay, side, raw, addr):
            if expected_first and side == "right" and len(received) < 2:
                received.append(bytes(raw))
            return native_receive(relay, side, raw, addr)

        monkeypatch.setattr(media_ports, "bind_sip_audio_socket", bind_before_offer)
        monkeypatch.setattr(SipCallClient, "invite", invite_after_bind)
        monkeypatch.setattr(LocalMediaBridge, "prepare", classmethod(prepare_with_queued_first))
        monkeypatch.setattr(SipRtpRelay, "handle_packet", receive)
        runtime.runtime.endpoints.update("transfer_c", device_id="pickup-video-device")
        await runtime.hass.services.async_call("voip_stack", "pickup", {
            "device_id": "pickup-video-device", "slot": "701", "answer_timeout": 3,
        }, blocking=True, return_response=True)
    else:
        await asyncio.to_thread(picker.dial, "701")
    await wait_for(lambda: not lot.calls)
    resumed = runtime.calls.resource_for(source_id, "relay")
    assert resumed.left_transport is source_audio and resumed.video_relay is video
    if pickup_method == "service":
        assert expected_first and received[0] == expected_first[0]
        assert list(resume_invites.values()) == ["sendrecv"]
    assert (video.left_port, video.right_port) == video_ports
    await wait_for(lambda: resumed.left_tx_packets > 20 and resumed.right_tx_packets > 20 and
                   all(len(captured[(id(video), side)]) >= before[side] + 15 for side in before))
    for side in before:
        await asyncio.to_thread(assert_h264_color_bars, captured[(id(video), side)][before[side]:], tmp_path / f"pickup-{side}.h264")
    assert retained.read().count("Call established") == 1
    await asyncio.to_thread(picker.command, "/hold")
    await wait_for(lambda: not resumed.right.can_receive)
    await asyncio.to_thread(picker.command, "/resume")
    await wait_for(lambda: resumed.right.can_receive)
    after_hold = (video.left_tx_packets, video.right_tx_packets)
    await wait_for(lambda: video.left_tx_packets > after_hold[0] + 5 and video.right_tx_packets > after_hold[1] + 5)
    await asyncio.to_thread(picker.hangup)
    await wait_for(lambda: not runtime.calls.sessions)
    assert not lot.calls and not runtime.runtime.rtp_port_pool["used"]


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"video": ("transfer_a", "transfer_b", "transfer_c"),
                                                "h264_profile_level_id": "42801f"}], indirect=True)
@pytest.mark.parametrize("camera_enabled", [False, True])
@pytest.mark.parametrize("pickup_target", ["sip", "browser"])
@pytest.mark.parametrize("source_role", ["uac", "uas"])
async def test_browser_parks_existing_video_and_another_sip_phone_picks_up(
    registered_phones, trunks, monkeypatch, camera_enabled, pickup_target, source_role, tmp_path
):
    from unittest.mock import AsyncMock
    from aiohttp import WSMsgType
    from custom_components.voip_stack import audio_ws_view, video_ws_view, softphone_originate
    from custom_components.voip_stack.const import CONF_SIP_VIDEO, CONF_VIDEO_CAMERA_SEND
    from custom_components.voip_stack.media_ws_session import async_media_websocket_session
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
    from custom_components.voip_stack.parking import get_parking_lot
    from custom_components.voip_stack.store import config_entry
    from custom_components.voip_stack.websocket_owner import MediaWebSocketOwner

    runtime = trunks
    from custom_components.voip_stack.sip_client import SipCallClient
    responses = []
    native_read_response = SipCallClient._read_response

    async def observe_response(client, *args, **kwargs):
        result = await native_read_response(client, *args, **kwargs)
        if result is not None:
            message, _address = result
            if message.status_code == 200 and message.header("CSeq").endswith(" INVITE"):
                responses.append(message.body)
        return result

    monkeypatch.setattr(SipCallClient, "_read_response", observe_response)
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(entry, data={**entry.data, "parking": {"enabled": True}})
    runtime.runtime.transport_config[CONF_SIP_VIDEO] = True
    runtime.runtime.transport_config[CONF_VIDEO_CAMERA_SEND] = camera_enabled
    endpoint = PhoneEndpoint(endpoint_id="av-browser", name="av_browser", device_id="av-browser-device",
        kind=EndpointKind.BROWSER, availability="available", capabilities=frozenset({"audio", "dtmf", "video"}))
    runtime.runtime.endpoints.register(endpoint)
    from custom_components.voip_stack.roster import RosterEntry, dump_roster_json
    runtime.roster.append(RosterEntry(endpoint.name, metadata={"local_ha": True, "endpoint_id": endpoint.endpoint_id}))
    runtime.hass.states.async_set("sensor.voip_phonebook", "ready", {"roster_json": dump_roster_json(runtime.roster)})
    monkeypatch.setattr(softphone_originate, "_ha_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(softphone_originate, "_resolve_target_device", AsyncMock(return_value=None))

    class Browser:
        def __init__(self):
            self.ready, self.frames, self.messages = asyncio.Event(), asyncio.Queue(), asyncio.Queue()
            self.closed = False
        def force_close(self):
            self.closed = True
            self.messages.put_nowait(SimpleNamespace(type=WSMsgType.CLOSE))
        def __aiter__(self): return self
        async def __anext__(self): return await self.messages.get()
        async def send_json(self, _): self.ready.set()
        async def send_bytes(self, value): self.frames.put_nowait(value)

    tasks = []
    async def wait_for(predicate):
        async with asyncio.timeout(6):
            while not predicate():
                for peer in registered_phones:
                    peer.read()
                await asyncio.sleep(.01)

    try:
        if source_role == "uac":
            await runtime.hass.services.async_call("voip_stack", "call", {
                "device_id": endpoint.device_id, "destination": "transfer_a", "media_client_id": "av-browser-client",
                "send_video": True,
            }, blocking=True)
        else:
            await asyncio.to_thread(registered_phones[0].command,
                f'/dial sip:av_browser@127.0.0.1:{runtime.runtime.transport_config["sip_port"]}')
            await wait_for(lambda: runtime.runtime.endpoints.get(endpoint.endpoint_id).active_call_id)
            await runtime.hass.services.async_call("voip_stack", "answer", {
                "device_id": endpoint.device_id, "media_client_id": "av-browser-client", "send_video": True,
            }, blocking=True)
        call_id = runtime.runtime.endpoints.get(endpoint.endpoint_id).active_call_id
        client = runtime.calls.sip_client_for(call_id)
        from custom_components.voip_stack.runtime_data import sip_endpoint_manager
        def source_dialog():
            return client.dialog if client is not None else sip_endpoint_manager(runtime.hass).invite_for_call(call_id)
        await wait_for(lambda: source_dialog() is not None and runtime.calls.get_session(call_id).state == "in_call")
        if source_dialog().video_format is None:
            (tmp_path / "peer-output.txt").write_text(registered_phones[0].read())
            for index, body in enumerate(responses):
                (tmp_path / f"peer-answer-{index}.sdp").write_bytes(body)
        assert source_dialog().video_format is not None
        from custom_components.voip_stack.websocket_api import _ha_softphone_state
        assert _ha_softphone_state(runtime.hass, endpoint.endpoint_id)["parking"]["video_supported"] is True
        original_wire_id = source_dialog().call_id
        audio = audio_ws_view._active_softphone_media_session(runtime.hass, endpoint.endpoint_id)
        video = video_ws_view._active_video_session(runtime.hass, endpoint.endpoint_id)
        assert video is not None
        if client is not None:
            client.audio_rtp_socket = None
        video_ws_view._detach_video_socket(runtime.hass, video)
        audio_browser, video_browser = Browser(), Browser()
        tasks.append(asyncio.create_task(audio_ws_view._run_audio_session(runtime.hass, audio_browser, audio,
            endpoint_id=endpoint.endpoint_id)))

        async def run_video():
            owner = MediaWebSocketOwner(websocket=video_browser, client_id="av-browser-client")
            async with async_media_websocket_session(runtime.runtime.media, runtime.calls, call_id,
                    endpoint.endpoint_id, owner, channel="video", timeout=2, shutdown_event=None,
                    pin_client_identity=True, local_bridge=None, publish_state=lambda: None):
                await video_ws_view._run_video_session(runtime.hass, video_browser, video, endpoint_id=endpoint.endpoint_id)
        tasks.append(asyncio.create_task(run_video()))
        await asyncio.wait_for(audio_browser.frames.get(), 2)
        assert len(await asyncio.wait_for(video_browser.frames.get(), 3)) > 6
        result = await runtime.hass.services.async_call("voip_stack", "park", {"device_id": endpoint.device_id},
                                                       blocking=True, return_response=True)
        assert result["call_id"] == call_id and result["slot"] == "701"
        assert result["generation"] == runtime.calls.get_session(call_id).generation
        await asyncio.wait_for(asyncio.gather(*tasks), 3)
        assert audio_browser.closed and video_browser.closed
        application = runtime.calls.resource_for(call_id, "parking")
        video_relay = application.media.video_relay
        assert video_relay is not None and not video_relay.transcoding
        assert video_relay.left.video_format.direction == video_relay.right.video_format.direction == "inactive"
        assert source_dialog().call_id == original_wire_id
        assert client is None or not client._closed
        if pickup_target == "browser":
            if not camera_enabled:
                from homeassistant.exceptions import ServiceValidationError
                native_transition = runtime.calls.transition
                old_media = application.media
                old_ports = set(runtime.runtime.rtp_port_pool["used"])
                rejected = []

                def reject_answer_claim(*args, **kwargs):
                    if kwargs.get("owner") == "ha_softphone" and kwargs.get("state") == "in_call" and not rejected:
                        rejected.append(True)
                        return None
                    return native_transition(*args, **kwargs)

                with monkeypatch.context() as fault:
                    fault.setattr(runtime.calls, "transition", reject_answer_claim)
                    with pytest.raises(ServiceValidationError, match="changed before commit"):
                        await runtime.hass.services.async_call("voip_stack", "call", {
                            "device_id": endpoint.device_id, "destination": "701", "media_client_id": "av-browser-client",
                        }, blocking=True)
                assert rejected and application.media is old_media and not old_media.closed.is_set()
                assert old_media.video_relay is video_relay and not video_relay.stopping
                assert runtime.calls.get_session(call_id).owner == "parking"
                assert get_parking_lot(runtime.hass).calls["701"].claim is None
                assert runtime.runtime.rtp_port_pool["used"] == old_ports
                assert source_dialog().call_id == original_wire_id
            await runtime.hass.services.async_call("voip_stack", "call", {
                "device_id": endpoint.device_id, "destination": "701", "media_client_id": "av-browser-client",
                "send_video": True,
            }, blocking=True)
            assert not get_parking_lot(runtime.hass).calls
            audio = audio_ws_view._active_softphone_media_session(runtime.hass, endpoint.endpoint_id)
            video = video_ws_view._active_video_session(runtime.hass, endpoint.endpoint_id)
            assert video is not None and video.local_direction == ("sendrecv" if camera_enabled else "recvonly")
            if client is not None:
                assert video.request_remote_keyframe == client.request_video_keyframe
                info_responses = []
                native_request = client._send_in_dialog_request

                async def observe_info(method, **kwargs):
                    response = await native_request(method, **kwargs)
                    if method == "INFO":
                        info_responses.append(response)
                    return response

                monkeypatch.setattr(client, "_send_in_dialog_request", observe_info)
                accepted = await video.request_remote_keyframe()
                response = info_responses[-1]
                # Baresip4.6 accepts only DTMF or empty INFO (call.c2008);
                # unsupported legacy media-control receives488 on this dialog.
                assert response is not None and response.status_code in (200, 488)
                assert accepted is (response.status_code == 200)
                assert response.header("Call-ID") == original_wire_id
                assert info_responses[-1].header("CSeq").endswith(" INFO")
            video_ws_view._detach_video_socket(runtime.hass, video)
            audio_browser, video_browser = Browser(), Browser()
            tasks.append(asyncio.create_task(audio_ws_view._run_audio_session(runtime.hass, audio_browser, audio,
                endpoint_id=endpoint.endpoint_id)))
            tasks.append(asyncio.create_task(run_video()))
            await asyncio.wait_for(audio_browser.frames.get(), 2)
            # Baresip 4.6 avcodec_keyint is seconds (gop_size = keyint * fps).
            # The existing peer fixture uses a five-second GOP.
            assert len(await asyncio.wait_for(video_browser.frames.get(), 6)) > 6
            assert source_dialog().call_id == original_wire_id
            await runtime.hass.services.async_call("voip_stack", "hangup", {"device_id": endpoint.device_id}, blocking=True)
            await wait_for(lambda: not runtime.calls.sessions)
            assert not runtime.runtime.rtp_port_pool["used"]
            return
        await asyncio.to_thread(registered_phones[1].dial, "701")
        await wait_for(lambda: not get_parking_lot(runtime.hass).calls)
        relay = runtime.calls.resource_for(call_id, "relay")
        assert relay.video_relay is video_relay
        await wait_for(lambda: relay.left_tx_packets > 20 and relay.right_tx_packets > 20
                       and video_relay.right_tx_packets > 10
                       and (not camera_enabled or video_relay.left_tx_packets > 10))
        if not camera_enabled:
            assert video_relay.left_tx_packets == 0
        assert source_dialog().call_id == original_wire_id and registered_phones[0].read().count("Call established") == 1
        await asyncio.to_thread(registered_phones[1].hangup)
        await wait_for(lambda: not runtime.calls.sessions)
        assert not runtime.runtime.rtp_port_pool["used"]
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_local_browser_video_snapshot_does_not_claim_sip_video_parking(trunks):
    from custom_components.voip_stack.local_softphone_runtime import (
        async_setup_local_softphone_bridge, async_shutdown_local_softphone_bridge, start_local_softphone_call,
    )
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
    from custom_components.voip_stack.websocket_api import _ha_softphone_state
    runtime = trunks
    for name in ("video-local-a", "video-local-b"):
        runtime.runtime.endpoints.register(PhoneEndpoint(endpoint_id=name, name=name,
            device_id="device-" + name, kind=EndpointKind.BROWSER, availability="available",
            capabilities=frozenset({"audio", "dtmf", "video"})))
    bridge = async_setup_local_softphone_bridge(runtime.hass)
    call = start_local_softphone_call(runtime.hass, "video-local-a", "video-local-b",
        caller_owner_id="owner-a", request_video=True, enable_caller_video_send=True)
    try:
        bridge.answer(call.call_id, "video-local-b", "owner-b", enable_video_send=True)
        snapshot = _ha_softphone_state(runtime.hass, "video-local-a")
        assert snapshot["video_active"] is True
        assert snapshot["parking"]["video_supported"] is False
        assert runtime.calls.get_session(call.call_id).owner == "local_bridge"
    finally:
        await runtime.calls.terminate_call_wait(call.call_id, reason="fixture_cleanup")
        async_shutdown_local_softphone_bridge(runtime.hass)


@pytest.mark.fault
@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"video": ("transfer_a", "transfer_b", "transfer_c"),
                                                "manual": "transfer_c"}], indirect=True)
@pytest.mark.parametrize("failure", ["reject", "cancel"])
async def test_video_pickup_before_answer_failure_preserves_park_and_retry(registered_phones, trunks, failure):
    from custom_components.voip_stack.parking import get_parking_lot
    from custom_components.voip_stack.store import config_entry
    runtime = trunks
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(entry, data={**entry.data, "parking": {"enabled": True}})
    caller, operator, picker = registered_phones

    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                for peer in registered_phones:
                    peer.read()
                await asyncio.sleep(.01)

    await asyncio.to_thread(caller.dial, "transfer_b")
    await wait_for(lambda: any(s.state == "in_call" for s in runtime.calls.sessions.values()))
    session = next(iter(runtime.calls.sessions.values()))
    runtime.runtime.endpoints.update("transfer_b", device_id="parking-operator")
    await runtime.hass.services.async_call("voip_stack", "park", {"device_id": "parking-operator"},
                                         blocking=True, return_response=True)
    lot = get_parking_lot(runtime.hass)
    media = runtime.calls.resource_for(session.call_id, "relay")
    video, audio = media.video_relay, media.transport
    ports = set(runtime.runtime.rtp_port_pool["used"])
    runtime.runtime.endpoints.update("transfer_c", device_id="parking-picker")
    pending = asyncio.create_task(runtime.hass.services.async_call("voip_stack", "pickup", {
        "device_id": "parking-picker", "slot": "701", "answer_timeout": 8,
    }, blocking=True, return_response=True))
    try:
        await wait_for(lambda: any(leg.endpoint_id == "transfer_c" and leg.dialog is not None
            and leg.dialog.last_sip_status_code == 180 for leg in session.legs.values()))
        assert runtime.calls.resource_for(session.call_id, "relay") is media
        if failure == "cancel":
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        else:
            await asyncio.to_thread(picker.command, "/hangup")
            with pytest.raises(ParkingError, match="did not answer"):
                await pending
        assert session.live and session.owner == "parking" and session.state == "in_call"
        assert lot.calls["701"].claim is None
        assert runtime.calls.resource_for(session.call_id, "relay") is media
        assert media.transport is audio and not audio.is_closing()
        assert media.video_relay is video and not video.stopping and not video._staged_peers
        assert video.left.video_format.direction == video.right.video_format.direction == "inactive"
        assert runtime.runtime.rtp_port_pool["used"] == ports
        await asyncio.to_thread(picker.dial, "701")
        await wait_for(lambda: not lot.calls)
        relay = runtime.calls.resource_for(session.call_id, "relay")
        await wait_for(lambda: relay.left_tx_packets > 20 and relay.right_tx_packets > 20
                       and video.left_tx_packets > 5 and video.right_tx_packets > 5)
        await asyncio.to_thread(picker.hangup)
        await wait_for(lambda: not runtime.calls.sessions)
        assert not runtime.runtime.rtp_port_pool["used"]
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.fault
@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{}, {"video": ("transfer_a", "transfer_b")}], indirect=True)
@pytest.mark.parametrize(
    "boundary,via_service",
    [("leg", True), ("leg", False), ("relay", True)],
    ids=["leg-service", "leg-refer", "relay-service"],
)
async def test_caller_bye_owns_resources_after_parking_close(
    registered_phones, trunks, monkeypatch, boundary, via_service
):
    from custom_components.voip_stack.endpoint_session import CallLeg, EndpointCallSession
    from custom_components.voip_stack.parking import get_parking_lot
    from custom_components.voip_stack.sip_rtp_bridge import SipRtpRelay
    from custom_components.voip_stack.store import config_entry

    runtime = trunks
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(
        entry, data={**entry.data, "parking": {"enabled": True}}
    )
    caller, operator, _picker = registered_phones

    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(0.01)

    await asyncio.to_thread(caller.dial, "transfer_b")
    await wait_for(
        lambda: any(s.state == "in_call" for s in runtime.calls.sessions.values())
    )
    session = next(iter(runtime.calls.sessions.values()))
    leg = session.legs[runtime.calls.bridge_link_for(session.call_id)]
    relay = runtime.calls.resource_for(session.call_id, "relay")
    retained_transport = relay.left_transport
    closed, resume, terminating, cleanup = (asyncio.Event() for _ in range(4))
    native_close = CallLeg.close
    native_stop = SipRtpRelay.stop
    native_cleanup = EndpointCallSession._run_termination
    paused_task = None
    stop_claimed = False
    service_task = None

    async def pause_after_close():
        nonlocal paused_task
        paused_task = asyncio.current_task()
        closed.set()
        await resume.wait()

    async def close_leg(current, reason):
        await native_close(current, reason)
        if (
            boundary == "leg"
            and current is leg
            and asyncio.current_task().get_name().startswith("parking-retire-")
        ):
            await pause_after_close()

    async def stop_relay(current):
        nonlocal stop_claimed
        pause = boundary == "relay" and current is relay and not stop_claimed
        stop_claimed |= pause
        await native_stop(current)
        if pause:
            await pause_after_close()

    async def terminate(current, intent):
        if current is session:
            terminating.set()
            await cleanup.wait()
        return await native_cleanup(current, intent)

    monkeypatch.setattr(CallLeg, "close", close_leg)
    monkeypatch.setattr(SipRtpRelay, "stop", stop_relay)
    monkeypatch.setattr(EndpointCallSession, "_run_termination", terminate)
    try:
        if via_service:
            runtime.runtime.endpoints.update("transfer_b", device_id="operator-device")
            service_task = asyncio.create_task(
                runtime.hass.services.async_call(
                    "voip_stack", "park", {"device_id": "operator-device"},
                    blocking=True, return_response=True,
                )
            )
        else:
            await asyncio.to_thread(
                operator.command,
                f'/transfer sip:700@127.0.0.1:{runtime.runtime.transport_config["sip_port"]}',
            )
        await asyncio.wait_for(closed.wait(), 5)
        lot = get_parking_lot(runtime.hass)
        assert lot.calls["701"].session is session
        assert runtime.calls.resource_for(session.call_id, "relay").transport is retained_transport
        assert caller.read().count("Call established") == 1

        # BYE claims termination before its cleanup task can drain resources.
        # Resume the already-completed close in that exact scheduling window.
        await asyncio.to_thread(caller.hangup)
        await asyncio.wait_for(terminating.wait(), 5)
        assert not session.live
        with pytest.raises(RuntimeError, match="cleanup has started"):
            session.ensure_transferable()
        resume.set()
        result = (await asyncio.gather(paused_task, return_exceptions=True))[0]
        assert not isinstance(result, BaseException), repr(result)
        retired = leg if boundary == "leg" else relay
        assert any(resource.value is retired for resource in session.resources)
        if service_task is not None:
            response = await service_task
            assert response == {
                "call_id": session.call_id, "slot": "701", "generation": session.generation,
            }
    finally:
        resume.set()
        cleanup.set()
        if session._termination_task is not None:
            result = await asyncio.wait_for(asyncio.shield(session._termination_task), 5)
            assert not result.errors
        if paused_task is not None:
            await asyncio.gather(paused_task, return_exceptions=True)
        if service_task is not None:
            await asyncio.gather(service_task, return_exceptions=True)
        assert not runtime.calls.sessions
        assert not runtime.runtime.rtp_port_pool["used"]
        assert not get_parking_lot(runtime.hass).calls
        assert not session.endpoint_claims




@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"video": ("transfer_a", "transfer_b", "transfer_c")}], indirect=True)
@pytest.mark.parametrize("unavailable", ["known_audio_only", "video_disabled"])
async def test_service_video_pickup_rejects_unavailable_video_and_retries_same_owner(
    registered_phones, trunks, unavailable,
):
    from custom_components.voip_stack.const import CONF_SIP_VIDEO
    from custom_components.voip_stack.endpoint_routing import assemble_directory_entries
    from custom_components.voip_stack.parking import get_parking_lot
    from custom_components.voip_stack.store import config_entry
    runtime = trunks
    caller, _operator, _picker = registered_phones
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(entry, data={**entry.data, "parking": {"enabled": True}})

    async def wait_for(predicate):
        async with asyncio.timeout(6):
            while not predicate():
                for peer in registered_phones:
                    peer.read()
                await asyncio.sleep(.01)

    await asyncio.to_thread(caller.dial, "transfer_b")
    await wait_for(lambda: any(s.state == "in_call" for s in runtime.calls.sessions.values()))
    session = next(iter(runtime.calls.sessions.values()))
    runtime.runtime.endpoints.update("transfer_b", device_id="negative-video-operator")
    await runtime.hass.services.async_call("voip_stack", "park", {"device_id": "negative-video-operator"},
        blocking=True, return_response=True)
    lot = get_parking_lot(runtime.hass)
    media = runtime.calls.resource_for(session.call_id, "relay")
    video, audio = media.video_relay, media.transport
    ports = set(runtime.runtime.rtp_port_pool["used"])
    fds = {key: transport.get_extra_info("socket").fileno() for key, transport in video._transports.items()}
    runtime.runtime.endpoints.update("transfer_c", device_id="negative-video-picker")
    if unavailable == "known_audio_only":
        runtime.runtime.endpoints.update("transfer_c", capabilities=frozenset({"audio", "dtmf"}))
        registrar = runtime.calls.component("registrar")
        runtime.roster[:] = assemble_directory_entries(runtime.hass, [], registrar.registered_roster_entries())
    else:
        runtime.runtime.transport_config[CONF_SIP_VIDEO] = False
    with pytest.raises(ParkingError, match="did not answer"):
        await runtime.hass.services.async_call("voip_stack", "pickup", {
            "device_id": "negative-video-picker", "slot": "701", "answer_timeout": 3,
        }, blocking=True)
    assert lot.calls["701"].claim is None
    assert runtime.calls.resource_for(session.call_id, "relay") is media
    assert media.video_relay is video and media.transport is audio
    assert set(runtime.runtime.rtp_port_pool["used"]) == ports
    assert {key: transport.get_extra_info("socket").fileno()
        for key, transport in video._transports.items()} == fds
    assert not runtime.runtime.endpoints.get("transfer_c").active_call_id
    runtime.runtime.transport_config[CONF_SIP_VIDEO] = True
    runtime.runtime.endpoints.update("transfer_c", capabilities=frozenset())
    registrar = runtime.calls.component("registrar")
    runtime.roster[:] = assemble_directory_entries(runtime.hass, [], registrar.registered_roster_entries())
    await runtime.hass.services.async_call("voip_stack", "pickup", {
        "device_id": "negative-video-picker", "slot": "701", "answer_timeout": 3,
    }, blocking=True)
    await wait_for(lambda: not lot.calls)
    resumed = runtime.calls.resource_for(session.call_id, "relay")
    assert resumed.video_relay is video
    await wait_for(lambda: video.left_tx_packets > 3 and video.right_tx_packets > 3)
    await asyncio.to_thread(caller.hangup)
    await wait_for(lambda: not runtime.calls.sessions)
    assert not runtime.runtime.rtp_port_pool["used"]
