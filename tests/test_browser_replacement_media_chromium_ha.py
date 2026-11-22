"""An answered real Chromium card follows its authenticated SIP Replaces successor."""

import asyncio
import json
import wave

import pytest

from tests.test_canonical_account_calls_ha import (
    canonical_browser as canonical_browser, logical_browser as logical_browser,
    registered_phones as registered_phones, trunks as trunks, lab as lab,
)
from tests.test_browser_logical_availability_ha import resources
from tests.support.shared_codec_browser import Card
from tests.support.shared_codec_peer import measure_tone
from tests.test_shared_account_codec_media_ha import measure_browser

pytestmark = [pytest.mark.ha, pytest.mark.browser_real]


async def until(predicate, seconds=7):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(.01)


@pytest.mark.parametrize('trunks', [True], indirect=True)
@pytest.mark.parametrize('logical_browser', [{'ringing_timeout': 30}], indirect=True)
@pytest.mark.parametrize('registered_phones', [
    {'audio_codecs': {'transfer_a': 'opus/48000/2', 'transfer_b': 'G722/16000/1'}},
    {'audio_codecs': {'transfer_a': 'opus/48000/2', 'transfer_b': 'G722/16000/1'},
     'video': ('transfer_a', 'transfer_b')},
], ids=['audio', 'receive-video-camera-off'], indirect=True)
async def test_real_card_adopts_replaces_successor_with_duplex_pcm_and_reuse(
    canonical_browser, registered_phones, hass_ws_client, hass_admin_user, tmp_path, monkeypatch,
):
    runtime = canonical_browser
    receive_video = bool(runtime.runtime.transport_config.get('experimental_sip_video'))
    caller, operator = registered_phones[1], registered_phones[0]
    card, spectator = Card(), Card()
    protocol = []
    evidence = {'route': 'SIP G722 caller -> SIP Opus operator -> answered Chromium browser', 'media': {}}
    try:
        await card.start(runtime, hass_ws_client, tmp_path / 'owner', hass_admin_user)
        await spectator.start(runtime, hass_ws_client, tmp_path / 'spectator', hass_admin_user)
        await asyncio.to_thread(caller.dial, 'transfer_a')
        await asyncio.to_thread(operator.wait_for, 'Call established', 5)
        original = runtime.calls.get_session(runtime.runtime.endpoints.require('transfer_a').active_call_id)
        assert original is not None and original.live
        original_relay = runtime.calls.resource_for(original.call_id, 'relay')
        assert original_relay.left.inbound_rtp_format.encoding == 'G722'
        assert original_relay.right.inbound_rtp_format.encoding == 'OPUS'
        from custom_components.voip_stack import sip_listener
        from custom_components.voip_stack.core import sip
        native_receive = sip_listener.SipUdpEndpoint.datagram_received
        def receive(endpoint, data, address):
            message = sip.parse_message(data)
            if message.method == 'ACK':
                protocol.append({'event': 'ack', 'call_id': message.header('Call-ID')})
            return native_receive(endpoint, data, address)
        monkeypatch.setattr(sip_listener.SipUdpEndpoint, 'datagram_received', receive)
        referrer = runtime.calls.sip_client_for(runtime.calls.bridge_link_for(original.call_id))
        native_send = referrer._send_in_dialog_request
        async def send(method, *, extra_headers=(), body=b'', content_type='', timeout=8.0, usage=None):
            response = await native_send(method, extra_headers=extra_headers, body=body,
                content_type=content_type, timeout=timeout, usage=usage)
            if method == 'NOTIFY' and b'SIP/2.0 200' in body and response is not None and response.status_code == 200:
                protocol.append({'event': 'final_notify_acked', 'call_id': referrer.dialog_ids.call_id})
            return response
        monkeypatch.setattr(referrer, '_send_in_dialog_request', send)
        manager = runtime.calls.component('udp_listener')
        native_bye = manager.async_send_bye
        async def bye(call_id=''):
            protocol.append({'event': 'bye', 'call_id': call_id})
            return await native_bye(call_id)
        monkeypatch.setattr(manager, 'async_send_bye', bye)
        offset = len(operator.read())
        await asyncio.to_thread(operator.command, f'/atransferstart sip:221@127.0.0.1:{runtime.runtime.transport_config["sip_port"]}')
        await card.command('wait-answer')
        old_id = runtime.runtime.endpoints.require('casa').active_call_id
        await card.command('answer')
        await until(lambda: 'Call established' in operator.read()[offset:])
        async with asyncio.timeout(8):
            while True:
                before = await card.command('stats')
                if before['card']['callId'] == old_id and before['card']['stats'].get('received', 0) >= 20:
                    break
                await asyncio.sleep(.03)
        if receive_video:
            async with asyncio.timeout(8):
                while not before['card'].get('video', {}).get('rendered', 0):
                    before = await card.command('stats')
                    await asyncio.sleep(.03)
        evidence['before_card'] = before['card']
        assert before['card']['ownedCallId'] == old_id
        expected_client = before['card']['mediaClientId']
        await asyncio.to_thread(operator.command, '/atransferexec')
        await until(lambda: len(runtime.calls.sessions) == 1
                    and not runtime.runtime.endpoints.require('transfer_a').active_call_id)
        successor = next(iter(runtime.calls.sessions.values()))
        assert successor.call_id not in {original.call_id, old_id}
        evidence.update(old_call_id=old_id, new_call_id=successor.call_id)
        async with asyncio.timeout(8):
            while True:
                after = await card.command('stats')
                evidence['after_card'] = after['card']
                if after['card']['callId'] == successor.call_id and after['card']['stats'].get('received', 0) >= 40 and after['card']['stats'].get('sent', 0) >= 40:
                    break
                await asyncio.sleep(.03)
        new_ack = next(index for index, row in enumerate(protocol) if row == {'event': 'ack', 'call_id': successor.call_id})
        final_notify = next(index for index, row in enumerate(protocol) if row['event'] == 'final_notify_acked')
        old_bye = next(index for index, row in enumerate(protocol) if row == {'event': 'bye', 'call_id': old_id})
        assert new_ack < old_bye and final_notify < old_bye
        evidence['protocol'] = protocol
        from custom_components.voip_stack.browser_phone_projection import _ha_softphone_state
        assert _ha_softphone_state(runtime.hass, 'transfer_b').get('media_handoff') is None
        assert after['card']['ownedCallId'] == successor.call_id
        assert after['card']['mediaClientId'] == expected_client
        assert after['media_connections'] == 2 and not after['errors']
        evidence['media']['browser_rx'] = measure_browser(after, successor.call_id, 'rx', 550)
        evidence['media']['browser_tx'] = measure_browser(after, successor.call_id, 'tx', 650)
        if receive_video:
            async with asyncio.timeout(8):
                while not after['card'].get('video', {}).get('rendered', 0):
                    after = await card.command('stats')
                    await asyncio.sleep(.03)
            assert after['card']['videoActive']
            assert after['card']['video'].get('sent', 0) == 0
            assert f'casa|{old_id}' not in runtime.runtime.media.owners_for('video')
            assert f'casa|{successor.call_id}' in runtime.runtime.media.owners_for('video')
            evidence['media']['video'] = after['card']['video']
        watching = await spectator.command('stats')
        assert watching['media_connections'] == 0 and not watching['card']['ownedCallId']
        assert f'casa|{old_id}' not in runtime.runtime.media.owners_for('audio')
        assert f'casa|{successor.call_id}' in runtime.runtime.media.owners_for('audio')
        await card.command('hangup')
        await until(lambda: not runtime.calls.sessions)
        await until(lambda: resources(runtime)['call_scoped_quiescent'])
        await asyncio.to_thread(caller.close)
        decoded = []
        for path in (tmp_path / 'transfer_b').glob('*-dec.wav'):
            with wave.open(str(path), 'rb') as audio:
                pcm, rate = audio.readframes(audio.getnframes()), audio.getframerate()
            try:
                decoded.append(measure_tone(pcm, rate, 650))
            except AssertionError:
                pass
        assert decoded, 'Transferred browser tone must reach the independent G722 caller'
        evidence['media']['independent_caller_rx'] = decoded
        await asyncio.to_thread(registered_phones[2].command, '/dial 221')
        await card.command('wait-answer')
        await card.command('answer')
        await until(lambda: runtime.runtime.endpoints.require('casa').active_call_id)
        await card.command('hangup')
        await until(lambda: not runtime.calls.sessions)
        await until(lambda: resources(runtime)['call_scoped_quiescent'])
        evidence['quiescent_and_reuse'] = True
    finally:
        evidence['protocol'] = protocol
        evidence['http_errors'] = getattr(card, 'http_errors', [])
        from custom_components.voip_stack.browser_phone_projection import _ha_softphone_state
        from custom_components.voip_stack.browser_phone_data import _ha_softphone_store
        evidence['native_snapshot'] = _ha_softphone_state(runtime.hass, 'casa')
        evidence['raw_store'] = dict(_ha_softphone_store(runtime.hass, 'casa'))
        (tmp_path / 'REPLACES_CHROMIUM.json').write_text(json.dumps(evidence, indent=2))
        for peer in registered_phones:
            if peer.proc.poll() is None:
                await asyncio.to_thread(peer.command, '/hangupall')
        for session in list(runtime.calls.sessions.values()):
            await runtime.calls.terminate_call_wait(session.call_id, reason='test_done')
        await card.close()
        await spectator.close()
