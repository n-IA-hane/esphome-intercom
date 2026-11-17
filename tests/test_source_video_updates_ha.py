"""Retained UAC/UAS video updates use the current dialog and relay owners."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("phase", ["prepare", "rollback"])
@pytest.mark.parametrize("ownership", ["current", "replaced", "stale_generation"])
async def test_unrecoverable_converter_restore_terminates_only_current_owner(phase, ownership):
    from custom_components.voip_stack.bridge_media_updates import prepare_owned_video_generation
    from custom_components.voip_stack.sip_video_relay import VideoTranscodeRestoreError
    session = SimpleNamespace(call_id="logical-video", generation=7)
    video = SimpleNamespace()
    owner = SimpleNamespace(video_relay=video)
    current, generation_current = [owner], [True]
    registry = SimpleNamespace(resource_for=lambda *_: current[0],
        is_generation_current=lambda *_: generation_current[0], request_termination=Mock())

    async def fail():
        if ownership == "replaced":
            current[0] = SimpleNamespace(video_relay=video)
        elif ownership == "stale_generation":
            generation_current[0] = False
        raise VideoTranscodeRestoreError("replacement and previous encoder both failed")

    async def prepare(**_kwargs):
        if phase == "prepare":
            await fail()
        return AsyncMock(), fail

    video.async_prepare_peer_generation = prepare
    with pytest.raises(VideoTranscodeRestoreError):
        _commit, rollback = await prepare_owned_video_generation(registry, session, video,
            left=object(), right=object())
        await rollback()
    if ownership == "current":
        registry.request_termination.assert_called_once()
        assert registry.request_termination.call_args.args[0] == session.call_id
        assert registry.request_termination.call_args.kwargs == {"generation": 7}
    else:
        registry.request_termination.assert_not_called()


class _AnswerPeer(asyncio.DatagramProtocol):
    """Independent localhost SIP answerer, with every PDU retained in the test."""

    def __init__(self):
        self.requests = []
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, raw, addr):
        from custom_components.voip_stack.core import sip, sdp
        message = sip.parse_message(raw)
        if message.is_response:
            return
        self.requests.append(message)
        if message.method == "ACK":
            return
        headers = [
            *(("Via", value) for value in message.header_values("Via")),
            ("From", message.header("From")),
            ("To", message.header("To") if ";tag=" in message.header("To") else message.header("To") + ";tag=owned-peer"),
            ("Call-ID", message.header("Call-ID")), ("CSeq", message.header("CSeq")),
            ("Contact", "<sip:peer@127.0.0.1:%d>" % self.transport.get_extra_info("sockname")[1]),
        ]
        body = ""
        if message.method == "INVITE":
            audio = sdp.offered_pcm_formats(message.body)[0]
            video = sdp.offered_video_formats(message.body)[0]
            body = sdp.build_answer_directional(
                "127.0.0.1", "127.0.0.1", 41000, audio, audio,
                remote_sdp=message.body, video_port=43000, video_format=video,
                video_direction=sdp.local_direction_for_remote(video.direction),
            )
            headers.append(("Content-Type", "application/sdp"))
        self.transport.sendto(sip.build_response(200, "OK", headers, body.encode()), addr)


@pytest.fixture
async def source_uac(socket_enabled, request):
    from custom_components.voip_stack.sip_client import SipCallClient
    from custom_components.voip_stack.core.audio_format import AudioFormat
    from custom_components.voip_stack.core.sdp import RtpVideoFormat
    transport, peer = await asyncio.get_running_loop().create_datagram_endpoint(
        _AnswerPeer, local_addr=("127.0.0.1", 0),
    )
    client = SipCallClient(
        local_ip="127.0.0.1", local_name="owned-source", local_sip_port=0,
        local_rtp_port=42000, local_video_rtp_port=44000,
        video_formats=(RtpVideoFormat(payload_type=103, encoding="H264"),),
        video_direction=getattr(request, "param", "sendrecv"),
        supported_formats=(AudioFormat(16000, "s16le", 1, 10),),
    )
    watcher = None
    try:
        result = await client.invite(
            target="peer", remote_host="127.0.0.1",
            remote_sip_port=transport.get_extra_info("sockname")[1], timeout=1,
        )
        assert result == "in_call"
        watcher = asyncio.create_task(client.wait_for_dialog_termination())
        yield client, peer
    finally:
        if client.dialog is not None:
            await client.terminate(timeout=.5)
        await client.close()
        if watcher is not None:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        transport.close()


@pytest.mark.parametrize("source_uac", ["sendrecv", "sendonly", "recvonly"], indirect=True)
async def test_uac_source_update_and_compensation_keep_one_dialog_on_real_udp(source_uac):
    from custom_components.voip_stack.bridge_media_updates import prepare_source_video_update
    from custom_components.voip_stack.core import sdp
    client, peer = source_uac
    previous = client.dialog
    prepared = await prepare_source_video_update(
        object(), "logical-parking-alias", source_client=client,
        local_video_rtp_port=44000, video_formats=(previous.video_format,),
        video_direction="inactive", timeout=1,
    )
    assert prepared is not None and client.dialog is previous
    assert prepared.candidate.local_video_direction == "inactive"
    assert prepared.commit() and client.dialog is prepared.candidate
    assert await prepared.restore(local_video_rtp_port=44000, video_formats=(previous.video_format,))
    assert client.dialog.local_video_direction == previous.local_video_direction
    invites = [m for m in peer.requests if m.method == "INVITE"]
    assert len(invites) == 3
    assert len({m.header("Call-ID") for m in invites}) == 1
    assert [sdp.offered_video_formats(m.body)[0].direction for m in invites] == [previous.local_video_direction, "inactive", previous.local_video_direction]
    audio = [m.body.split(b"m=video", 1)[0].split(b"m=audio", 1)[1] for m in invites]
    assert audio[0] == audio[1] == audio[2]
    assert not any(m.method == "BYE" for m in peer.requests)


async def test_uac_compensation_does_not_touch_a_newer_dialog(source_uac):
    from custom_components.voip_stack.bridge_media_updates import prepare_source_video_update
    client, peer = source_uac
    previous = client.dialog
    prepared = await prepare_source_video_update(
        object(), "logical-alias", source_client=client,
        local_video_rtp_port=44000, video_formats=(previous.video_format,),
        video_direction="inactive", timeout=1,
    )
    assert prepared is not None
    newer = replace(previous, remote_rtp_port=41002)
    client.dialog = newer
    assert not prepared.commit()
    assert not await prepared.restore(local_video_rtp_port=44000, video_formats=(previous.video_format,))
    assert client.dialog is newer
    assert len([m for m in peer.requests if m.method == "INVITE"]) == 2


def _dialog(direction="sendrecv", *, call_id="wire-source"):
    from custom_components.voip_stack.core import sdp
    from custom_components.voip_stack.sip_client import SipDialog
    audio = sdp.RtpPcmFormat(payload_type=0, encoding="PCMU", sample_rate=8000, channels=1)
    video = sdp.RtpVideoFormat(payload_type=103, encoding="H264", direction=direction)
    return SipDialog(
        target="peer", remote_host="127.0.0.1", remote_sip_port=5060,
        remote_rtp_host="127.0.0.1", remote_rtp_port=41000,
        local_rtp_port=42000, call_id=call_id,
        local_uri="sip:ha@localhost", remote_uri="sip:peer@localhost",
        send_format=audio, recv_format=audio, video_format=video,
        local_video_format=video, remote_video_rtp_host="127.0.0.1",
        remote_video_rtp_port=43000, remote_video_rtcp_port=43001,
        local_video_rtp_port=44000,
        local_video_direction=sdp.local_direction_for_remote(direction),
    )


@pytest.mark.parametrize("remote,expected", [("sendonly", "recvonly"), ("recvonly", "sendonly")])
async def test_uas_compensation_restores_local_one_way_direction(remote, expected):
    from custom_components.voip_stack.sip_endpoint import PreparedEndpointVideoReinvite
    previous, candidate, restored = _dialog(remote), _dialog("inactive"), _dialog(remote)
    active = SimpleNamespace(invite=previous)

    def commit(_call_id, old, new):
        if active.invite is not old:
            return False
        active.invite = new
        return True

    endpoint = SimpleNamespace(
        active_dialogs={"wire-source": active}, commit_prepared_reinvite=commit,
        async_prepare_video_reinvite=AsyncMock(return_value=restored),
    )
    prepared = PreparedEndpointVideoReinvite(endpoint, "wire-source", previous, candidate)
    assert await prepared.restore(local_video_rtp_port=44000, video_formats=(previous.video_format,))
    assert endpoint.async_prepare_video_reinvite.await_args.kwargs["video_direction"] == expected
    assert active.invite is restored


async def test_uas_compensation_never_overwrites_a_newer_dialog():
    from custom_components.voip_stack.sip_endpoint import PreparedEndpointVideoReinvite
    previous, candidate, newer = _dialog(), _dialog("inactive"), _dialog("recvonly")
    active = SimpleNamespace(invite=candidate)
    endpoint = SimpleNamespace(
        active_dialogs={"wire-source": active}, commit_prepared_reinvite=Mock(return_value=True),
        async_prepare_video_reinvite=AsyncMock(return_value=newer),
    )
    prepared = PreparedEndpointVideoReinvite(endpoint, "wire-source", previous, candidate, committed=True)
    active.invite = newer
    assert not await prepared.restore(local_video_rtp_port=44000, video_formats=(previous.video_format,))
    endpoint.async_prepare_video_reinvite.assert_not_awaited()


async def test_uas_compensation_keeps_local_policy_when_remote_offer_was_sendrecv():
    from custom_components.voip_stack.sip_endpoint import SipEndpointManager
    previous, candidate, restored = _dialog(), _dialog("inactive"), _dialog()
    active = SimpleNamespace(invite=previous, local_sdp_body=(
        "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
        "m=video 44000 RTP/AVP 103\r\na=rtpmap:103 H264/90000\r\na=recvonly\r\n"
    ), answer_sdp="")

    def commit(_call_id, old, new):
        if active.invite is not old:
            return False
        active.invite = new
        return True

    endpoint = SimpleNamespace(active_dialogs={"wire-source": active},
        async_prepare_video_reinvite=AsyncMock(side_effect=[candidate, restored]),
        commit_prepared_reinvite=commit)
    manager = SipEndpointManager.__new__(SipEndpointManager)
    manager._dialog_endpoints = lambda: [endpoint]
    prepared = await manager.async_prepare_video_reinvite("wire-source", video_direction="inactive")
    assert prepared is not None and prepared.commit()
    assert await prepared.restore(local_video_rtp_port=44000, video_formats=(previous.video_format,))
    assert endpoint.async_prepare_video_reinvite.await_args.kwargs["video_direction"] == "recvonly"


async def test_explicit_uac_accepts_logical_alias_but_rejects_wrong_dialog_identity():
    from custom_components.voip_stack.bridge_media_updates import prepare_source_video_update
    previous, candidate = _dialog(), _dialog("inactive")
    client = SimpleNamespace(
        dialog=previous, dialog_ids=SimpleNamespace(call_id="wire-source"),
        async_prepare_video_reinvite=AsyncMock(return_value=candidate),
    )
    prepared = await prepare_source_video_update(object(), "logical-park-alias", source_client=client)
    assert prepared is not None and prepared.previous is previous
    client.async_prepare_video_reinvite.assert_awaited_once_with(expected_dialog=previous)
    client.async_prepare_video_reinvite.reset_mock()
    client.dialog_ids.call_id = "replacement-wire-dialog"
    assert await prepare_source_video_update(object(), "logical-park-alias", source_client=client) is None
    client.async_prepare_video_reinvite.assert_not_awaited()


@pytest.mark.parametrize("side", ["left", "right"])
@pytest.mark.parametrize("direction", ["inactive", "sendonly", "recvonly", "sendrecv"])
@pytest.mark.parametrize("outcome", ["commit", "rollback", "stale_generation", "stale_dialog", "stale_callback"])
async def test_caller_uac_retained_video_uses_staged_generation(monkeypatch, side, direction, outcome):
    from custom_components.voip_stack import bridge_media_updates as module, call_projection
    from custom_components.voip_stack.sip_bridge import dialog_rtp_peer, dialog_video_rtp_peer
    from custom_components.voip_stack.sip_rtp_bridge import SipRtpRelay
    from custom_components.voip_stack.sip_video_relay import SipVideoRtpRelay
    previous, other = _dialog(), _dialog(call_id="wire-destination")
    updated = replace(previous, video_format=replace(previous.video_format, direction=direction))
    relay = SipRtpRelay(left=dialog_rtp_peer(previous), right=dialog_rtp_peer(other), left_port=0, right_port=0)
    video = SipVideoRtpRelay(left=dialog_video_rtp_peer(previous), right=dialog_video_rtp_peer(other), left_port=44000, right_port=44002)
    video.started = True
    relay.attach_video_relay(video)
    client = SimpleNamespace(dialog=previous)
    session = SimpleNamespace(call_id="logical-source", live=True, generation=7, state_changed=Mock())
    current = [True]
    registry = SimpleNamespace(
        get_session=lambda _: session, is_generation_current=lambda *_: current[0],
        resource_for=lambda *_: relay,
    )
    monkeypatch.setattr(module, "call_registry", lambda _: registry)
    monkeypatch.setattr(call_projection, "publish_bridge_projection", Mock())
    module.BridgeMediaUpdateBinder(object()).attach_audio_side(client, relay, source_call_id="logical-source", side=side)
    old_peer = getattr(video, side)
    old_audio = getattr(relay, side)
    try:
        prepared = await client.on_media_update(previous, updated, "INVITE")
        assert prepared is not None
        assert getattr(video, side) is old_peer
        if outcome != "commit":
            if outcome == "stale_generation":
                current[0] = False
            elif outcome == "stale_dialog":
                client.dialog = replace(previous, remote_rtp_port=41002)
            elif outcome == "stale_callback":
                client.on_media_update = object()
            if outcome != "rollback":
                with pytest.raises(RuntimeError, match="ownership changed"):
                    await prepared.commit()
            await prepared.rollback()
            assert getattr(video, side) is old_peer
            assert getattr(relay, side) is old_audio
            assert not video._staged_peers
            session.state_changed.assert_not_called()
            return
        await prepared.commit()
        assert getattr(video, side).video_format.direction == direction
        assert not video._staged_peers
        assert video._transcode.transcoders == {}
        client.dialog = updated
        prepared.on_committed(updated)
        session.state_changed.assert_called_once()
    finally:
        await video.stop()


@pytest.mark.parametrize("rollback", [False, True])
async def test_destination_resume_negotiates_retained_uac_source_on_wire(monkeypatch, source_uac, rollback):
    from custom_components.voip_stack import bridge_media_updates as module, call_projection
    from custom_components.voip_stack.sip_bridge import dialog_rtp_peer, dialog_video_rtp_peer
    from custom_components.voip_stack.sip_rtp_bridge import SipRtpRelay
    from custom_components.voip_stack.sip_video_relay import SipVideoRtpRelay
    source, peer = source_uac
    active = source.dialog
    hold = await module.prepare_source_video_update(
        object(), "logical-source", source_client=source,
        local_video_rtp_port=44000, video_formats=(active.video_format,),
        video_direction="inactive", timeout=1,
    )
    assert hold is not None and hold.commit()
    previous, updated = _dialog("inactive", call_id="destination"), _dialog(call_id="destination")
    client = SimpleNamespace(dialog=previous, dialog_ids=SimpleNamespace(call_id="destination"))
    relay = SipRtpRelay(left=dialog_rtp_peer(source.dialog), right=dialog_rtp_peer(previous), left_port=0, right_port=0)
    video = SipVideoRtpRelay(left=dialog_video_rtp_peer(source.dialog), right=dialog_video_rtp_peer(previous), left_port=44000, right_port=44002)
    video.started = True
    relay.attach_video_relay(video)
    session = SimpleNamespace(call_id="logical-source", live=True, generation=7, state_changed=Mock())
    registry = SimpleNamespace(
        sessions={"logical-source": session}, resolve_session_id=lambda key: key,
        is_generation_current=lambda *_: True, resource_for=lambda *_: relay,
    )
    monkeypatch.setattr(module, "call_registry", lambda _: registry)
    monkeypatch.setattr(module, "sip_endpoint_manager", lambda _: pytest.fail("UAC source has no UAS endpoint"))
    monkeypatch.setattr(call_projection, "publish_esp_media_route", Mock())
    module.BridgeMediaUpdateBinder(object()).attach(client, relay, source_call_id="logical-source", source_client=source)
    try:
        prepared = await client.on_media_update(previous, updated, "INVITE")
        assert prepared is not None
        assert source.dialog.local_video_direction == "sendrecv"
        assert video.left.video_format.direction == "inactive"
        if rollback:
            await prepared.rollback()
            assert source.dialog.local_video_direction == "inactive"
            assert video.left.video_format.direction == "inactive"
            assert video.right.video_format.direction == "inactive"
        else:
            await prepared.commit()
            assert video.left.video_format.direction == "sendrecv"
            assert video.right.video_format.direction == "sendrecv"
        assert not video._staged_peers
        invites = [m for m in peer.requests if m.method == "INVITE"]
        assert len(invites) == (4 if rollback else 3)
        assert len({m.header("Call-ID") for m in invites}) == 1
    finally:
        await video.stop()


async def test_preanswered_bridge_activates_explicit_uac_source_without_uas_lookup(monkeypatch):
    from custom_components.voip_stack import outbound_bridge_commit as module, sip_bridge, sip_rtp_bridge, call_projection
    from custom_components.voip_stack.inbound_answer import AnswerCommitResult
    from tests.test_outbound_bridge_commit import _fixture
    invite, winner, registry, relay = _fixture()
    source = SimpleNamespace(dialog=object(), dialog_ids=SimpleNamespace(call_id="retained-wire-call"))
    video = SimpleNamespace(left=SimpleNamespace(recv_format=object(), send_format=object()), left_port=40008)
    winner.video_relay = video
    relay.attach_video_relay.side_effect = lambda resource: setattr(relay, "video_relay", resource)
    binder = SimpleNamespace(attach=Mock(), attach_audio_side=Mock())
    prepared = SimpleNamespace(commit=Mock(return_value=True))
    prepare = AsyncMock(return_value=prepared)
    monkeypatch.setattr(module, "prepare_source_video_update", prepare)
    monkeypatch.setattr(module, "sip_endpoint_manager", lambda _: pytest.fail("UAC source cannot use UAS lookup"))
    monkeypatch.setattr(sip_bridge, "dialog_rtp_peer", lambda value: value)
    monkeypatch.setattr(sip_rtp_bridge, "SipRtpRelay", lambda **_: relay)
    monkeypatch.setattr(module, "BridgeMediaUpdateBinder", lambda _: binder)
    monkeypatch.setattr(module, "configure_answered_invite_video_relay", lambda *_a, **_k: SimpleNamespace(direction="sendrecv"))
    monkeypatch.setattr(module, "async_apply_outbound_video_answer", AsyncMock())
    monkeypatch.setattr(module, "async_start_sip_bridge_media", AsyncMock(return_value=False))
    monkeypatch.setattr(module, "attach_dtmf_event_bridge", Mock())
    monkeypatch.setattr(module, "async_commit_runtime_answer", AsyncMock(return_value=AnswerCommitResult(True, True)))
    monkeypatch.setattr(module, "async_watch_sip_bridge_destination", AsyncMock())
    monkeypatch.setattr(call_projection, "publish_esp_media_route", Mock())
    hass = object()
    result = await module.async_commit_outbound_bridge(
        hass, registry,
        module.BridgeCommitData(invite=invite, winner=winner, source_relay_port=40000,
            dest_relay_port=40002, local_ip="127.0.0.1", release_port_pairs=(winner.ports.ports,),
            detach_reservations=(winner.ports,), source_client=source),
        module.BridgeCommitPolicy(route_kind="pickup", caller="source", callee="peer",
            connected_party="peer", response_already_sent=True),
    )
    assert result is not None
    prepare.assert_awaited_once_with(hass, invite.call_id,
        source_client=source, local_video_rtp_port=40008,
        video_formats=(video.left.recv_format, video.left.send_format), video_direction="sendrecv")
    prepared.commit.assert_called_once_with()
    assert binder.attach.call_args.kwargs["source_client"] is source


@pytest.mark.parametrize("protocol", ["uac", "uas"])
@pytest.mark.parametrize("preanswered", [False, True])
@pytest.mark.parametrize("outcome", ["commit", "rollback", "stale_generation", "stale_video_peer", "stale_media_revision", "prepare_failure"])
async def test_parked_source_reoffer_keeps_child_inactive_and_updates_audio_atomically(
    monkeypatch, protocol, preanswered, outcome,
):
    from custom_components.voip_stack import local_application as local, media_renegotiation as uas
    from custom_components.voip_stack.local_call_media import ApplicationCallSource, LocalCallMedia
    from custom_components.voip_stack.sip_bridge import dialog_video_rtp_peer
    from custom_components.voip_stack.sip_video_relay import SipVideoRtpRelay
    from custom_components.voip_stack.sip_listener import SipInvite
    from custom_components.voip_stack.core import sip, sdp
    previous = _dialog("inactive")
    video_format = replace(previous.video_format, payload_type=105, direction="sendrecv")
    updated = replace(previous, remote_audio_direction="sendonly", local_audio_direction="recvonly",
        video_format=video_format, local_video_format=video_format,
        remote_video_rtp_port=45000, remote_video_rtcp_port=45001)
    canonical = "logical-park" if protocol == "uac" else previous.call_id

    def invite(dialog):
        body = sdp.build_offer_directional("127.0.0.1", "127.0.0.1", dialog.remote_rtp_port,
            [dialog.send_format.audio_format], [dialog.recv_format.audio_format],
            video_port=dialog.remote_video_rtp_port, video_formats=(dialog.video_format,),
            audio_direction=dialog.remote_audio_direction, video_direction=dialog.video_format.direction)
        return SipInvite(source_host="127.0.0.1", source_port=5060,
            request_uri=sip.parse_sip_uri(dialog.local_uri), caller_uri=sip.parse_sip_uri(dialog.remote_uri),
            target="701", caller="retained", call_id=dialog.call_id, cseq="2 INVITE", remote_sdp=body.encode(),
            send_format=dialog.send_format, recv_format=dialog.recv_format,
            remote_rtp_host=dialog.remote_rtp_host, remote_rtp_port=dialog.remote_rtp_port,
            local_audio_direction=dialog.local_audio_direction, remote_audio_direction=dialog.remote_audio_direction,
            video_format=dialog.video_format, local_video_format=dialog.recv_video_format,
            video_answer_format=dialog.recv_video_format, remote_video_rtp_host=dialog.remote_video_rtp_host,
            remote_video_rtp_port=dialog.remote_video_rtp_port, remote_video_rtcp_host="127.0.0.1",
            remote_video_rtcp_port=dialog.remote_video_rtcp_port)

    source = (ApplicationCallSource.from_peer_dialog(previous, call_id=canonical, caller="retained", allow_video=True)
              if protocol == "uac" else invite(previous))
    reservation = SimpleNamespace(ports=(42000, 42002), release=Mock())
    media = LocalCallMedia(object(), invite=source, local_rtp_port=42000, reservation=reservation, on_complete=AsyncMock())
    held = dialog_video_rtp_peer(previous)
    video = SipVideoRtpRelay(left=held, right=replace(held, port=0, rtcp_port=0), left_port=44000, right_port=44002)
    video.started = True
    media.video_relay = video
    client = SimpleNamespace(dialog=previous, dialog_ids=SimpleNamespace(call_id=previous.call_id))
    session = SimpleNamespace(call_id=canonical, live=True, generation=7, caller="retained", callee="Parking701", metadata={})
    application = SimpleNamespace(hass=object(), session=session, media=media, source_client=client,
        invite=source, video_source=dialog_video_rtp_peer(_dialog()))
    current, pending = [True], [source]
    provisional = {"local_rtp_port": 42000, "rtp_reservation": reservation} if preanswered else None
    registry = SimpleNamespace(sessions={canonical: session}, resolve_session_id=lambda _: canonical,
        is_generation_current=lambda *_: current[0], artifact_for=lambda *_: pending[0],
        set_pending_invite=lambda _call, value: pending.__setitem__(0, value),
        resource_for=lambda _call, kind: {"relay": media, "parking": application, "preanswered": provisional}.get(kind))
    monkeypatch.setattr(local, "call_registry", lambda _: registry)
    monkeypatch.setattr(uas, "_call_registry", lambda _: registry)
    monkeypatch.setattr(uas, "reserve_sip_video_media", lambda _: pytest.fail("parked child must not allocate new video ports"))
    if outcome == "prepare_failure":
        if protocol == "uas":
            monkeypatch.setattr(uas, "build_answer_directional", Mock(side_effect=ValueError("injected SDP failure")))
        else:
            video.async_prepare_peer_generation = AsyncMock(side_effect=RuntimeError("injected stage failure"))
    old_left, old_right, old_source = video.left, video.right, application.video_source
    try:
        if protocol == "uac":
            local.bind_application_source_dialog(application)
            prepared = await client.on_media_update(previous, updated, "INVITE")
            if outcome != "prepare_failure":
                assert prepared is not None and prepared.answer_video_rtp_port == 44000
        else:
            prepared = await uas.async_prepare_media_update(object(), "127.0.0.1", invite(previous), invite(updated), "INVITE")
            if outcome != "prepare_failure":
                assert prepared.status == 200
                section = sdp.parse_video_sdp(prepared.answer_sdp)
                assert section["media_port"] == 44000 and section["direction"] == "inactive"
        assert media.invite is source and media.can_send
        assert video.left is old_left and video.right is old_right
        if outcome == "prepare_failure":
            if protocol == "uac":
                assert prepared is None
            else:
                assert prepared.status == 488
            assert not video._staged_peers
            assert application.video_source is old_source and pending[0] is source
            return
        if outcome != "commit":
            if outcome == "stale_generation":
                current[0] = False
                with pytest.raises(RuntimeError):
                    await prepared.commit()
            elif outcome == "stale_video_peer":
                replacement_left = replace(old_left, port=46000)
                video.left = replacement_left
                with pytest.raises(RuntimeError):
                    await prepared.commit()
            elif outcome == "stale_media_revision":
                replacement_source = replace(source, remote_rtp_port=46002)
                media.invite = replacement_source
                with pytest.raises(RuntimeError):
                    await prepared.commit()
            await prepared.rollback()
            assert media.invite is (replacement_source if outcome == "stale_media_revision" else source)
            assert media.can_send
            assert application.video_source is old_source
            assert video.left is (replacement_left if outcome == "stale_video_peer" else old_left)
            assert video.right is old_right
        else:
            await prepared.commit()
            assert not media.can_send and media.can_receive
            assert video.left.port == 45000
            assert video.left.video_format.direction == video.right.video_format.direction == "inactive"
            assert application.video_source.port == 45000
            assert application.video_source.video_format.direction == "sendrecv"
            assert pending[0] is media.invite
        assert media.video_relay is video and not video._staged_peers
        assert video.left_port == 44000 and video.right_port == 44002
        assert video._transcode.transcoders == {}
    finally:
        await media.stop()


async def test_parked_source_cannot_stage_an_unsupported_codec_while_inactive():
    from custom_components.voip_stack.local_application import prepare_held_application_video_update
    from custom_components.voip_stack.sip_bridge import dialog_video_rtp_peer
    from custom_components.voip_stack.sip_video_relay import SipVideoRtpRelay
    from custom_components.voip_stack.core.sdp import RtpVideoFormat
    held = dialog_video_rtp_peer(_dialog("inactive"))
    video = SipVideoRtpRelay(left=held, right=replace(held), left_port=44000, right_port=44002)
    video.started = True
    media = SimpleNamespace(video_relay=video, closed=asyncio.Event())
    unsupported = RtpVideoFormat(payload_type=105, encoding="VP8")
    try:
        with pytest.raises(ValueError, match="negotiated contract"):
            await prepare_held_application_video_update(media, replace(held,
                video_format=unsupported, local_video_format=unsupported))
        assert not video._staged_peers and not video._transcode.transcoders
    finally:
        await video.stop()
