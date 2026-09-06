import base64
import json
import wave

from tools.ha_voip_lab.browser_audio_capture import BrowserAudioCapture


def test_cdp_capture_preserves_wire_pcm_and_separates_format_changes(tmp_path):
    observer = BrowserAudioCapture()
    negotiation = lambda rate: {'opcode': 1, 'payloadData': json.dumps({
        'rx_format': f'{rate}:s16le:1:10', 'tx_format': '48000:s16le:1:10'})}
    pcm = bytes.fromhex('0100ffff0200feff')
    binary = {'opcode': 2, 'payloadData': base64.b64encode(b'\x01'+pcm).decode()}
    observer.record('connection', 'rx', negotiation(16000), 1.0)
    observer.record('connection', 'rx', binary, 1.1)
    observer.record('connection', 'tx', binary, 1.2)
    observer.record('connection', 'rx', negotiation(24000), 1.3)
    observer.record('connection', 'rx', binary, 1.4)
    report = observer.save(tmp_path)
    assert not report['errors']
    assert len(report['streams']) == 3
    for row, rate in zip(report['streams'], (16000, 48000, 24000), strict=True):
        with wave.open(str(tmp_path/row['file'])) as stream:
            assert stream.getframerate() == rate
            assert stream.getnframes() == 4
            assert stream.readframes(4) == pcm


def test_invalid_framing_and_capacity_fail_capture_without_mutating_media(tmp_path):
    observer = BrowserAudioCapture(max_bytes=2)
    observer.record('c', 'rx', {'opcode': 1, 'payloadData': '{"rx_format":"16000:s16le:1:10"}'}, 0)
    observer.record('c', 'rx', {'opcode': 2, 'payloadData': base64.b64encode(b'\x02\x00').decode()}, 1)
    observer.record('c', 'rx', {'opcode': 2, 'payloadData': base64.b64encode(b'\x01\x00\x00\x00\x00').decode()}, 2)
    report = observer.save(tmp_path)
    assert len(report['errors']) == 2
    assert not report['streams']
