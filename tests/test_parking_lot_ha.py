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
        def __init__(self, side): self.side = side; alive.add(side)
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
@pytest.mark.parametrize("registered_phones", [{"video": ("transfer_a", "transfer_b", "transfer_c")}], indirect=True)
@pytest.mark.parametrize("camera_enabled", [False, True])
@pytest.mark.parametrize("pickup_target", ["sip", "browser"])
@pytest.mark.parametrize("source_role", ["uac", "uas"])
async def test_browser_parks_existing_video_and_another_sip_phone_picks_up(
    registered_phones, trunks, monkeypatch, camera_enabled, pickup_target, source_role
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
@pytest.mark.parametrize("hangup_side", [0, 1])
async def test_real_sip_parking_pickup_keeps_audio_and_releases_resources(registered_phones, trunks, tmp_path, hangup_side):
    import math
    import struct
    import wave
    from custom_components.voip_stack.store import config_entry
    from custom_components.voip_stack.parking import get_parking_lot

    runtime = trunks
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(entry, data={**entry.data, "parking": {"enabled": True}})
    caller, picker, _other = registered_phones
    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(0.01)
    await asyncio.to_thread(caller.dial, "700")
    lot = get_parking_lot(runtime.hass)
    assert list(lot.calls) == ["701"]
    parked = lot.calls["701"]
    source_id = parked.session.call_id
    media = runtime.calls.resource_for(source_id, "relay")
    source_transport = media.transport
    await asyncio.to_thread(picker.dial, "701")
    await wait_for(lambda: not lot.calls)
    relay = runtime.calls.resource_for(source_id, "relay")
    assert relay.left_transport is source_transport
    assert len(runtime.calls.sessions) == 1
    assert runtime.runtime.rtp_port_pool['used'] == {relay.left_port, relay.right_port}
    await wait_for(lambda: relay.left_tx_packets >= 50 and relay.right_tx_packets >= 50)
    if hangup_side == 1:
        left_contract = relay.left
        await asyncio.to_thread(picker.command, "/hold")
        await wait_for(lambda: not relay.right.can_receive)
        assert relay.left is left_contract
        await asyncio.to_thread(picker.command, "/resume")
        await wait_for(lambda: relay.right.can_receive)
        left_count, right_count = relay.left_tx_packets, relay.right_tx_packets
        await wait_for(lambda: relay.left_tx_packets >= left_count + 50 and relay.right_tx_packets >= right_count + 50)
    await asyncio.to_thread(registered_phones[hangup_side].hangup)
    await wait_for(lambda: not runtime.calls.sessions)
    assert not runtime.runtime.rtp_port_pool['used']
    for peer in registered_phones:
        await asyncio.to_thread(peer.close)
    for name, expected in (("transfer_a", 550), ("transfer_b", 450)):
        found = False
        for path in (tmp_path/name).glob('*-dec.wav'):
            with wave.open(str(path),'rb') as recording:
                rate=recording.getframerate()
                samples=struct.unpack(f'<{recording.getnframes()}h',recording.readframes(recording.getnframes()))[-1600:]
            if len(samples)<800:
                continue
            energies={frequency: abs(sum(value*complex(math.cos(2*math.pi*frequency*index/rate),math.sin(2*math.pi*frequency*index/rate)) for index,value in enumerate(samples))) for frequency in (450,550)}
            found |= energies[expected] > energies[1000-expected]*5 and energies[expected]>10000
        assert found, f'{name} did not receive the opposite phone audio after pickup'


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{}, {"manual": "transfer_c"}], indirect=True)
async def test_parking_timeout_uses_existing_return_route(registered_phones, trunks, request):
    from custom_components.voip_stack.store import config_entry
    from custom_components.voip_stack.parking import get_parking_lot

    runtime = trunks
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(entry, data={**entry.data, "parking": {
        "enabled": True, "timeout": 1, "return_timeout": 1,
        "return_destination": "transfer_c",
    }})
    caller, _picker, destination = registered_phones
    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(0.01)
    await asyncio.to_thread(caller.dial, "700")
    lot = get_parking_lot(runtime.hass)
    await wait_for(lambda: "Incoming call" in destination.read() or "Call established" in destination.read())
    await wait_for(lambda: not lot.calls)
    if "manual" in request.node.callspec.params["registered_phones"]:
        await wait_for(lambda: not runtime.calls.sessions)
        assert "Call established" not in destination.read()
    else:
        await asyncio.to_thread(destination.wait_for, "Call established", 3)
        await wait_for(lambda: any(s.owner == "bridge" and s.state == "in_call" for s in runtime.calls.sessions.values()))
        session = next(iter(runtime.calls.sessions.values()))
        relay = runtime.calls.resource_for(session.call_id, "relay")
        await wait_for(lambda: relay.left_tx_packets >= 20 and relay.right_tx_packets >= 20)
        assert runtime.calls.resource_for(session.call_id, "parking") is None
        # Returning a call must retire the old application so another park
        # acquires a fresh slot and controller on the same live dialog.
        await asyncio.to_thread(destination.command,
            f'/transfer sip:700@127.0.0.1:{runtime.runtime.transport_config["sip_port"]}')
        await wait_for(lambda: "701" in lot.calls)
        assert lot.calls["701"].session is session
        assert runtime.calls.resource_for(session.call_id, "parking") is not None
        await asyncio.to_thread(caller.hangup)
        await wait_for(lambda: not runtime.calls.sessions)
        assert not lot.calls
    assert not runtime.runtime.rtp_port_pool['used']


async def test_slot_contention_rollback_cleanup_and_reuse():
    owner = SipEndpointRuntime()
    owner.activate()
    lot = ParkingLot(("701",))
    first = owner.upsert("first", state="in_call", owner="parking")
    second = owner.upsert("second", state="in_call", owner="parking")
    slot = lot.reserve(first)
    assert lot.reserve(first) is slot
    with pytest.raises(ParkingError, match="full"):
        lot.reserve(second)
    claim = lot.claim("701", "pickup-a")
    with pytest.raises(ParkingError, match="already"):
        lot.claim("701", "pickup-b")
    claim.rollback()
    successor = lot.claim("701", "pickup-b")
    claim.rollback()
    assert successor.current
    successor.commit()
    replacement = lot.reserve(second)
    await first.terminate(TerminationIntent("remote_hangup"))
    assert lot.calls["701"] is replacement
    with pytest.raises(ParkingError):
        successor.commit()
    await lot.close()
    assert not lot.calls
    await owner.shutdown()


async def test_timeout_and_pickup_cannot_both_control_the_call():
    owner = SipEndpointRuntime()
    owner.activate()
    session = owner.upsert("call", state="in_call", owner="parking")
    lot = ParkingLot(("701",))
    parked = lot.reserve(session)
    expired = asyncio.Event()
    release = asyncio.Event()
    async def on_return(claim):
        expired.set()
        await release.wait()
        claim.commit()
    lot.arm_return(parked, 0.01, on_return)
    await asyncio.wait_for(expired.wait(), 1)
    with pytest.raises(ParkingError, match="already"):
        lot.claim("701", "late-pickup")
    release.set()
    await parked.timer
    assert not lot.calls
    assert session.live
    await owner.shutdown()


async def test_remote_hangup_revokes_pending_pickup_and_owned_timer():
    owner = SipEndpointRuntime()
    owner.activate()
    session = owner.upsert("call", state="in_call", owner="parking")
    lot = ParkingLot(("701",))
    parked = lot.reserve(session)
    async def unexpected(_claim):
        pytest.fail("A terminated call returned from parking")
    lot.arm_return(parked, 60, unexpected)
    claim = lot.claim("701", "pickup")
    await session.terminate(TerminationIntent("remote_hangup"))
    assert not lot.calls and parked.timer.done()
    with pytest.raises(ParkingError):
        claim.commit()
    claim.rollback()
    await owner.shutdown()


async def test_failed_pickup_after_deadline_still_returns_the_parked_call():
    owner = SipEndpointRuntime()
    owner.activate()
    session = owner.upsert("call", state="in_call", owner="parking")
    lot = ParkingLot(("701",))
    parked = lot.reserve(session)
    returned = asyncio.Event()
    async def on_return(claim):
        claim.commit()
        returned.set()
    lot.arm_return(parked, 0.01, on_return)
    claim = lot.claim("701", "pickup")
    await asyncio.sleep(0.02)
    assert not returned.is_set() and claim.current
    claim.rollback()
    await asyncio.wait_for(returned.wait(), 1)
    await parked.timer
    assert not lot.calls
    await owner.shutdown()


@pytest.mark.parametrize("hangup_side", ["caller", "pickup"])
async def test_joined_inbound_dialog_uses_parent_cleanup(monkeypatch, hangup_side):
    from custom_components.voip_stack import endpoint_termination
    from custom_components.voip_stack.endpoint_session import SipTerminationDisposition

    owner = SipEndpointRuntime()
    owner.activate()
    dialogs = {"caller", "pickup"}
    signaled = []
    async def bye(identity):
        if identity not in dialogs:
            return False
        dialogs.remove(identity)
        signaled.append(identity)
        return True
    server = SimpleNamespace(dialog_key=lambda identity: identity if identity in dialogs else None,
                             async_send_bye=bye)
    async def signal(current, intent):
        if intent.sip_disposition != SipTerminationDisposition.NONE:
            await bye(current)
    owner.bind_termination_signaler(signal)
    session = owner.upsert("caller", state="in_call", owner="bridge")
    leg = owner.attach_inbound_dialog("caller", "pickup", server, endpoint_id="desk",
                                     expected_generation=session.generation)
    assert owner.resolve_session_id("pickup") == "caller"
    assert not session.named_tasks and leg.dialog is None
    assert len(owner.sessions) == 1
    monkeypatch.setattr(endpoint_termination, "call_registry", lambda _h: owner)
    monkeypatch.setattr(endpoint_termination, "call_runtime_artifacts", lambda _h: owner)
    dialogs.remove(hangup_side)  # The native listener has accepted the remote BYE.
    await endpoint_termination.EndpointTerminationHandler(None).handle(hangup_side)
    assert not dialogs and not owner.sessions and not owner.leg_index
    assert signaled == ["pickup" if hangup_side == "caller" else "caller"]
    await owner.shutdown()


async def test_retired_inbound_leg_cannot_terminate_surviving_call(monkeypatch):
    from custom_components.voip_stack import endpoint_termination
    from custom_components.voip_stack.endpoint_session import LegPhase

    owner = SipEndpointRuntime()
    owner.activate()
    session = owner.upsert("caller", state="in_call", owner="bridge")
    async def bye(_identity):
        return True
    server = SimpleNamespace(dialog_key=lambda identity: identity, async_send_bye=bye)
    leg = owner.attach_inbound_dialog("caller", "retired", server, endpoint_id="desk",
                                     expected_generation=session.generation)
    leg.phase = LegPhase.CLOSING
    monkeypatch.setattr(endpoint_termination, "call_registry", lambda _h: owner)
    monkeypatch.setattr(endpoint_termination, "call_runtime_artifacts", lambda _h: owner)
    await endpoint_termination.EndpointTerminationHandler(None).handle("retired")
    assert session.live and owner.sessions["caller"] is session
    await owner.shutdown()


async def test_inbound_dialog_rejects_unknown_stale_and_already_owned_identity():
    owner = SipEndpointRuntime()
    owner.activate()
    session = owner.upsert("caller", state="in_call", owner="bridge")
    async def bye(_identity):
        return True
    server = SimpleNamespace(dialog_key=lambda identity: identity if identity == "pickup" else None,
                             async_send_bye=bye)
    with pytest.raises(RuntimeError):
        owner.attach_inbound_dialog("caller", "pickup", server, endpoint_id="desk",
                                    expected_generation=session.generation + 1)
    with pytest.raises(ValueError):
        owner.attach_inbound_dialog("caller", "unknown", server, endpoint_id="desk",
                                    expected_generation=session.generation)
    assert not session.legs and not owner.leg_index
    owner.attach_inbound_dialog("caller", "pickup", server, endpoint_id="desk",
                                expected_generation=session.generation)
    other = owner.upsert("other", state="in_call", owner="bridge")
    with pytest.raises(ValueError):
        owner.attach_inbound_dialog("other", "pickup", server, endpoint_id="other-desk",
                                    expected_generation=other.generation)
    assert not other.legs and owner.resolve_session_id("pickup") == "caller"
    await owner.shutdown()


@pytest.mark.parametrize('conflict',[False,True])
async def test_parking_configuration_validates_public_routes(lab, custom_integrations, conflict):
    from homeassistant.config_entries import ConfigSubentry
    from custom_components.voip_stack.store import config_entry
    from custom_components.voip_stack.phonebook_services import _validate_contact_namespace
    from custom_components.voip_stack.roster import RosterEntry

    entry=config_entry(lab.hass)
    lab.hass.config_entries.async_update_entry(entry,data={})
    if conflict:
        lab.hass.config_entries.async_add_subentry(entry,ConfigSubentry(subentry_type='contact',title='701',unique_id='existing-slot',data={'id':'existing-slot','name':'701','number':'12345'}))
    lab.hass.config.components.update({'assist_pipeline','ffmpeg','http','lovelace','media_source','network'})
    form=await lab.hass.config_entries.flow.async_init('voip_stack',context={'source':'reconfigure','entry_id':entry.entry_id})
    form=await lab.hass.config_entries.flow.async_configure(form['flow_id'],{'next_step_id':'parking'})
    assert form['step_id']=='parking'
    data=form['data_schema']({})
    result=await lab.hass.config_entries.flow.async_configure(form['flow_id'],data)
    if conflict:
        assert result['errors']=={'base':'parking_route_conflict'}
        assert not entry.data.get('parking')
    else:
        assert result['type']=='abort'
        assert 'enabled' not in entry.data['parking'] and entry.data['parking']['slots'][0]=='701'
        with pytest.raises(ValueError,match='conflicts'):
            _validate_contact_namespace(lab.hass,[RosterEntry('future-conflict',name='701')])


@pytest.mark.parametrize('trunks',[True],indirect=True)
@pytest.mark.parametrize('retained_outgoing',[False,True])
async def test_browser_pickup_uses_existing_call_and_actual_rtp(registered_phones,trunks,monkeypatch,tmp_path,retained_outgoing):
    import math
    import struct
    import wave
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from aiohttp import WSMsgType
    from custom_components.voip_stack.store import config_entry
    from custom_components.voip_stack.parking import get_parking_lot
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint,EndpointKind
    from custom_components.voip_stack import softphone_originate,audio_ws_view,audio_ws
    from custom_components.voip_stack.roster import RosterEntry,dump_roster_json

    runtime=trunks
    entry=config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(entry,data={**entry.data,'parking':{'enabled':True}})
    endpoint=PhoneEndpoint(endpoint_id='browser-pickup',name='Pickup Browser',device_id='pickup-device',kind=EndpointKind.BROWSER,
                           availability='available',capabilities=frozenset({'audio','dtmf'}))
    runtime.runtime.endpoints.register(endpoint)
    runtime.roster.append(RosterEntry('Pickup Browser',metadata={'local_ha':True,'endpoint_id':endpoint.endpoint_id}))
    runtime.hass.states.async_set('sensor.voip_phonebook','ready',{'roster_json':dump_roster_json(runtime.roster)})
    monkeypatch.setattr(softphone_originate,'_ha_advertise_host',AsyncMock(return_value='127.0.0.1'))
    monkeypatch.setattr(softphone_originate,'_resolve_target_device',AsyncMock(return_value=None))
    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(0.01)
    if retained_outgoing:
        await asyncio.to_thread(registered_phones[0].dial,'transfer_b')
        await wait_for(lambda:any(s.state=='in_call' for s in runtime.calls.sessions.values()))
        await asyncio.to_thread(registered_phones[0].command,f'/transfer sip:700@127.0.0.1:{runtime.runtime.transport_config["sip_port"]}')
        await wait_for(lambda:'701' in get_parking_lot(runtime.hass).calls)
        await wait_for(lambda:not runtime.runtime.endpoints.get('transfer_a').active_call_id)
    else:
        await asyncio.to_thread(registered_phones[0].dial,'700')
    lot=get_parking_lot(runtime.hass)
    source_id=lot.calls['701'].session.call_id
    clients_before=set(runtime.calls.sip_clients_snapshot())
    await runtime.hass.services.async_call('voip_stack','call',{
        'device_id':endpoint.device_id,'destination':'701','media_client_id':'parking-browser-media-owner',
    },blocking=True)
    assert not lot.calls and len(runtime.calls.sessions)==1
    assert runtime.runtime.endpoints.get(endpoint.endpoint_id).active_call_id==source_id
    assert set(runtime.calls.sip_clients_snapshot())==clients_before
    session=audio_ws_view._active_softphone_media_session(runtime.hass,endpoint.endpoint_id)
    assert session.call_id==source_id
    class Browser:
        def __init__(self):
            self.audio=asyncio.Queue()
            self.messages=asyncio.Queue()
            self.ready=asyncio.Event()
            self.closed=False
        def force_close(self):self.closed=True
        def __aiter__(self):return self
        async def __anext__(self):return await self.messages.get()
        async def send_json(self,_value):self.ready.set()
        async def send_bytes(self,value):await self.audio.put(value)
    browser=Browser()
    task=asyncio.create_task(audio_ws_view._run_audio_session(runtime.hass,browser,session,endpoint_id=endpoint.endpoint_id))
    try:
        await asyncio.wait_for(browser.ready.wait(),2)
        pcm=audio_ws.decode_audio_frame(await asyncio.wait_for(browser.audio.get(),2))
        samples=struct.unpack(f'<{len(pcm)//2}h',pcm)
        assert max(abs(value) for value in samples)>500
        if retained_outgoing:
            await asyncio.to_thread(registered_phones[1].command,'/hold')
            await wait_for(lambda:session.local_audio_direction=='recvonly')
            await asyncio.to_thread(registered_phones[1].command,'/resume')
            await wait_for(lambda:session.local_audio_direction=='sendrecv')
        fmt=session.send_format.audio_format
        frame=struct.pack(f'<{fmt.nominal_frame_samples}h',*(int(8000*math.sin(2*math.pi*650*i/fmt.sample_rate)) for i in range(fmt.nominal_frame_samples)))
        for _ in range(30):
            await browser.messages.put(SimpleNamespace(type=WSMsgType.BINARY,data=audio_ws.encode_audio_frame(frame)))
            await asyncio.sleep(fmt.frame_ms/1000)
        from homeassistant.exceptions import ServiceValidationError
        original_owner = runtime.calls.get_session(source_id)
        with pytest.raises(ServiceValidationError, match="generation"):
            await runtime.hass.services.async_call('voip_stack', 'park',
                {'device_id': endpoint.device_id,
                 'expected_generation': original_owner.generation + 1}, blocking=True)
        assert not lot.calls and session.handoff_transport is not None
        response = await runtime.hass.services.async_call('voip_stack', 'park',
            {'device_id': endpoint.device_id}, blocking=True, return_response=True)
        assert response['call_id'] == source_id and response['slot'] == '701'
        application = runtime.calls.resource_for(source_id, 'parking')
        await asyncio.wait_for(task,2)
        assert browser.closed
        assert lot.calls['701'].session.call_id == source_id
        assert endpoint.endpoint_id not in lot.calls['701'].session.endpoint_claims
        assert application.media.transport is not None
        # A real SIP phone retrieves the same caller after browser parking.
        await asyncio.to_thread(registered_phones[2].dial, '701')
        await wait_for(lambda:not lot.calls)
        relay=runtime.calls.resource_for(source_id,'relay')
        await wait_for(lambda:relay.left_tx_packets >= 30 and relay.right_tx_packets >= 30)
        await asyncio.to_thread(registered_phones[2].hangup)
        await wait_for(lambda:not runtime.calls.sessions)
        assert not runtime.runtime.rtp_port_pool['used']
        for peer in registered_phones:
            await asyncio.to_thread(peer.close)
        heard=False
        for path in (tmp_path/('transfer_b' if retained_outgoing else 'transfer_a')).glob('*-dec.wav'):
            with wave.open(str(path),'rb') as recording:
                rate=recording.getframerate()
                values=struct.unpack(f'<{recording.getnframes()}h',recording.readframes(recording.getnframes()))
            if len(values)<800:
                continue
            power={frequency:max(abs(sum(value*complex(math.cos(2*math.pi*frequency*i/rate),math.sin(2*math.pi*frequency*i/rate)) for i,value in enumerate(values[start:start+800]))) for start in range(0,len(values)-799,400)) for frequency in (450,650)}
            heard |= power[650]>power[450]*5 and power[650]>10000
        assert heard, 'The SIP caller did not receive browser PCM after pickup'
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task,return_exceptions=True)


@pytest.mark.parametrize('trunks',[True],indirect=True)
@pytest.mark.parametrize('via_service', [False, True])
async def test_sip_refer_parks_established_caller_without_redial(registered_phones,trunks,via_service):
    from custom_components.voip_stack.store import config_entry
    from custom_components.voip_stack.parking import get_parking_lot
    runtime=trunks
    entry=config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(entry,data={**entry.data,'parking':{'enabled':True}})
    caller,operator,picker=registered_phones
    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(0.01)
    await asyncio.to_thread(caller.dial,'transfer_b')
    await wait_for(lambda: any(s.state=='in_call' for s in runtime.calls.sessions.values()))
    original=next(iter(runtime.calls.sessions.values()))
    source_id=original.call_id
    relay=runtime.calls.resource_for(source_id,'relay')
    transport=relay.left_transport
    if via_service:
        runtime.runtime.endpoints.update('transfer_b', device_id='operator-device')
        response = await runtime.hass.services.async_call('voip_stack', 'park',
            {'device_id': 'operator-device'}, blocking=True, return_response=True)
        assert response['slot'] == '701' and response['call_id'] == source_id
    else:
        await asyncio.to_thread(operator.command,f'/transfer sip:700@127.0.0.1:{runtime.runtime.transport_config["sip_port"]}')
    lot=get_parking_lot(runtime.hass)
    await wait_for(lambda: '701' in lot.calls and not runtime.runtime.endpoints.get('transfer_b').active_call_id)
    assert lot.calls['701'].session is original
    assert runtime.calls.resource_for(source_id,'relay').transport is transport
    assert caller.read().count('Call established')==1
    await asyncio.to_thread(picker.dial,'701')
    await wait_for(lambda: not lot.calls)
    relay=runtime.calls.resource_for(source_id,'relay')
    await wait_for(lambda: relay.left_tx_packets>30 and relay.right_tx_packets>30)
    # The pickup leg is now a native incoming dialog. It can park the same
    # caller again without creating a new source call or leaking another pair.
    await asyncio.to_thread(picker.command,f'/transfer sip:700@127.0.0.1:{runtime.runtime.transport_config["sip_port"]}')
    await wait_for(lambda: '701' in lot.calls and not runtime.runtime.endpoints.get('transfer_c').active_call_id)
    assert lot.calls['701'].session is original
    await asyncio.to_thread(operator.dial,'701')
    await wait_for(lambda: not lot.calls)
    assert len(runtime.runtime.rtp_port_pool['used'])==2
    await asyncio.to_thread(operator.hangup)
    await wait_for(lambda: not runtime.calls.sessions)
    assert not runtime.runtime.rtp_port_pool['used']


@pytest.mark.parametrize('trunks',[True],indirect=True)
@pytest.mark.parametrize("via_service", [False, True])
async def test_caller_can_park_answered_outgoing_peer_and_keep_its_dialog(registered_phones,trunks,via_service):
    from custom_components.voip_stack.store import config_entry
    from custom_components.voip_stack.parking import get_parking_lot
    runtime=trunks
    entry=config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(entry,data={**entry.data,'parking':{'enabled':True}})
    operator,remaining,picker=registered_phones
    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(0.01)
    await asyncio.to_thread(operator.dial,'transfer_b')
    await wait_for(lambda: any(s.state=='in_call' for s in runtime.calls.sessions.values()))
    original=next(iter(runtime.calls.sessions.values()))
    client=runtime.calls.sip_client_for(runtime.calls.bridge_link_for(original.call_id))
    transport=runtime.calls.resource_for(original.call_id,'relay').right_transport
    source_address=transport.get_extra_info('sockname')
    await asyncio.to_thread(operator.command,f'/transfer sip:700@127.0.0.1:{runtime.runtime.transport_config["sip_port"]}')
    lot=get_parking_lot(runtime.hass)
    await wait_for(lambda: '701' in lot.calls and not runtime.runtime.endpoints.get('transfer_a').active_call_id)
    current=lot.calls['701'].session
    assert current is not original and current.state=='in_call'
    assert runtime.calls.sip_client_for(client.dialog_ids.call_id) is client
    media=runtime.calls.resource_for(current.call_id,'relay')
    assert media.transport is transport and remaining.read().count('Call established')==1
    await asyncio.to_thread(remaining.command,'/hold')
    await wait_for(lambda: not media.can_send)
    await asyncio.to_thread(remaining.command,'/resume')
    await wait_for(lambda: media.can_send)
    if via_service:
        runtime.runtime.endpoints.update("transfer_c", device_id="pickup-device")
        result = await runtime.hass.services.async_call("voip_stack", "pickup", {
            "device_id": "pickup-device", "slot": "701", "answer_timeout": 3,
        }, blocking=True, return_response=True)
        assert result["call_id"] == current.call_id
    else:
        await asyncio.to_thread(picker.dial,'701')
    await wait_for(lambda: not lot.calls)
    relay=runtime.calls.resource_for(current.call_id,'relay')
    if via_service:
        assert transport.is_closing()
        assert relay.left_transport.get_extra_info("sockname") == source_address
        assert runtime.calls.sip_client_for(client.dialog_ids.call_id) is client
    else:
        assert relay.left_transport is transport
    await wait_for(lambda: relay.left_tx_packets>30 and relay.right_tx_packets>30)
    await asyncio.to_thread(remaining.hangup)
    await wait_for(lambda: not runtime.calls.sessions)
    assert not runtime.runtime.rtp_port_pool['used']


@pytest.mark.parametrize('trunks',[True],indirect=True)
async def test_failed_pickup_handoff_keeps_original_call_and_allows_retry(registered_phones,trunks,monkeypatch):
    from custom_components.voip_stack.store import config_entry
    from custom_components.voip_stack.parking import get_parking_lot
    runtime=trunks
    entry=config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(entry,data={**entry.data,'parking':{'enabled':True}})
    caller,first,second=registered_phones
    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(0.01)
    await asyncio.to_thread(caller.dial,'700')
    lot=get_parking_lot(runtime.hass)
    parked=lot.calls['701']
    media=runtime.calls.resource_for(parked.session.call_id,'relay')
    transport=media.transport
    protocol=transport.get_protocol()
    original_set=transport.set_protocol
    failures=[]
    def fail_once(value):
        if not failures:
            failures.append(True)
            raise RuntimeError('injected RTP ownership failure')
        original_set(value)
    monkeypatch.setattr(transport,'set_protocol',fail_once)
    await asyncio.to_thread(first.command,'/dial 701')
    await wait_for(lambda:failures and parked.claim is None and not runtime.runtime.endpoints.get('transfer_b').active_call_id)
    assert parked.session.live and media.transport is transport and transport.get_protocol() is protocol
    assert runtime.runtime.rtp_port_pool['used']==set(media.reservation.ports)
    assert caller.read().count('Call established')==1
    await asyncio.to_thread(second.dial,'701')
    await wait_for(lambda:not lot.calls)
    await asyncio.to_thread(second.hangup)
    await wait_for(lambda:not runtime.calls.sessions)
    assert not runtime.runtime.rtp_port_pool['used']


@pytest.mark.parametrize('trunks', [True], indirect=True)
@pytest.mark.parametrize('parker', ['local-a', 'local-b'])
@pytest.mark.parametrize('pickup_kind', ['sip', 'browser', 'return-browser'])
async def test_local_browser_parking_keeps_other_phone_for_sip_pickup(registered_phones, trunks, parker, pickup_kind, monkeypatch):
    import struct
    from aiohttp import WSMsgType
    from custom_components.voip_stack import audio_ws, audio_ws_view
    from custom_components.voip_stack.local_softphone_runtime import (
        async_setup_local_softphone_bridge, async_shutdown_local_softphone_bridge,
        start_local_softphone_call,
    )
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
    from custom_components.voip_stack.parking import get_parking_lot
    from custom_components.voip_stack.store import config_entry

    runtime = trunks
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(entry, data={**entry.data, 'parking': {'enabled': True}})
    for name in ('local-a', 'local-b', 'local-c'):
        runtime.runtime.endpoints.register(PhoneEndpoint(endpoint_id=name, name=name,
            device_id='device-' + name, kind=EndpointKind.BROWSER,
            availability='available', capabilities=frozenset({'audio', 'dtmf'})))
    bridge = async_setup_local_softphone_bridge(runtime.hass)
    call = start_local_softphone_call(runtime.hass, 'local-a', 'local-b', caller_owner_id='owner-a')
    bridge.answer(call.call_id, 'local-b', 'owner-b')
    retained = 'local-b' if parker == 'local-a' else 'local-a'
    picker = parker if pickup_kind == 'return-browser' else 'local-c'
    from custom_components.voip_stack.local_call_media import LocalCallMedia
    from homeassistant.exceptions import ServiceValidationError
    original_start = LocalCallMedia.start
    async def fail_after_bind(media):
        await original_start(media)
        raise OSError("injected application startup failure")
    with monkeypatch.context() as fault:
        fault.setattr(LocalCallMedia, 'start', fail_after_bind)
        with pytest.raises(OSError, match="injected"):
            await runtime.hass.services.async_call('voip_stack', 'park',
                {'device_id': 'device-' + parker}, blocking=True)
    assert bridge.get_call(call.call_id).callee_state.value == 'in_call'
    assert not get_parking_lot(runtime.hass).calls
    assert not runtime.runtime.rtp_port_pool['used']
    assert runtime.calls.resource_for(call.call_id, 'softphone_media')['local_bridge']
    lease = bridge.acquire_media(call.call_id, parker, 'owner-a' if parker == 'local-a' else 'owner-b')
    async def start_while_audio_arrives(media):
        await original_start(media)
        bridge.send_audio(call.call_id, parker, lease.token, struct.pack('<480h', *([6000] * 480)))
    with monkeypatch.context() as live_audio:
        live_audio.setattr(LocalCallMedia, 'start', start_while_audio_arrives)
        response = await runtime.hass.services.async_call('voip_stack', 'park',
            {'device_id': 'device-' + parker}, blocking=True, return_response=True)
    with pytest.raises(ServiceValidationError, match="does not own"):
        await runtime.hass.services.async_call('voip_stack', 'park',
            {'device_id': 'device-' + parker, 'call_id': call.call_id}, blocking=True)
    assert response['call_id'] == call.call_id
    assert bridge.get_call(call.call_id) is None
    assert not runtime.runtime.endpoints.get(parker).active_call_id
    assert runtime.runtime.endpoints.get(retained).active_call_id == call.call_id
    media = audio_ws_view._active_softphone_media_session(runtime.hass, retained)
    assert media is not None

    class Browser:
        def __init__(self):
            self.messages = asyncio.Queue()
            self.audio = asyncio.Queue()
            self.ready = asyncio.Event()
        def force_close(self):
            pass
        def __aiter__(self):
            return self
        async def __anext__(self):
            return await self.messages.get()
        async def send_json(self, value):
            self.ready.set()
        async def send_bytes(self, value):
            await self.audio.put(value)

    browser = Browser()
    worker = asyncio.create_task(audio_ws_view._run_audio_session(runtime.hass, browser, media, endpoint_id=retained))
    async def wait_for(predicate):
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(0.01)
    try:
        await asyncio.wait_for(browser.ready.wait(), 2)
        if pickup_kind != 'sip':
            from unittest.mock import AsyncMock
            from custom_components.voip_stack import softphone_originate
            from custom_components.voip_stack.roster import RosterEntry, dump_roster_json
            runtime.roster.append(RosterEntry(picker, metadata={'local_ha': True, 'endpoint_id': picker}))
            runtime.hass.states.async_set('sensor.voip_phonebook', 'ready', {'roster_json': dump_roster_json(runtime.roster)})
            monkeypatch.setattr(softphone_originate, '_ha_advertise_host', AsyncMock(return_value='127.0.0.1'))
            monkeypatch.setattr(softphone_originate, '_resolve_target_device', AsyncMock(return_value=None))
            await runtime.hass.services.async_call('voip_stack', 'call',
                {'device_id': 'device-' + picker, 'destination': '701', 'media_client_id': 'owner-c'}, blocking=True)
            resumed = bridge.get_call(call.call_id)
            assert resumed is not None and resumed.caller_endpoint_id == retained
            await asyncio.wait_for(worker, 2)
            assert not runtime.runtime.rtp_port_pool['used']
            source_lease = bridge.acquire_media(call.call_id, retained, 'owner-b' if retained == 'local-b' else 'owner-a')
            target_lease = bridge.acquire_media(call.call_id, picker, 'owner-c')
            left_pcm = struct.pack('<480h', *([7000] * 480))
            right_pcm = struct.pack('<480h', *([9000] * 480))
            bridge.send_audio(call.call_id, retained, source_lease.token, left_pcm)
            bridge.send_audio(call.call_id, picker, target_lease.token, right_pcm)
            assert bridge.receive_audio_nowait(call.call_id, picker, target_lease.token) == left_pcm
            assert bridge.receive_audio_nowait(call.call_id, retained, source_lease.token) == right_pcm
            # The restored ordinary browser call can be parked in the opposite direction.
            await runtime.hass.services.async_call('voip_stack', 'park',
                {'device_id': 'device-' + retained}, blocking=True)
            assert get_parking_lot(runtime.hass).calls['701'].session.call_id == call.call_id
            assert runtime.calls.get_session(call.call_id).metadata['source_endpoint_id'] == picker
            await runtime.hass.services.async_call('voip_stack', 'hangup',
                {'device_id': 'device-' + picker}, blocking=True)
            await wait_for(lambda: not runtime.calls.sessions)
            assert not runtime.runtime.rtp_port_pool['used']
            return
        else:
            await asyncio.to_thread(registered_phones[2].dial, '701')
        await wait_for(lambda: not get_parking_lot(runtime.hass).calls)
        frame = struct.pack('<h', 7000) * media.send_format.audio_format.nominal_frame_samples
        for _ in range(20):
            await browser.messages.put(SimpleNamespace(type=WSMsgType.BINARY, data=audio_ws.encode_audio_frame(frame)))
            await asyncio.sleep(media.send_format.audio_format.frame_ms / 1000)
        async with asyncio.timeout(3):
            while True:
                pcm = audio_ws.decode_audio_frame(await browser.audio.get())
                if max(abs(value) for value in struct.unpack(f'<{len(pcm)//2}h', pcm)) > 1000:
                    break
        relay = runtime.calls.resource_for(call.call_id, 'relay')
        assert relay.left_rx_packets > 0 and relay.right_rx_packets > 0
        await asyncio.to_thread(registered_phones[2].hangup)
        await wait_for(lambda: not runtime.calls.sessions)
        await asyncio.wait_for(worker, 2)
        assert not runtime.runtime.rtp_port_pool['used']
    finally:
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        async_shutdown_local_softphone_bridge(runtime.hass)


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_inbound_trunk_parking_can_return_to_same_browser(trunks, monkeypatch):
    """An external caller remains parked while its original browser rejoins."""
    import socket
    import struct
    from unittest.mock import AsyncMock
    from aiohttp import WSMsgType
    from custom_components.voip_stack import audio_ws, audio_ws_view, softphone_originate
    from custom_components.voip_stack.config import normalized_trunk_config
    from custom_components.voip_stack.core.audio_format import AudioFormat
    from custom_components.voip_stack.core import rtp
    from custom_components.voip_stack.parking import get_parking_lot
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
    from custom_components.voip_stack.roster import RosterEntry, dump_roster_json
    from custom_components.voip_stack.sip_client import SipCallClient
    from custom_components.voip_stack.store import config_entry

    lab = trunks
    entry = config_entry(lab.hass)
    lab.hass.config_entries.async_update_entry(entry, data={**entry.data, "parking": {"enabled": True}})
    phone = PhoneEndpoint(endpoint_id="desk", name="Desk", device_id="desk-device",
                          kind=EndpointKind.BROWSER, availability="available", capabilities=frozenset({"audio", "dtmf"}))
    lab.runtime.endpoints.register(phone)
    lab.runtime.preferred_phone_device_id = phone.device_id
    lab.roster.append(RosterEntry("Desk", metadata={"local_ha": True, "endpoint_id": phone.endpoint_id}))
    lab.hass.states.async_set("sensor.voip_phonebook", "ready", {"roster_json": dump_roster_json(lab.roster)})
    lab.runtime.trunk_configs["home"] = normalized_trunk_config({
        "trunk_enabled": True, "trunk_register": False, "trunk_server": "127.0.0.3",
        "trunk_allowed_ips": ["127.0.0.3"], "trunk_inbound_default_target": "Desk",
    })
    from custom_components.voip_stack.trunk_runtime import async_start_sip_trunk, async_stop_sip_trunk
    await async_stop_sip_trunk(lab.hass)
    lab.runtime.trunk_configs = {"home": lab.runtime.trunk_configs["home"]}
    lab.runtime.trunk_config = lab.runtime.trunk_configs["home"]
    await async_start_sip_trunk(lab.hass, local_ip="127.0.0.1")
    monkeypatch.setattr(softphone_originate, "_ha_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(softphone_originate, "_resolve_target_device", AsyncMock(return_value=None))
    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(.01)

    class Browser:
        def __init__(self):
            self.audio = asyncio.Queue()
            self.messages = asyncio.Queue()
            self.ready = asyncio.Event()
        def force_close(self):
            pass
        def __aiter__(self):
            return self
        async def __anext__(self):
            return await self.messages.get()
        async def send_json(self, value):
            self.ready.set()
        async def send_bytes(self, value):
            await self.audio.put(value)

    audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    audio.bind(("127.0.0.3", 0))
    audio.setblocking(False)
    client = SipCallClient(local_ip="127.0.0.3", local_name="External", local_sip_port=0,
                           local_rtp_port=audio.getsockname()[1], supported_formats=[AudioFormat(16000, "s16le", 1, 20)])
    from custom_components.voip_stack.sip_udp_io import SipDatagramQueueProtocol
    client.transport, client.protocol = await lab.hass.loop.create_datagram_endpoint(
        lambda: SipDatagramQueueProtocol(client.queue), local_addr=("127.0.0.3", 0))
    client.local_sip_port = client.transport.get_extra_info("sockname")[1]
    workers = []
    signaling = None
    try:
        port = lab.runtime.transport_config["sip_port"]
        assert await client.invite(target="Desk", remote_host="127.0.0.1", remote_sip_port=port,
                                   request_uri=f"sip:Desk@127.0.0.1:{port}", timeout=2) == "ringing"
        call_id = client.dialog_ids.call_id
        await lab.hass.services.async_call("voip_stack", "answer", {"device_id": phone.device_id, "call_id": call_id}, blocking=True)
        await wait_for(lambda: client.dialog is not None and lab.runtime.endpoints.get(phone.endpoint_id).active_call_id == call_id)
        signaling = asyncio.create_task(client.wait_for_dialog_termination(timeout=15))
        owner = lab.calls.get_session(call_id)
        assert owner.metadata["inbound_trunk_id"] == "home"
        lot = get_parking_lot(lab.hass)
        for cycle in range(2):
            media = audio_ws_view._active_softphone_media_session(lab.hass, phone.endpoint_id)
            browser = Browser()
            worker = asyncio.create_task(audio_ws_view._run_audio_session(lab.hass, browser, media, endpoint_id=phone.endpoint_id))
            workers.append(worker)
            await asyncio.wait_for(browser.ready.wait(), 2)
            # Keep the call's real RTP path observable before each park.
            frame = struct.pack("<h", 6000) * media.send_format.audio_format.nominal_frame_samples
            await browser.messages.put(SimpleNamespace(type=WSMsgType.BINARY, data=audio_ws.encode_audio_frame(frame)))
            async with asyncio.timeout(2):
                while True:
                    raw = await lab.hass.loop.sock_recv(audio, 4096)
                    payload = rtp.parse_packet(raw).payload
                    if any(payload):
                        samples = struct.unpack(f"!{len(payload)//2}h", payload)
                        assert 5940 <= max(samples) <= 6060
                        break
            assert client.dialog.send_format.encoding == "L16"
            for index in range(10):
                sequence = cycle * 10 + index
                packet = rtp.build_packet(rtp.RtpPacket(
                    client.dialog.send_format.payload_type, sequence, sequence * 320, 99,
                    struct.pack("!h", 7000) * 320))
                await lab.hass.loop.sock_sendto(audio, packet,
                    (client.dialog.remote_rtp_host, client.dialog.remote_rtp_port))
                await asyncio.sleep(.02)
            async with asyncio.timeout(2):
                while True:
                    decoded = audio_ws.decode_audio_frame(await browser.audio.get())
                    if any(decoded):
                        assert max(struct.unpack(f"<{len(decoded)//2}h", decoded)) == 7000
                        break
            response = await lab.hass.services.async_call("voip_stack", "park", {"device_id": phone.device_id},
                                                          blocking=True, return_response=True)
            await asyncio.wait_for(worker, 2)
            assert response["slot"] == "701"
            assert not lab.runtime.endpoints.get(phone.endpoint_id).active_call_id
            assert phone.endpoint_id not in owner.endpoint_claims
            assert lot.calls["701"].session is owner
            await lab.hass.services.async_call("voip_stack", "call", {"device_id": phone.device_id, "destination": "701"}, blocking=True)
            assert not lot.calls
            assert lab.calls.get_session(call_id) is owner
            assert lab.runtime.endpoints.get(phone.endpoint_id).active_call_id == call_id
        await lab.hass.services.async_call("voip_stack", "hangup", {"device_id": phone.device_id}, blocking=True)
        await wait_for(lambda: not lab.calls.sessions)
        assert not lot.calls
        assert await asyncio.wait_for(signaling, 2) == "remote_hangup"
        assert not lab.runtime.rtp_port_pool["used"]
    finally:
        await client.close()
        if signaling is not None:
            workers.append(signaling)
        for worker in workers:
            if not worker.done():
                worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        audio.close()


@pytest.mark.parametrize("active_video", [False, True])
async def test_browser_video_parking_requires_audio_readiness_without_mutating_call(monkeypatch, active_video):
    from unittest.mock import Mock
    from custom_components.voip_stack import parking
    owner = SimpleNamespace(call_id="video-call", live=True, state="in_call", endpoint_claims={"browser"})
    registry = Mock()
    registry.resolve_session_id.return_value = "video-call"
    registry.get_session.return_value = owner
    registry.resource_for.return_value = None if active_video else {"video_direction": "sendrecv", "video_format": object()}
    runtime = SimpleNamespace(sip=registry, media=SimpleNamespace(sessions_for=lambda kind:
        {("video-call", "browser"): object()} if active_video and kind == "video" else {}))
    monkeypatch.setattr(parking, "require_runtime_data", lambda _: runtime)
    with pytest.raises(ParkingError, match="audio connection"):
        await parking.park_browser_sip_call(object(), "video-call", "browser")
    registry.take_media.assert_not_called()
    registry.request_termination.assert_not_called()


@pytest.mark.parametrize('trunks', [True], indirect=True)
@pytest.mark.parametrize('offer_video', [False, True])
@pytest.mark.parametrize('capabilities_known', [False, True])
async def test_originated_browser_call_parks_without_inbound_media_descriptor(
    registered_phones, trunks, monkeypatch, offer_video, capabilities_known, tmp_path,
):
    import struct
    import wave
    from aiohttp import WSMsgType
    from unittest.mock import AsyncMock
    from custom_components.voip_stack import audio_ws_view, audio_ws, softphone_originate
    from custom_components.voip_stack.const import CONF_SIP_VIDEO
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
    from custom_components.voip_stack.parking import get_parking_lot
    from custom_components.voip_stack.store import config_entry
    from custom_components.voip_stack.sip_client import SipCallClient

    runtime = trunks
    if not capabilities_known:
        from custom_components.voip_stack.endpoint_routing import assemble_directory_entries

        # Exercise the ordinary interval before an OPTIONS description arrives.
        # The independent audio-only phone still answers the real offer.
        registrar = runtime.calls.component('registrar')
        for registration in registrar.registrations.values():
            if registration.username == 'transfer_a':
                registration.capabilities = None
        runtime.roster[:] = assemble_directory_entries(runtime.hass, [], registrar.registered_roster_entries())
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(entry, data={**entry.data, 'parking': {'enabled': True}})
    runtime.runtime.transport_config[CONF_SIP_VIDEO] = offer_video
    if offer_video:
        runtime.runtime.endpoints.update('transfer_a', capabilities=frozenset({'audio', 'dtmf', 'video'}))
    endpoint = PhoneEndpoint(endpoint_id='direct-browser', name='Direct browser', device_id='direct-browser-device',
        kind=EndpointKind.BROWSER, availability='available', capabilities=frozenset({'audio', 'dtmf', 'video'}))
    runtime.runtime.endpoints.register(endpoint)
    from custom_components.voip_stack.roster import RosterEntry, dump_roster_json
    runtime.roster.append(RosterEntry(endpoint.name, metadata={'local_ha': True, 'endpoint_id': endpoint.endpoint_id}))
    runtime.hass.states.async_set('sensor.voip_phonebook', 'ready', {'roster_json': dump_roster_json(runtime.roster)})
    monkeypatch.setattr(softphone_originate, '_ha_advertise_host', AsyncMock(return_value='127.0.0.1'))
    monkeypatch.setattr(softphone_originate, '_resolve_target_device', AsyncMock(return_value=None))

    class Browser:
        def __init__(self):
            self.ready = asyncio.Event()
            self.audio = asyncio.Queue()
            self.messages = asyncio.Queue()
            self.closed = False
        def force_close(self): self.closed = True
        def __aiter__(self): return self
        async def __anext__(self): return await self.messages.get()
        async def send_json(self, _): self.ready.set()
        async def send_bytes(self, value): self.audio.put_nowait(value)

    tasks = []
    async def connect():
        media = audio_ws_view._active_softphone_media_session(runtime.hass, endpoint.endpoint_id)
        client = runtime.calls.sip_client_for(media.call_id)
        if client is not None and client.audio_rtp_socket is media.rtp_socket:
            client.audio_rtp_socket = None  # The HTTP handler transfers this socket before running media.
        browser = Browser()
        task = asyncio.create_task(audio_ws_view._run_audio_session(runtime.hass, browser, media, endpoint_id=endpoint.endpoint_id))
        tasks.append(task)
        await asyncio.wait_for(browser.ready.wait(), 2)
        pcm = audio_ws.decode_audio_frame(await asyncio.wait_for(browser.audio.get(), 2))
        samples = struct.unpack(f'<{len(pcm)//2}h', pcm)
        assert max(abs(value) for value in samples) > 500
        return browser, media

    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(.01)

    final_received = asyncio.Event()
    release_final = asyncio.Event()
    native_read_response = SipCallClient._read_response

    async def hold_final_answer(client, *args, **kwargs):
        response = await native_read_response(client, *args, **kwargs)
        if response is not None:
            message, _address = response
            if (message.status_code == 200
                    and message.header("CSeq").endswith(" INVITE")):
                final_received.set()
                await release_final.wait()
        return response

    monkeypatch.setattr(SipCallClient, "_read_response", hold_final_answer)

    try:
        await runtime.hass.services.async_call('voip_stack', 'call', {
            'device_id': endpoint.device_id, 'destination': 'transfer_a', 'media_client_id': 'direct-browser-media',
        }, blocking=True)
        call_id = runtime.runtime.endpoints.get(endpoint.endpoint_id).active_call_id
        client = runtime.calls.sip_client_for(call_id)
        await asyncio.wait_for(final_received.wait(), 2)
        # The call service returns at 180; blocking=True does not wait for
        # the peer's final answer. Keep that valid window deterministic.
        assert client.dialog is None
        assert runtime.calls.get_session(call_id).state == 'remote_ringing'
        release_final.set()
        await wait_for(lambda: client.dialog is not None
                       and runtime.calls.get_session(call_id).state == 'in_call')
        assert client.dialog is not None and client.dialog.video_format is None
        assert runtime.calls.resource_for(call_id, 'softphone_media') is None
        offered_video = offer_video and not capabilities_known
        assert bool(client.video_formats) is offered_video
        if offered_video:
            assert client.media_reservation is not None
        else:
            assert client.media_reservation is None
        browser, media = await connect()
        source = media.rtp_source
        response = await runtime.hass.services.async_call('voip_stack', 'park', {
            'device_id': endpoint.device_id,
        }, blocking=True, return_response=True)
        assert response['slot'] == '701' and response['call_id'] == call_id
        await asyncio.wait_for(tasks[0], 2)
        assert browser.closed
        application = runtime.calls.resource_for(call_id, 'parking')
        assert application.media.rtp_source is source
        assert runtime.calls.sip_client_for(call_id) is client
        assert not client._closed
        await runtime.hass.services.async_call('voip_stack', 'call', {
            'device_id': endpoint.device_id, 'destination': '701', 'media_client_id': 'direct-browser-media',
        }, blocking=True)
        assert not get_parking_lot(runtime.hass).calls
        recovered_browser, recovered_media = await connect()
        fmt = recovered_media.send_format.audio_format
        pcm = struct.pack(f'<{fmt.nominal_frame_samples}h', *([1200] * fmt.nominal_frame_samples))
        for _ in range(30):
            recovered_browser.messages.put_nowait(SimpleNamespace(type=WSMsgType.BINARY, data=audio_ws.encode_audio_frame(pcm)))
            await asyncio.sleep(fmt.frame_ms / 1000)
        await runtime.hass.services.async_call('voip_stack', 'hangup', {'device_id': endpoint.device_id}, blocking=True)
        await wait_for(lambda: not runtime.calls.sessions)
        assert not runtime.runtime.rtp_port_pool['used']
        await asyncio.to_thread(registered_phones[0].close)
        heard = 0
        for path in (tmp_path / 'transfer_a').glob('*-dec.wav'):
            with wave.open(str(path), 'rb') as recording:
                samples = struct.unpack(f'<{recording.getnframes()}h', recording.readframes(recording.getnframes()))
                heard += sum(abs(value - 1200) < 80 for value in samples)
        assert heard > 1000, 'The retained SIP peer did not receive browser audio after pickup'
    finally:
        release_final.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
