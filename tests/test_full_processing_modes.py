"""Check FULL processing choices through ESPHome's real config pipeline."""
from pathlib import Path
import subprocess
import sys

from esphome import yaml_util
import pytest

from tests.native_profile import PackageLoader, by_id, nodes, profile
from tests.test_package_composition import device_config
import yaml


FULL_PROFILES = [
    'full-experience/dual-bus/generic-s3-full-aec.yaml',
    'full-experience/esphome-native/generic-s3-full-esphome-native.yaml',
    'full-experience/single-bus/generic-s3-full-aec-camera.yaml',
    'full-experience/single-bus/generic-s3-full-aec.yaml',
    'full-experience/single-bus/generic-s3-full-lite-aec.yaml',
    'full-experience/single-bus/spotpear-ball-v2-full-afe.yaml',
    'full-experience/single-bus/waveshare-p4-touch-full-afe-landscape-videophone-jpeg.yaml',
    'full-experience/single-bus/waveshare-p4-touch-full-afe-portrait.yaml',
    'full-experience/single-bus/waveshare-s3-full-afe.yaml',
    'full-experience/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-full-afe.yaml',
    'full-experience/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-full.yaml',
    'experimental/esp32-s3-box-3/esp32-s3-box-3-full-aec.yaml',
    'experimental/esp32-s3-box-3/esp32-s3-box-3-full-afe.yaml',
    'experimental/waveshare-s3-touch-lcd-1.85c-box-v2/waveshare-s3-touch-lcd-1.85c-box-v2-full-aec.yaml',
    'experimental/waveshare-s3-touch-lcd-1.85c/waveshare-s3-touch-lcd-1.85c-box-full-afe.yaml',
    'untested/generic-s3-full-afe.yaml',
]
SR_MODES = ['sr_low_cost', 'sr_high_perf']


def processor_mode(document):
    if 'esp_aec' in document:
        return str(document['esp_aec']['mode']).lower()
    processor = document['esp_afe']
    return f"{processor['type']}_{processor['mode']}".lower()


@pytest.mark.parametrize('relative', FULL_PROFILES)
def test_full_dsp_and_ui_share_sr_high_default(relative):
    document = profile(relative)
    if relative.startswith('full-experience/esphome-native/'):
        assert 'esp_aec' not in document and 'esp_afe' not in document
        return
    assert processor_mode(document) == 'sr_high_perf'
    control = by_id(document['select'], 'aec_mode_select')
    assert control['initial_option'] == 'sr_high_perf'
    assert control['options'] == SR_MODES
    widgets = [node for node in nodes(document.get('lvgl', {})) if node.get('id') == 'ui_aec_mode_dd']
    for widget in widgets:
        assert widget['options'] == control['options']
    microphone = document['micro_wake_word']['microphone']
    if isinstance(microphone, dict):
        assert microphone.get('gain_factor', 1) == 1


@pytest.mark.parametrize(('relative', 'initial_key'), [
    ('full-experience/single-bus/generic-s3-full-aec.yaml', 'aec_initial_mode'),
    ('full-experience/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-full.yaml', 'aec_initial_mode'),
    ('full-experience/single-bus/waveshare-s3-full-afe.yaml', 'afe_initial_mode'),
    ('full-experience/single-bus/spotpear-ball-v2-full-afe.yaml', 'afe_initial_mode'),
    ('full-experience/single-bus/waveshare-p4-touch-full-afe-portrait.yaml', 'afe_initial_mode'),
])
def test_explicit_sr_low_initial_mode_controls_dsp_and_select(tmp_path: Path, relative, initial_key):
    shipped = yaml_util.load_yaml(Path(__file__).resolve().parents[1] / 'yamls' / relative)
    config = device_config(tmp_path, shipped['substitutions']['firmware_features'], 'yamls/' + relative)
    original = yaml_util.load_yaml(config)
    original['substitutions'][initial_key] = 'sr_low_cost'
    config.write_text(yaml_util.dump(original))
    result = subprocess.run([sys.executable, '-m', 'esphome', 'config', '--no-defaults', str(config)],
                            capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    document = yaml.load(result.stdout, Loader=PackageLoader)
    assert processor_mode(document) == 'sr_low_cost'
    control = by_id(document['select'], 'aec_mode_select')
    assert control['initial_option'] == 'sr_low_cost'
    assert control['options'] == SR_MODES
