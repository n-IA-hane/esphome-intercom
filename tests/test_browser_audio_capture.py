import base64
import json
import wave

from tools.ha_voip_lab.browser_audio_capture import BrowserAudioCapture


def test_cdp_capture_preserves_wire_pcm_and_separates_format_changes(tmp_path):
    observer = BrowserAudioCapture()
    def negotiation(rate):
        return {'opcode': 1, 'payloadData': json.dumps({
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


def test_explicit_bounded_capture_budget_preserves_default_and_full_90s_duplex():
    from tools import sip_video_browser_probe as probe
    budget=probe._audio_capture_max_bytes(str(32*1024*1024))
    original=BrowserAudioCapture()
    expanded=BrowserAudioCapture(max_bytes=budget)
    assert original.max_bytes==10*1024*1024
    negotiation={'opcode':1,'payloadData':json.dumps({'tx_format':'48000:s16le:1:10','rx_format':'48000:s16le:1:10'})}
    frame={'opcode':2,'payloadData':base64.b64encode(b'\x01'+bytes(960)).decode()}
    for observer in (original,expanded):observer.record('c','rx',negotiation,0)
    for i in range(9000):
        for direction in ('tx','rx'):
            original.record('c',direction,frame,i/100)
            expanded.record('c',direction,frame,i/100)
    assert original.errors==['browser audio capture capacity exceeded']
    assert not expanded.errors and expanded.total_bytes==48000*2*2*90
    assert expanded.total_bytes<expanded.max_bytes<=64*1024*1024


def test_capture_budget_rejects_outside_limits_before_browser_or_auth(monkeypatch):
    import sys
    import pytest
    from tools import sip_video_browser_probe as probe
    for value in ('0',str(1024*1024-1),str(64*1024*1024+1),'not-a-number'):
        monkeypatch.setattr(sys,'argv',['probe','--audio-capture-max-bytes',value])
        with pytest.raises(SystemExit) as error:probe.main()
        assert error.value.code==2
    assert probe._audio_capture_max_bytes(str(1024*1024))==1024*1024
    assert probe._audio_capture_max_bytes(str(64*1024*1024))==64*1024*1024
