"""Compose public FULL audio controls and explicit media-provider ownership."""
from pathlib import Path
import subprocess
import sys

from esphome import yaml_util
import pytest

from tests.native_profile import nodes, text
from tests.test_package_composition import device_config


PROFILES = [
    ('yamls/full-experience/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-full-afe.yaml', 0, 24),
    ('yamls/full-experience/single-bus/waveshare-p4-touch-full-afe-landscape-videophone-jpeg.yaml', 30, 24),
    ('yamls/full-experience/single-bus/waveshare-p4-touch-full-afe-portrait.yaml', 30, 0),
]


@pytest.mark.parametrize('profile,mic_default,ref_default', PROFILES)
@pytest.mark.parametrize('legacy', [True, False])
def test_full_gain_controls_and_provider_selection(tmp_path, profile, mic_default, ref_default, legacy):
    original = yaml_util.load_yaml(Path(__file__).parents[1] / profile)
    config = device_config(tmp_path, original['substitutions']['firmware_features'], profile)
    document = yaml_util.load_yaml(config)
    if not legacy:
        document['substitutions']['esphome_legacy_audio_providers'] = []
        document['substitutions']['microphone_pga_name'] = 'Existing microphone preference'
        document['substitutions']['reference_pga_name'] = 'Existing reference preference'
    config.write_text(yaml_util.dump(document))
    result = subprocess.run([sys.executable, '-m', 'esphome', 'config', str(config)],
        capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    expanded = tmp_path / 'expanded.yaml'
    expanded.write_text(result.stdout)
    current = yaml_util.load_yaml(expanded)
    assert current['esp_afe']['afe_linear_gain'] == 1
    assert current['esp_audio_stack']['codec']['input']['gain_db'] == mic_default
    assert current['esp_audio_stack']['codec']['input']['ref_gain_db'] == ref_default
    numbers = {str(node['id']): node for node in nodes(current['number']) if 'id' in node}
    assert 'mic_gain' not in numbers
    for identifier in ('microphone_pga', 'reference_pga'):
        assert identifier in numbers and 'initial_value' not in numbers[identifier]
    assert numbers['microphone_pga']['name'] == (
        'Microphone Gain (pre-processing)' if legacy else 'Existing microphone preference')
    assert numbers['reference_pga']['name'] == (
        'AEC Reference Gain' if legacy else 'Existing reference preference')
    assert 'id(mic_gain)' not in text(current['lvgl'])
    for owner in ('voice_assistant', 'micro_wake_word'):
        source = current[owner]['microphone']
        if isinstance(source, list):
            source, = source
        assert source['gain_factor'] == 1
    providers = [component for entry in current['external_components'] for component in entry['components']]
    assert providers.count('micro_wake_word') == 1
    for component in ('voice_assistant', 'audio_http', 'speaker_source'):
        assert providers.count(component) == int(legacy)
    physical = next(item for item in current['speaker'] if item.get('platform') == 'esp_audio_stack')
    assert 'audio_stack_speaker_idle_powerdown' in str(physical['amplifier_settle_time'])
    assert '? 0 : 250' in str(physical['amplifier_settle_time'])
    assert not any('delay' in node for node in nodes(current['esp_audio_stack']['on_amplifier_required']))
