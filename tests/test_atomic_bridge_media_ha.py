"""Joint bridge updates publish audio only after the video owner accepts."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.test_source_video_updates_ha import _dialog

pytestmark = pytest.mark.ha


def _invite(dialog):
    from custom_components.voip_stack.core import sdp, sip
    from custom_components.voip_stack.sip_listener import SipInvite
    body = sdp.build_offer_directional('127.0.0.1', '127.0.0.1', dialog.remote_rtp_port,
        [dialog.send_format.audio_format], [dialog.recv_format.audio_format],
        video_port=dialog.remote_video_rtp_port, video_formats=(dialog.video_format,),
        audio_direction=dialog.remote_audio_direction, video_direction=dialog.video_format.direction)
    return SipInvite(source_host='127.0.0.1', source_port=5060,
        request_uri=sip.parse_sip_uri(dialog.local_uri), caller_uri=sip.parse_sip_uri(dialog.remote_uri),
        target='retained', caller='peer', call_id=dialog.call_id, cseq='2 INVITE', remote_sdp=body.encode(),
        send_format=dialog.send_format, recv_format=dialog.recv_format,
        remote_rtp_host=dialog.remote_rtp_host, remote_rtp_port=dialog.remote_rtp_port,
        local_audio_direction=dialog.local_audio_direction, remote_audio_direction=dialog.remote_audio_direction,
        video_format=dialog.video_format, local_video_format=dialog.recv_video_format,
        video_answer_format=dialog.recv_video_format, remote_video_rtp_host=dialog.remote_video_rtp_host,
        remote_video_rtp_port=dialog.remote_video_rtp_port, remote_video_rtcp_host='127.0.0.1',
        remote_video_rtcp_port=dialog.remote_video_rtcp_port)


@pytest.mark.parametrize('branch', ['retained_left', 'retained_right', 'destination', 'uas_right'])
@pytest.mark.parametrize('outcome', ['success', 'video_failure', 'cancel', 'stale_audio', 'stale_owner', 'stale_generation'])
async def test_joint_update_does_not_publish_audio_during_video_await(monkeypatch, branch, outcome):
    from custom_components.voip_stack import bridge_media_updates as binder, media_renegotiation as uas, call_projection
    from custom_components.voip_stack.core.sip_dialog import apply_remote_offer_media
    from custom_components.voip_stack.sip_bridge import dialog_rtp_peer, dialog_video_rtp_peer
    from custom_components.voip_stack.sip_rtp_bridge import SipRtpRelay
    from custom_components.voip_stack.sip_video_relay import SipVideoRtpRelay

    previous, other = _dialog(), _dialog(call_id='other-dialog')
    updated = replace(previous, remote_rtp_port=45000,
        remote_audio_direction='sendonly', local_audio_direction='recvonly',
        video_format=replace(previous.video_format, direction='sendonly') if branch == 'uas_right' else previous.video_format)
    side = 'left' if branch == 'retained_left' else 'right'
    relay = SipRtpRelay(left=dialog_rtp_peer(previous if side == 'left' else other),
        right=dialog_rtp_peer(previous if side == 'right' else other), left_port=42000, right_port=42002)
    video = SipVideoRtpRelay(left=dialog_video_rtp_peer(previous if side == 'left' else other),
        right=dialog_video_rtp_peer(previous if side == 'right' else other), left_port=44000, right_port=44002)
    video.started = True
    relay.attach_video_relay(video)
    client = SimpleNamespace(dialog=previous, dialog_ids=SimpleNamespace(call_id=previous.call_id))
    session = SimpleNamespace(call_id='logical-source', live=True, generation=7, metadata={}, state_changed=Mock())
    current, generations = [relay], [True]
    registry = SimpleNamespace(sessions={session.call_id: session},
        get_session=lambda _: session, resolve_session_id=lambda _: session.call_id,
        is_generation_current=lambda *_: generations[0],
        resource_for=lambda _call, kind: current[0] if kind == 'relay' else None)
    monkeypatch.setattr(binder, 'call_registry', lambda _: registry)
    monkeypatch.setattr(uas, '_call_registry', lambda _: registry)
    monkeypatch.setattr(call_projection, 'publish_bridge_projection', Mock())
    monkeypatch.setattr(call_projection, 'publish_esp_media_route', Mock())
    real_prepare = binder.prepare_owned_video_generation
    entered, release = asyncio.Event(), asyncio.Event()

    async def prepare_with_barrier(*args, **kwargs):
        commit, rollback = await real_prepare(*args, **kwargs)
        async def commit_after_barrier():
            entered.set()
            await release.wait()
            await commit()
        return commit_after_barrier, rollback

    monkeypatch.setattr(binder, 'prepare_owned_video_generation', prepare_with_barrier)
    previous_audio, previous_video = getattr(relay, side), getattr(video, side)
    replacement = replace(previous_audio, port=46000)
    replacement_owner = object()
    task = None
    try:
        if branch.startswith('retained'):
            binder.BridgeMediaUpdateBinder(object()).attach_audio_side(client, relay,
                source_call_id=session.call_id, side=side)
            prepared = await client.on_media_update(previous, updated, 'INVITE')
        elif branch == 'destination':
            binder.BridgeMediaUpdateBinder(object()).attach(client, relay,
                source_call_id=session.call_id, source_client=SimpleNamespace(dialog=other))
            prepared = await client.on_media_update(previous, updated, 'INVITE')
        else:
            prepared = await uas.async_prepare_media_update(object(), '127.0.0.1', _invite(previous), _invite(updated), 'INVITE')
            assert prepared.status == 200
        assert prepared is not None and getattr(relay, side) is previous_audio
        task = asyncio.create_task(apply_remote_offer_media(prepared.commit, prepared.rollback))
        await asyncio.wait_for(entered.wait(), 1)
        inflight_audio = getattr(relay, side)
        if outcome == 'video_failure':
            video._staged_peers.pop('right')
        elif outcome == 'stale_audio':
            setattr(relay, side, replacement)
        elif outcome == 'stale_owner':
            current[0] = replacement_owner
        elif outcome == 'stale_generation':
            generations[0] = False
        if outcome == 'cancel':
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            accepted = False
        else:
            release.set()
            accepted = await asyncio.wait_for(task, 1)
        assert inflight_audio is previous_audio, 'audio became visible before video accepted'
        if outcome == 'success':
            assert accepted and getattr(relay, side).port == 45000
        else:
            assert not accepted
            assert getattr(relay, side) is (replacement if outcome == 'stale_audio' else previous_audio)
            if outcome not in {'stale_owner', 'stale_generation'}:
                assert getattr(video, side) is previous_video
        assert client.dialog is previous
        assert current[0] is (replacement_owner if outcome == 'stale_owner' else relay)
        assert not video._staged_peers
    finally:
        release.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await video.stop()


@pytest.mark.parametrize('change', ['restore', 'owner', 'generation', 'peer', 'stage',
    'restore_failure', 'owner_during_restore', 'stage_during_restore'])
async def test_committed_video_compensation_respects_the_current_owner(monkeypatch, change):
    from custom_components.voip_stack.bridge_media_updates import prepare_owned_video_generation
    from custom_components.voip_stack.sip_bridge import dialog_video_rtp_peer
    from custom_components.voip_stack.sip_video_relay import SipVideoRtpRelay, VideoTranscodeRestoreError

    old_left, old_right = dialog_video_rtp_peer(_dialog()), dialog_video_rtp_peer(_dialog(call_id='other'))
    left, right = replace(old_left, port=45000), replace(old_right, port=45002)
    video = SipVideoRtpRelay(left=old_left, right=old_right, left_port=44000, right_port=44002)
    video.started = True
    owner = SimpleNamespace(video_relay=video)
    current, generation = [owner], [True]
    terminate = Mock()
    registry = SimpleNamespace(resource_for=lambda *_: current[0],
        is_generation_current=lambda *_: generation[0], request_termination=terminate)
    session = SimpleNamespace(call_id='owned', generation=7)
    video.stage_peer_reconfiguration('left', left)
    video.stage_peer_reconfiguration('right', right)
    commit, rollback = await prepare_owned_video_generation(registry, session, video,
        left=left, right=right, compensate_committed=True)
    foreign = replace(right, port=46000)
    try:
        await commit()
        assert video.left is left and video.right is right
        if change == 'owner':
            current[0] = object()
        elif change == 'generation':
            generation[0] = False
        elif change == 'peer':
            video.right = foreign
        elif change == 'stage':
            video.stage_peer_reconfiguration('right', foreign)
        elif change == 'restore_failure':
            async def fail_restore(**_kwargs):
                raise RuntimeError('injected prior generation restore failure')
            monkeypatch.setattr(video, 'async_prepare_peer_generation', fail_restore)
        elif change.endswith('_during_restore'):
            native_prepare = video.async_prepare_peer_generation
            async def changed_during_restore(**kwargs):
                prepared = await native_prepare(**kwargs)
                if change == 'owner_during_restore':
                    current[0] = object()
                else:
                    video._staged_peers.pop('right')
                    video.stage_peer_reconfiguration('right', foreign)
                return prepared
            monkeypatch.setattr(video, 'async_prepare_peer_generation', changed_during_restore)
        if change == 'restore_failure':
            with pytest.raises(VideoTranscodeRestoreError):
                await rollback()
            terminate.assert_called_once()
            assert terminate.call_args.kwargs == {'generation': 7}
            assert terminate.call_args.args[0] == 'owned'
        else:
            await rollback()
            terminate.assert_not_called()
        if change == 'restore':
            assert video.left is old_left and video.right is old_right
        elif change == 'peer':
            assert video.left is left and video.right is foreign
        else:
            assert video.left is left and video.right is right
        if change in {'stage', 'stage_during_restore'}:
            assert video._staged_peers['right'].peer is foreign
        else:
            assert not video._staged_peers
    finally:
        await video.stop()
