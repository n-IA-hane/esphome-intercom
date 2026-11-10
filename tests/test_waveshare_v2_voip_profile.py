"""Resolve the maintained V2 standalone phone through ESPHome's real schema."""
from pathlib import Path

from esphome import yaml_util

from tests.native_profile import by_id, profile, text
from tests.test_package_composition import _assert_standalone_callbacks_without_runtime
from tests.test_yaml_paths_contract import _load_yaml_paths


ROOT = Path(__file__).resolve().parents[1]
PHONE = 'yamls/voip-only/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-voip.yaml'
FULL = 'yamls/full-experience/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-full.yaml'


def test_v2_phone_resolves_only_opus_and_standalone_owners() -> None:
    document = profile(PHONE)
    _assert_standalone_callbacks_without_runtime(document)
    assert not {'voice_assistant', 'micro_wake_word', 'sendspin', 'esp_afe'} & document.keys()
    assert document['esp_aec']['mode'] == 'voip_high_perf'
    codecs = document['voip_stack']['audio']['codecs']
    assert [codec['codec'] for codec in codecs] == ['opus']
    assert [(item['sample_rate'], item['frame_ms']) for item in codecs[0]['tx']] == [(16000, 20)]
    assert [(item['sample_rate'], item['frame_ms']) for item in codecs[0]['rx']] == [(48000, 20)]
    assert 'blf' in document['voip_stack']
    assert 'message_waiting' in text(document['voip_stack'])
    assert by_id(document, 'contacts_list_view')
    assert by_id(document, 'call_idle_page')
    assert len([item for item in document['switch'] if str(item.get('id')) == 'mute']) == 1
    assert 'voip_stack.dump_diagnostics' in text(document['button'])
    for item_id in ('call_button', 'next_contact_button', 'prev_contact_button', 'decline_button'):
        assert by_id(document['button'], item_id)
    assert any(item['platform'] == 'restart' for item in document['button'])


def test_v2_phone_keeps_shared_board_and_audio_calibration() -> None:
    phone, full = profile(PHONE), profile(FULL)
    for key in ('sample_rate', 'output_sample_rate', 'i2s_mclk_pin', 'i2s_bclk_pin',
                'i2s_lrclk_pin', 'i2s_din_pin', 'i2s_dout_pin', 'codec',
                'tdm_total_slots', 'tdm_mic_slots', 'tdm_ref_slot', 'use_tdm_reference'):
        assert phone['esp_audio_stack'][key] == full['esp_audio_stack'][key], key
    assert phone['esp32']['flash_size'] == full['esp32']['flash_size'] == '16MB'
    assert phone['psram'] == full['psram']
    for kind, item_id in (('display', 's3_box_lcd'), ('touchscreen', 'touch')):
        left, right = by_id(phone[kind], item_id), by_id(full[kind], item_id)
        assert left['reset_pin'] == right['reset_pin']
    assert by_id(phone, 's3_box_lcd')['reset_pin']['number'] == 1
    assert by_id(phone, 'touch')['reset_pin']['number'] == 0
    hint = by_id(phone['binary_sensor'], 'usb_connected')
    assert hint['platform'] == 'copy' and hint['internal'] is True
    assert str(hint['source_id']) == 'battery_above_standby_threshold'
    assert by_id(phone['output'], 'speaker_enable') == by_id(full['output'], 'speaker_enable')


def test_v2_phone_source_channels_roundtrip_without_local_public_paths(tmp_path: Path) -> None:
    tool = _load_yaml_paths()
    candidate = tmp_path / 'phone.yaml'
    source = (ROOT / PHONE).read_text()
    source = source.replace('!include ../../../packages/devices/waveshare-s3-box-v2-phone.yaml',
                            '!include ' + str(ROOT / 'packages/devices/waveshare-s3-box-v2-phone.yaml'))
    candidate.write_text(source)
    for ref in ('dev', 'main'):
        tool.rewrite_yaml(candidate, ref)
        tool.rewrite_sources(candidate, ref)
        assert tool.mode(candidate) == 'remote'
        assert list(tool.source_errors(candidate, ref, True)) == []
        opposite = 'main' if ref == 'dev' else 'dev'
        assert list(tool.source_errors(candidate, opposite, True))
        assert f'packages/devices/waveshare-s3-box-v2-phone.yaml@{ref}' in candidate.read_text()
        assert '/home/' not in candidate.read_text()
    tool.rewrite_yaml(candidate, 'local')
    tool.rewrite_sources(candidate, 'local')
    assert tool.mode(candidate) == 'local'
    assert list(tool.source_errors(candidate, None, False)) == []
    assert yaml_util.load_yaml(candidate)['substitutions']['firmware_features'] == [
        'voip', 'directory', 'blf', 'mwi', 'display', 'diagnostics']
