from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.ha


@pytest.fixture(autouse=True)
def media_projection(monkeypatch):
    from custom_components.voip_stack import call_projection
    projection = MagicMock()
    monkeypatch.setattr(call_projection, "publish_esp_media_route", projection)
    return projection


def _format(token: str):
    return SimpleNamespace(
        audio_format=SimpleNamespace(wire_token=lambda: token),
        wire_token=lambda: token,
    )


def _fixture():
    from custom_components.voip_stack.outbound_attempts import OutboundLeg

    client = SimpleNamespace(
        dialog_ids=SimpleNamespace(call_id="dest-call"),
        dialog=object(),
        wait_for_dialog_termination=AsyncMock(return_value="remote_hangup"),
    )
    ports = SimpleNamespace(
        ports=(40000, 40002), detach=MagicMock(), release=MagicMock()
    )
    winner = OutboundLeg("desk", "sip:desk@pbx", client, ports)
    invite = SimpleNamespace(
        call_id="source-call",
        send_format=_format("PCMA/8000"),
        recv_format=_format("PCMU/8000"),
        send_video_format=None,
        recv_video_format=None,
        remote_sdp=(
            "v=0\r\nc=IN IP4 127.0.0.1\r\n"
            "m=audio 5000 RTP/AVP 8\r\na=rtpmap:8 PCMA/8000\r\n"
        ),
    )
    session = SimpleNamespace(generation=1, create_task=asyncio.create_task)
    registry = SimpleNamespace(
        register_bridge=MagicMock(return_value=session),
        is_generation_current=MagicMock(return_value=True),
        request_termination=MagicMock(return_value=None),
        forget_bridge_link=MagicMock(),
        close_leg=AsyncMock(return_value=True),
        attach_relay=MagicMock(),
        attach_media=MagicMock(),
        take_pending_invite=MagicMock(),
        take_media=MagicMock(return_value=None),
    )
    relay = SimpleNamespace(
        media_route="ha_transcoding",
        video_relay=None,
        start=AsyncMock(),
        stop=AsyncMock(),
        attach_video_relay=MagicMock(),
    )
    return invite, winner, registry, relay


def _pending_watchers() -> list[asyncio.Task]:
    return [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and task.get_name().startswith("voip-bridge-destination-")
        and not task.done()
    ]


@pytest.mark.asyncio
async def test_commit_transfers_bridge_and_owns_destination_watcher(monkeypatch, media_projection):
    from custom_components.voip_stack import outbound_bridge_commit as module
    from custom_components.voip_stack.inbound_answer import AnswerCommitResult

    invite, winner, registry, relay = _fixture()
    binder = SimpleNamespace(attach=MagicMock())
    monkeypatch.setattr(module, "build_invite_client_relay", lambda **_: relay)
    monkeypatch.setattr(module, "attach_dtmf_event_bridge", MagicMock())
    monkeypatch.setattr(module, "build_answer_directional", lambda *_a, **_k: "sdp")
    monkeypatch.setattr(module, "BridgeMediaUpdateBinder", lambda _hass: binder)
    monkeypatch.setattr(
        module,
        "async_commit_runtime_answer",
        AsyncMock(return_value=AnswerCommitResult(True, True)),
    )
    monkeypatch.setattr(
        module, "async_watch_sip_bridge_destination", AsyncMock()
    )

    result = await module.async_commit_outbound_bridge(
        MagicMock(),
        registry,
        module.BridgeCommitData(
            invite=invite,
            winner=winner,
            source_relay_port=40000,
            dest_relay_port=40002,
            local_ip="127.0.0.1",
            release_port_pairs=(winner.ports.ports,),
            detach_reservations=(winner.ports,),
        ),
        module.BridgeCommitPolicy(
            route_kind="direct",
            caller="door",
            callee="desk",
            connected_party="desk",
        ),
    )
    await asyncio.sleep(0)

    assert result is not None
    relay.start.assert_awaited_once()
    winner.ports.detach.assert_called_once()
    registry.attach_relay.assert_called_once_with("source-call", relay)
    binder.attach.assert_called_once()
    assert media_projection.call_args.args[1:] == ("source-call", "ha_transcoding")
    assert not _pending_watchers()


@pytest.mark.asyncio
async def test_commit_reuses_registered_session_without_second_watcher(monkeypatch):
    from custom_components.voip_stack import outbound_bridge_commit as module
    from custom_components.voip_stack.inbound_answer import AnswerCommitResult

    invite, winner, registry, relay = _fixture()
    session = SimpleNamespace(generation=7, create_task=asyncio.create_task)
    registry.is_generation_current = MagicMock(return_value=True)
    registry.register_bridge.side_effect = AssertionError("duplicate bridge owner")
    monkeypatch.setattr(module, "build_invite_client_relay", lambda **_: relay)
    monkeypatch.setattr(module, "attach_dtmf_event_bridge", MagicMock())
    monkeypatch.setattr(module, "build_answer_directional", lambda *_a, **_k: "sdp")
    monkeypatch.setattr(
        module,
        "BridgeMediaUpdateBinder",
        lambda _hass: SimpleNamespace(attach=MagicMock()),
    )
    monkeypatch.setattr(
        module,
        "async_commit_runtime_answer",
        AsyncMock(return_value=AnswerCommitResult(True, True)),
    )

    result = await module.async_commit_outbound_bridge(
        MagicMock(),
        registry,
        module.BridgeCommitData(
            invite=invite,
            winner=winner,
            source_relay_port=40000,
            dest_relay_port=40002,
            local_ip="127.0.0.1",
            release_port_pairs=(winner.ports.ports,),
            detach_reservations=(winner.ports,),
        ),
        module.BridgeCommitPolicy(
            route_kind="direct",
            caller="door",
            callee="desk",
            connected_party="desk",
        ),
        session=session,
    )
    await asyncio.sleep(0)

    assert result is not None
    assert registry.is_generation_current.call_args.args == ("source-call", 7)
    registry.register_bridge.assert_not_called()
    assert not _pending_watchers()


@pytest.mark.asyncio
async def test_commit_atomically_transfers_provisional_video_owner(monkeypatch):
    from custom_components.voip_stack import outbound_bridge_commit as module
    from custom_components.voip_stack.inbound_answer import AnswerCommitResult

    invite, winner, registry, relay = _fixture()
    reservation = SimpleNamespace(release=MagicMock())
    rtp_socket = SimpleNamespace(close=MagicMock())
    rtcp_socket = SimpleNamespace(close=MagicMock())
    pending_source = {
        "video_rtp_reservation": reservation,
        "video_rtp_socket": rtp_socket,
        "video_rtcp_socket": rtcp_socket,
    }
    registry.take_media.return_value = pending_source
    video_relay = SimpleNamespace(
        left=SimpleNamespace(send_format=object(), recv_format=object()),
        left_port=40008,
    )
    winner.video_relay = video_relay
    relay.attach_video_relay.side_effect = lambda value: setattr(
        relay, "video_relay", value
    )
    binder = SimpleNamespace(attach=MagicMock())
    monkeypatch.setattr(module, "build_invite_client_relay", lambda **_: relay)
    monkeypatch.setattr(
        module,
        "configure_answered_invite_video_relay",
        lambda *_a, **_k: SimpleNamespace(direction="sendrecv"),
    )
    monkeypatch.setattr(module, "attach_dtmf_event_bridge", MagicMock())
    monkeypatch.setattr(
        module, "async_start_sip_bridge_media", AsyncMock(return_value=False)
    )
    activate_source = AsyncMock(return_value=True)
    monkeypatch.setattr(
        module,
        "sip_endpoint_manager",
        lambda _hass: SimpleNamespace(
            async_activate_video_reinvite=activate_source
        ),
    )
    monkeypatch.setattr(module, "build_answer_directional", lambda *_a, **_k: "")
    monkeypatch.setattr(module, "BridgeMediaUpdateBinder", lambda _hass: binder)
    monkeypatch.setattr(
        module,
        "async_commit_runtime_answer",
        AsyncMock(return_value=AnswerCommitResult(True, True)),
    )
    monkeypatch.setattr(module, "async_watch_sip_bridge_destination", AsyncMock())

    await module.async_commit_outbound_bridge(
        MagicMock(),
        registry,
        module.BridgeCommitData(
            invite=invite,
            winner=winner,
            source_relay_port=40000,
            dest_relay_port=40002,
            local_ip="127.0.0.1",
            release_port_pairs=(winner.ports.ports,),
            detach_reservations=(winner.ports,),
        ),
        module.BridgeCommitPolicy(
            route_kind="trunk",
            caller="provider",
            callee="desk",
            connected_party="desk",
            response_already_sent=True,
            consume_pending_source=True,
        ),
    )
    await asyncio.sleep(0)

    registry.attach_relay.assert_called_once_with("source-call", relay)
    registry.take_media.assert_called_once_with("source-call", provisional=True)
    assert pending_source == {}
    reservation.release.assert_not_called()
    rtp_socket.close.assert_not_called()
    rtcp_socket.close.assert_not_called()
    assert relay.video_relay is video_relay
    activate_source.assert_awaited_once_with(
        "source-call",
        local_video_rtp_port=relay.video_relay.left_port,
        video_formats=(
            relay.video_relay.left.recv_format,
            relay.video_relay.left.send_format,
        ),
        video_direction="sendrecv",
    )
    assert not _pending_watchers()


@pytest.mark.asyncio
async def test_preanswered_video_activation_uses_source_dialog(monkeypatch):
    from custom_components.voip_stack.sip_endpoint import SipEndpointManager

    send = object()
    receive = object()
    prepared = SimpleNamespace(commit=MagicMock(return_value=True))
    endpoint = SimpleNamespace(
        async_prepare_video_reinvite=AsyncMock(return_value=prepared)
    )

    assert await SipEndpointManager.async_activate_video_reinvite(
        endpoint,
        "source-call",
        local_video_rtp_port=40008,
        video_formats=(receive, send),
        video_direction="sendrecv",
    )
    endpoint.async_prepare_video_reinvite.assert_awaited_once_with(
        "source-call",
        local_video_rtp_port=40008,
        video_formats=(receive, send),
        video_direction="sendrecv",
    )
    prepared.commit.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["relay", "answer"])
async def test_commit_failure_leaves_no_destination_watcher(monkeypatch, failure):
    from custom_components.voip_stack import outbound_bridge_commit as module
    from custom_components.voip_stack.inbound_answer import AnswerCommitResult

    invite, winner, registry, relay = _fixture()
    binder = SimpleNamespace(attach=MagicMock())
    monkeypatch.setattr(module, "build_invite_client_relay", lambda **_: relay)
    monkeypatch.setattr(module, "attach_dtmf_event_bridge", MagicMock())
    monkeypatch.setattr(module, "build_answer_directional", lambda *_a, **_k: "sdp")
    monkeypatch.setattr(module, "BridgeMediaUpdateBinder", lambda _hass: binder)
    monkeypatch.setattr(
        module,
        "async_commit_runtime_answer",
        AsyncMock(
            return_value=AnswerCommitResult(
                failure != "answer", failure != "answer", "claim_failed"
            )
        ),
    )
    if failure == "relay":
        relay.start.side_effect = RuntimeError("media failed")

    with pytest.raises(RuntimeError):
        await module.async_commit_outbound_bridge(
            MagicMock(),
            registry,
            module.BridgeCommitData(
                invite=invite,
                winner=winner,
                source_relay_port=40000,
                dest_relay_port=40002,
                local_ip="127.0.0.1",
                release_port_pairs=(winner.ports.ports,),
                detach_reservations=(winner.ports,),
            ),
            module.BridgeCommitPolicy(
                route_kind="direct",
                caller="door",
                callee="desk",
                connected_party="desk",
            ),
        )
    await asyncio.sleep(0)

    assert not _pending_watchers()


@pytest.fixture
def owned_bridge(monkeypatch, socket_enabled):
    """Real session, relay sockets and reservation release inventory."""
    from custom_components.voip_stack import outbound_bridge_commit as module
    from custom_components.voip_stack import media_ports
    from custom_components.voip_stack.core.audio_format import AudioFormat
    from custom_components.voip_stack.inbound_answer import AnswerCommitResult
    from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
    from custom_components.voip_stack.sip_rtp_bridge import RtpPeer, SipRtpRelay
    import socket

    invite, winner, _, _ = _fixture()
    registry = SipEndpointRuntime(allow_dark_sessions=True)
    registry.activate()
    session = registry.upsert(invite.call_id, state="connecting", owner="bridge")
    pool = {(40000, 40002)}
    releases = []

    def release(_hass, ports):
        assert ports in pool, "reservation was released twice"
        pool.remove(ports)
        releases.append(ports)

    winner.ports = media_ports.RtpPortReservation(object(), (40000, 40002))
    monkeypatch.setattr(media_ports, "release_sip_rtp_port_pair", release)
    monkeypatch.setattr(module, "release_sip_rtp_port_pair", release)
    monkeypatch.setattr(module, "attach_dtmf_event_bridge", MagicMock())
    monkeypatch.setattr(module, "build_answer_directional", lambda *_a, **_k: "sdp")
    monkeypatch.setattr(module, "async_commit_runtime_answer", AsyncMock(return_value=AnswerCommitResult(True, True)))
    binder = SimpleNamespace(attach=MagicMock())
    monkeypatch.setattr(module, "BridgeMediaUpdateBinder", lambda _: binder)
    relays = []
    peers = []
    for _ in range(2):
        peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        peer.bind(("127.0.0.1", 0))
        peer.setblocking(False)
        peers.append(peer)

    def build(**kwargs):
        fmt = AudioFormat(16000, "s16le", 1, 20)
        relay = SipRtpRelay(
            left=RtpPeer("127.0.0.1", peers[0].getsockname()[1], 96, fmt),
            right=RtpPeer("127.0.0.1", peers[1].getsockname()[1], 96, fmt),
            left_port=0, right_port=0, on_release=kwargs["on_release"],
        )
        relays.append(relay)
        return relay

    monkeypatch.setattr(module, "build_invite_client_relay", build)
    monkeypatch.setattr(module, "build_local_client_relay", build)
    data = module.BridgeCommitData(
        invite=invite, winner=winner, source_relay_port=0, dest_relay_port=0,
        local_ip="127.0.0.1", release_port_pairs=(winner.ports.ports,),
        detach_reservations=(winner.ports,),
    )
    try:
        yield SimpleNamespace(module=module, invite=invite, winner=winner, registry=registry,
                              session=session, pool=pool, releases=releases, binder=binder,
                              relays=relays, peers=peers, data=data)
    finally:
        for peer in peers:
            peer.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["build", "adopt", "bind", "start", "ended", "successor", "cancel"])
async def test_bridge_failures_release_owned_inventory(owned_bridge, monkeypatch, failure):
    h = owned_bridge
    module = h.module
    successor = None
    transports = []
    original_start = module.async_start_sip_bridge_media

    async def start(relay):
        nonlocal successor
        await original_start(relay)
        transports.extend((relay.left_transport, relay.right_transport))
        if failure in {"ended", "successor"}:
            await h.registry.terminate_call_wait(h.invite.call_id, reason="remote_hangup")
            if failure == "successor":
                successor = h.registry.upsert(h.invite.call_id, state="ringing", owner="router")
        elif failure == "cancel":
            raise asyncio.CancelledError
        elif failure == "start":
            raise RuntimeError("media startup failed")
        return False

    monkeypatch.setattr(module, "async_start_sip_bridge_media", start)
    if failure == "build":
        monkeypatch.setattr(module, "build_invite_client_relay", MagicMock(side_effect=RuntimeError("construction failed")))
    elif failure == "adopt":
        monkeypatch.setattr(h.registry, "attach_relay", MagicMock(side_effect=RuntimeError("adoption failed")))
    elif failure == "bind":
        h.binder.attach.side_effect = RuntimeError("binding failed")

    try:
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else RuntimeError):
            await module.async_commit_outbound_bridge(
                MagicMock(), h.registry, h.data,
                module.BridgeCommitPolicy(route_kind="direct", caller="a", callee="b", connected_party="b"),
                session=h.session,
            )
        if failure in {"start", "ended", "successor", "cancel"}:
            assert len(transports) == 2
        assert not h.pool
        assert len(h.releases) == 1
        assert all(t.is_closing() for t in transports)
        assert all(r.left_transport is None and r.right_transport is None for r in h.relays)
        assert h.session.terminated.is_set()
        if successor is not None:
            assert h.registry.get_session(h.invite.call_id) is successor
            assert successor.live and successor.state == "ringing"
        else:
            assert not h.registry.sessions
        assert not _pending_watchers()
    finally:
        if successor is not None:
            await successor.terminate(module.TerminationIntent("test_done"))
        for relay in h.relays:
            await relay.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("local_source,preanswered,video", [(False, False, False), (True, False, False), (False, True, True)])
async def test_bridge_success_keeps_media_until_session_termination(owned_bridge, monkeypatch, local_source, preanswered, video):
    h = owned_bridge
    module = h.module
    video_stop = AsyncMock()
    if video:
        fake_video = SimpleNamespace(
            start=AsyncMock(), stop=video_stop, transcoding=False,
            transcodes_from=lambda _: False,
            left_port=40008,
            left=SimpleNamespace(send_format="jpeg", recv_format="jpeg"),
            right=SimpleNamespace(recv_format=SimpleNamespace(encoding="JPEG")),
        )
        h.winner.video_relay = fake_video
        monkeypatch.setattr(module, "configure_answered_invite_video_relay", lambda *_a, **_k: SimpleNamespace(direction="sendrecv", video_format=None))
        monkeypatch.setattr(module, "async_apply_outbound_video_answer", AsyncMock())
        monkeypatch.setattr(module, "sip_endpoint_manager", lambda _: SimpleNamespace(async_activate_video_reinvite=AsyncMock(return_value=True)))
    try:
        result = await module.async_commit_outbound_bridge(
            MagicMock(), h.registry, h.data,
            module.BridgeCommitPolicy(route_kind="direct", caller="a", callee="b", connected_party="b", local_source=local_source, response_already_sent=preanswered),
            session=h.session,
        )
        assert result is not None
        relay = result.relay
        assert relay.left_transport is not None and relay.right_transport is not None
        assert h.pool and not h.releases
        # Independent wire oracle: both sides receive the exact PCM payload.
        import struct
        loop = asyncio.get_running_loop()
        for side, transport in enumerate((relay.left_transport, relay.right_transport)):
            pcm = (1200 + side * 1000).to_bytes(2, "big", signed=True) * 320
            packet = struct.pack("!BBHII", 0x80, 96, 1, 320, side + 1) + pcm
            await loop.sock_sendto(h.peers[side], packet, transport.get_extra_info("sockname"))
            received = await asyncio.wait_for(loop.sock_recv(h.peers[1 - side], 2048), 2)
            assert received[12:] == pcm
        await h.registry.terminate_call_wait(h.invite.call_id, reason="remote_hangup")
        assert not h.pool and len(h.releases) == 1
        assert relay.left_transport is None and relay.right_transport is None
        assert not h.registry.sessions
        if video:
            video_stop.assert_awaited_once()
    finally:
        await h.session.terminate(module.TerminationIntent("test_done"))
        for relay in h.relays:
            await relay.stop()


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_relay_cleanup(owned_bridge, monkeypatch):
    h = owned_bridge
    module = h.module
    entered_start = asyncio.Event()
    entered_stop = asyncio.Event()
    finish_stop = asyncio.Event()
    original_start = module.async_start_sip_bridge_media

    async def start(relay):
        await original_start(relay)
        stop = relay.stop
        async def slow_stop():
            entered_stop.set()
            await finish_stop.wait()
            await stop()
        relay.stop = slow_stop
        entered_start.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(module, "async_start_sip_bridge_media", start)
    task = asyncio.create_task(module.async_commit_outbound_bridge(
        MagicMock(), h.registry, h.data,
        module.BridgeCommitPolicy(route_kind="direct", caller="a", callee="b", connected_party="b"),
        session=h.session,
    ))
    try:
        await asyncio.wait_for(entered_start.wait(), 2)
        task.cancel()
        await asyncio.wait_for(entered_stop.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and h.pool
        finish_stop.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert not h.pool and len(h.releases) == 1
        assert not h.registry.sessions
        assert all(r.left_transport is None and r.right_transport is None for r in h.relays)
    finally:
        finish_stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await h.session.terminate(module.TerminationIntent("test_done"))
