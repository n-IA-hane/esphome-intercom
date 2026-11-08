"""Compose the call-only control and check the shared AFE gain remains unity."""
from pathlib import Path
import subprocess
import sys
import pytest
from esphome import yaml_util
from tests.native_profile import nodes
from tests.test_package_composition import device_config

ROOT = Path(__file__).resolve().parents[1]

@pytest.mark.parametrize('name', ['spotpear-ball-v2-full-afe.yaml','waveshare-s3-touch-lcd-1.85c-box-v2-full-afe.yaml','waveshare-p4-touch-full-afe-landscape-videophone-jpeg.yaml'])
def test_full_has_distinct_call_gain_without_shared_mic_gain(tmp_path, name):
    profile = 'yamls/full-experience/single-bus/' + name
    original = yaml_util.load_yaml(ROOT / profile)
    config = device_config(tmp_path, original['substitutions']['firmware_features'], profile)
    result = subprocess.run([sys.executable, '-m', 'esphome', 'config', str(config)], capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    expanded = tmp_path / 'expanded.yaml'
    expanded.write_text(result.stdout)
    current = yaml_util.load_yaml(expanded)
    numbers = {str(n['id']): n for n in nodes(current['number']) if 'id' in n}
    assert numbers['voip_tx_gain']['name'] == 'VoIP TX Gain'
    assert numbers['voip_tx_gain']['unit_of_measurement'] == 'dB'
    assert numbers['voip_tx_gain']['min_value'] == -20
    assert numbers['voip_tx_gain']['max_value'] == 20
    assert 'mic_gain' not in numbers
    for owner in ('voice_assistant', 'micro_wake_word'):
        source = current[owner]['microphone']
        if isinstance(source, list):
            source, = source
        assert source['gain_factor'] == 1
    assert current['esp_afe']['afe_linear_gain'] == 1
