"""A rendered Fanvil Casa key picks up the real P4-to-browser SIP route."""

import asyncio
from contextlib import suppress
import json
import math
import os
from pathlib import Path
import socket
import struct
import wave
from xml.etree import ElementTree

import pytest

from custom_components.voip_stack.core import rtp
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
from custom_components.voip_stack.phone_profile import render_phone_bundle, resolve_phone_keys
from custom_components.voip_stack.phone_profile_catalog import decode_profile_bundle
from custom_components.voip_stack.roster import RosterEntry, dump_roster_json
from custom_components.voip_stack.sip_client import SipCallClient
from custom_components.voip_stack.sip_udp_io import SipDatagramQueueProtocol
from custom_components.voip_stack.store import config_entry
from .test_sip_phone_transfers_ha import registered_phones as registered_phones
from .test_trunk_calls_ha import lab as lab, trunks as trunks, sip_test_account

pytestmark = pytest.mark.ha


def _catalog():
    configured = os.environ.get('VOIP_PHONE_PROFILE_CATALOG_PATH')
    if not configured:
        pytest.skip('The separate, explicit phone-profile catalog is required')
    root = Path(configured)
    manifest = json.loads((root / 'manifest.json').read_text())
    metadata = next(item for item in manifest['profiles'] if item['id'] == 'fanvil/x3u-pro')
    return decode_profile_bundle((root / metadata['bundle']['path']).read_bytes(), metadata)


def _power(samples, rate, frequency):
    return abs(sum(value * complex(math.cos(2 * math.pi * frequency * index / rate),
                                  math.sin(2 * math.pi * frequency * index / rate))
                   for index, value in enumerate(samples)))


@pytest.mark.parametrize('trunks', [True], indirect=True)
@pytest.mark.parametrize('registered_phones', [{'manual': 'transfer_b'}], indirect=True)
@pytest.mark.parametrize('hangup_side', ['p4', 'fanvil'])
async def test_rendered_casa_blf_pickup_retains_p4_caller_duplex_voice_and_cleanup(
    trunks, registered_phones, tmp_path, caplog, hangup_side,
):
    profile, templates = _catalog()
    lab = trunks
    entry = config_entry(lab.hass)
    lab.hass.config_entries.async_update_entry(entry, data={**entry.data, 'parking': {'enabled': True}})
    casa = PhoneEndpoint(endpoint_id='browser:casa-fixture', name='Casa', device_id='casa-device',
        kind=EndpointKind.BROWSER, availability='available', capabilities=frozenset({'audio', 'dtmf'}))
    p4 = PhoneEndpoint(endpoint_id='esphome:p4-fixture', name='Waveshare P4 Touch', username='waveshare-p4-touch',
        kind=EndpointKind.ESPHOME, availability='available', capabilities=frozenset({'audio', 'dtmf'}))
    lab.runtime.endpoints.register(casa)
    lab.runtime.endpoints.register(p4)
    lab.roster.extend([
        RosterEntry('ha_browser_casa_fixture', name='Casa', metadata={'local_ha': True, 'endpoint_id': casa.endpoint_id}),
        RosterEntry(p4.username, name=p4.name, address='127.0.0.2', metadata={'endpoint_id': p4.endpoint_id}),
    ])
    lab.hass.states.async_set('sensor.voip_phonebook', 'ready', {'roster_json': dump_roster_json(lab.roster)})
    lab.runtime.endpoints.update('transfer_c', name='Fanvil X3U Pro')
    physical = {'lines': [{'slot': '1', 'endpoint_id': 'transfer_c'}], 'keys': [
        {'slot': '3', 'line': '1', 'function': 'blf', 'target': casa.endpoint_id, 'label': 'Casa'}]}
    context = {'physical': {'name': 'Fanvil fixture', 'mac': '020000000003'},
        'lines': [{'slot': 1, 'username': 'transfer_c', 'password': 'isolated-test-only',
                   'server': '127.0.0.1', 'port': lab.runtime.transport_config['sip_port'], 'transport': 'udp'}],
        'keys': resolve_phone_keys(lab.runtime, physical, profile)}
    files, _ = render_phone_bundle(profile, templates, context)
    value = ElementTree.fromstring(next(iter(files.values()))).findtext('./dsskey/dssSide/Fkey/Value')
    assert value == 'Casa@1/bc*8Casa'
    pickup_uri = value.split('/bc', 1)[1]

    account = sip_test_account(lab, p4.endpoint_id)
    audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    audio.bind(('127.0.0.2', 0)); audio.setblocking(False)
    caller = SipCallClient(local_ip='127.0.0.2', local_name=p4.name, local_uri_user=p4.username,
        username=account.username, password=account.password, local_sip_port=0,
        local_rtp_port=audio.getsockname()[1], supported_send_formats=[AudioFormat(16000, frame_ms=20)],
        supported_recv_formats=[AudioFormat(48000, frame_ms=10)], allow_directional_audio_payloads=True,
        independent_audio_ptime=True)
    caller.transport, caller.protocol = await lab.hass.loop.create_datagram_endpoint(
        lambda: SipDatagramQueueProtocol(caller.queue), local_addr=('127.0.0.2', 0))
    caller.local_sip_port = caller.transport.get_extra_info('sockname')[1]
    picker = registered_phones[2]
    media_task = final = dialog_reader = None
    original = None
    try:
        assert await caller.invite(target='ha_browser_casa_fixture', remote_host='127.0.0.1',
            remote_sip_port=lab.runtime.transport_config['sip_port'], timeout=3) == 'ringing'
        original = next(iter(lab.calls.sessions.values()))
        assert original.owner == 'ha_softphone' and original.state == 'ringing'
        assert original.metadata['source_endpoint_id'] == p4.endpoint_id
        assert original.metadata['endpoint_id'] == casa.endpoint_id
        original_call_id = original.call_id
        final = asyncio.create_task(caller.wait_for_final(timeout=4))
        await asyncio.to_thread(picker.dial, pickup_uri)
        assert await final == 'in_call'
        # The real P4 keeps processing connected identity and terminal SIP
        # requests after its initial INVITE transaction has completed.
        dialog_reader = asyncio.create_task(caller.wait_for_dialog_termination())
        assert original.call_id == original_call_id and len(lab.calls.sessions) == 1
        assert not lab.runtime.endpoints.require(casa.endpoint_id).active_call_id
        assert lab.runtime.endpoints.require('transfer_c').active_call_id == original_call_id
        assert (caller.dialog.send_format.sample_rate, caller.dialog.send_format.frame_ms) == (16000, 20)
        assert (caller.dialog.recv_format.sample_rate, caller.dialog.recv_format.frame_ms) == (48000, 10)
        assert any('SIP RX INVITE sip:*8Casa@' in record.getMessage() for record in caplog.records)
        async def send_p4_voice():
            sequence = timestamp = 0
            while True:
                samples = [round(8000 * math.sin(2 * math.pi * 880 * (timestamp + index) / 16000)) for index in range(320)]
                packet = rtp.build_packet(rtp.RtpPacket(caller.dialog.send_format.payload_type,
                    sequence, timestamp, 0x50440001, struct.pack('!320h', *samples)))
                await lab.hass.loop.sock_sendto(audio, packet, (caller.dialog.remote_rtp_host, caller.dialog.remote_rtp_port))
                sequence = (sequence + 1) & 65535; timestamp += 320
                await asyncio.sleep(.02)
        media_task = asyncio.create_task(send_p4_voice())
        received = []
        async with asyncio.timeout(4):
            while len(received) < 4800:
                packet = rtp.parse_packet(await lab.hass.loop.sock_recv(audio, 4096))
                assert packet.payload_type == caller.dialog.recv_format.payload_type
                received.extend(struct.unpack('!'+str(len(packet.payload)//2)+'h', packet.payload))
        assert _power(received[-4800:], 48000, 650) > _power(received[-4800:], 48000, 880) * 5
        relay = lab.calls.resource_for(original.call_id, 'relay')
        async with asyncio.timeout(4):
            while relay.left_tx_packets < 50 or relay.right_tx_packets < 50:
                await asyncio.sleep(.02)
        media_task.cancel(); await asyncio.gather(media_task, return_exceptions=True)
        if hangup_side == 'p4':
            await caller.terminate(timeout=1)
        else:
            await asyncio.to_thread(picker.hangup)
        async with asyncio.timeout(4):
            while lab.calls.sessions:
                await asyncio.sleep(.01)
        assert not lab.runtime.rtp_port_pool.get('used')
        assert all(not item.active_call_id for item in lab.runtime.endpoints.endpoints)
        await asyncio.to_thread(picker.close)
        heard = False
        for path in (tmp_path / 'transfer_c').glob('*-dec.wav'):
            with wave.open(str(path), 'rb') as wav:
                rate = wav.getframerate()
                samples = struct.unpack('<'+str(wav.getnframes())+'h', wav.readframes(wav.getnframes()))[-1600:]
            heard |= bool(len(samples) >= 800 and _power(samples, rate, 880) > _power(samples, rate, 650) * 5)
        assert heard, 'Independent Fanvil peer did not decode the P4 microphone tone'
    finally:
        for task in (media_task, final, dialog_reader):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in (media_task, final, dialog_reader) if task is not None), return_exceptions=True)
        with suppress(Exception):
            await caller.terminate(timeout=1)
        await caller.close()
        audio.close()
        if original is not None and original.live:
            from custom_components.voip_stack.endpoint_session import TerminationIntent
            await lab.calls.request_termination(original.call_id, TerminationIntent('test_done'))
