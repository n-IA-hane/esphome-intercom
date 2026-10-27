"""Validate feature removal through ESPHome's real package and schema pipeline."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

from esphome import yaml_util
import pytest


ROOT = Path(__file__).resolve().parents[1]
FULL = ['voice', 'wake_word', 'voip', 'music', 'sendspin', 'timers', 'status_led', 'diagnostics']


def test_full_lite_shipped_defaults_keep_sendspin_optional() -> None:
    from tests.native_profile import profile

    document = profile('full-experience/single-bus/generic-s3-full-lite-aec.yaml')
    assert 'sendspin' not in document, 'The 4 MB Lite default must not instantiate Sendspin'
    assert document['esp32']['flash_size'] == '4MB'
    assert 'voice_assistant' in document
    assert 'micro_wake_word' in document
    assert 'voip_stack' in document
    player = document['media_player'][0]
    assert 'media_pipeline' in player
    assert 'announcement_pipeline' in player


def device_config(directory: Path, features: list[str], profile_path: str = 'yamls/full-experience/single-bus/waveshare-s3-full-afe.yaml') -> Path:
    profile = yaml_util.load_yaml(ROOT / profile_path)
    substitutions = dict(profile['substitutions'])
    substitutions['firmware_features'] = features
    substitutions['assets_base'] = str(ROOT) + '/'
    substitutions['intercom_idf_root'] = str(ROOT / 'idf_components')
    for key, repository in (
        ('ext_components_source', ROOT),
        ('audio_stack_components_source', Path(os.environ.get('AUDIO_STACK_REPO', ROOT.parent / 'esphome-audio-stack'))),
        ('voip_stack_components_source', Path(os.environ.get('VOIP_STACK_REPO', ROOT.parent / 'esphome-voip-stack'))),
        ('runtime_controller_components_source', Path(os.environ.get('RUNTIME_CONTROLLER_REPO', ROOT.parent / 'esphome-runtime-controller'))),
        ('camera_components_source', Path(os.environ.get('ESP_VIDEO_CAMERA_REPO', ROOT.parent / 'esphome-esp-video-camera'))),
    ):
        component_path = 'components' if key == 'camera_components_source' else 'esphome/components'
        substitutions[key] = str(repository / component_path)
    directory.mkdir(parents=True, exist_ok=True)
    for companion in (ROOT / profile_path).parent.glob('*.csv'):
        shutil.copy2(companion, directory / companion.name)
    (directory / 'secrets.yaml').write_text('wifi_ssid: fixture-network\nwifi_password: fixture-password\n')
    config = directory / 'device.yaml'
    package_path = str(profile['packages']['device']).split('/packages/', 1)[1].rsplit('@', 1)[0]
    config.write_text(yaml_util.dump({'substitutions': substitutions}) +
                      '\npackages:\n  device: !include ' +
                      str(ROOT / 'packages' / package_path) + '\n')
    return config


def maintenance_begin_actions(document):
    ota = next(item for item in document['ota'] if item['platform'] == 'esphome')
    for event, resume in (('on_begin', False), ('on_abort', True), ('on_error', True)):
        assert ota[event]['then'] == [{'script.execute': {'id': 'firmware_maintenance', 'resume': resume}}]
    owner = next(script for script in document['script'] if script['id'] == 'firmware_maintenance')
    assert owner['mode'] == 'restart'
    assert owner['parameters'] == {'resume': 'bool'}
    return owner['then'][0]['if']['else']


@pytest.mark.parametrize('features', [
    FULL,
    ['voip'],
    ['voice'],
    ['music'],
    ['voice', 'voip'],
    ['voice', 'wake_word', 'timers'],
    *[[feature for feature in FULL if feature != removed]
      for removed in ('timers', 'sendspin', 'wake_word', 'voip', 'music', 'voice', 'status_led')],
])
def test_ws3_native_feature_composition(tmp_path: Path, features: list[str]) -> None:
    config = device_config(tmp_path, features)
    result = subprocess.run(
        [sys.executable, '-m', 'esphome', 'config', '--no-defaults', str(config)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Could not resolve substitution' not in result.stderr, result.stderr
    expanded = tmp_path / 'expanded.yaml'
    expanded.write_text(result.stdout)
    document = yaml_util.load_yaml(expanded)
    assert document['substitutions']['firmware_features'] == features
    assert all('speaker' not in item.get('components', [])
               for item in document.get('external_components', [])), (
        'The maintained source-based player uses ESPHome native speaker'
    )
    has_voice = 'voice' in features
    has_music = 'music' in features
    assert ('voip_stack' in document) is ('voip' in features)
    assert ('voice_assistant' in document) is has_voice
    assert ('micro_wake_word' in document) is (has_voice and 'wake_word' in features)
    assert ('sendspin' in document) is (has_music and 'sendspin' in features)
    pairing_sensor = any(item.get('id') == 'sendspin_pairing_code'
                         for item in document.get('text_sensor', []))
    pairing_buttons = {str(item.get('id')) for item in document.get('button', [])
                       if str(item.get('id', '')).startswith('sendspin_')}
    assert pairing_sensor is (has_music and 'sendspin' in features)
    assert pairing_buttons == ({'sendspin_confirm_pairing', 'sendspin_cancel_pairing'}
                               if has_music and 'sendspin' in features else set())
    assert ('light' in document) is ('status_led' in features)

    scripts = {str(script['id']) for script in document.get('script', [])}
    assert ('timer_alarm_loop' in scripts) is (has_voice and 'timers' in features)
    assert ('start_mww_when_ready' in scripts) is (has_voice and 'wake_word' in features)
    if 'voip' not in features:
        assert 'voip_start_ringtone' not in scripts
        assert not any(button.get('name') in ('Call', 'Next Contact', 'Previous Contact', 'Decline')
                       for button in document.get('button', []))
    players = document.get('media_player', [])
    assert len(players) <= 1
    if players:
        assert ('media_pipeline' in players[0]) is has_music
    speakers = [str(speaker.get('id')) for speaker in document['speaker']]
    inputs = int(has_music) + int(has_voice or 'voip' in features) + int('voip' in features)
    assert speakers.count('audio_mixer') == int(inputs >= 2)
    if inputs >= 2:
        mixer = next(speaker for speaker in document['speaker'] if speaker.get('id') == 'audio_mixer')
        expected_inputs = (
            (['media_mixer_input'] if has_music else [])
            + (['tts_mixer_input'] if has_voice or 'voip' in features else [])
            + (['voip_mixer_input'] if 'voip' in features else [])
        )
        assert [item['id'] for item in mixer['source_speakers']] == expected_inputs
    assert ('voip_speaker_input' in speakers) is ('voip' in features)
    assert ('media_speaker_input' in speakers) is has_music

    # Each input event is forwarded only once, even with combined features.
    if has_voice:
        assert document['runtime_controller']['observe']['voice_assistant'] == 'va'
        starts = document['voice_assistant'].get('on_start', [])
        assert not any(action.get('runtime_controller.event', {}).get('event') == 'va_start'
                       for action in starts)

    stops = maintenance_begin_actions(document)
    actions = [next(iter(action)) for action in stops]
    assert ('voice_assistant.stop' in actions) is has_voice
    assert ('micro_wake_word.stop' in actions) is (has_voice and 'wake_word' in features)
    assert ('voip_stack.stop' in actions) is ('voip' in features)
    assert actions[-2:] == ['esp_audio_stack.stop', 'wait_until']


def test_voice_preset_loads_outside_sibling_repository_layout(tmp_path: Path) -> None:
    from esphome.components.packages import resolve_packages

    target = tmp_path / 'download' / 'packages'
    shutil.copytree(ROOT / 'packages', target)
    config = tmp_path / 'voice.yaml'
    config.write_text('packages:\n  voice: !include ' + str(target / 'presets/voice_runtime.yaml') + '\n')
    resolved = resolve_packages(yaml_util.load_yaml(config))
    assert 'runtime_controller' in resolved
    assert 'voice_assistant' in resolved
    assert 'voip_stack' not in resolved


@pytest.mark.parametrize('profile_path', [
    'yamls/full-experience/single-bus/generic-s3-full-aec.yaml',
    'yamls/full-experience/dual-bus/generic-s3-full-aec.yaml',
    'yamls/full-experience/single-bus/generic-s3-full-lite-aec.yaml',
    'yamls/full-experience/esphome-native/generic-s3-full-esphome-native.yaml',
    'yamls/untested/generic-s3-full-afe.yaml',
])
@pytest.mark.parametrize('features', [FULL, ['voice'], ['voip'], ['music']])
def test_generic_feature_composition(tmp_path: Path, profile_path: str, features: list[str]) -> None:
    config = device_config(tmp_path, features, profile_path)
    result = subprocess.run(
        [sys.executable, '-m', 'esphome', 'compile', '--only-generate', str(config)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Could not resolve substitution' not in result.stderr, result.stderr


@pytest.mark.parametrize('features', [
    FULL + ['display'],
    FULL,
    *[[feature for feature in FULL if feature != removed] + ['display']
      for removed in ('voip', 'voice', 'music', 'sendspin', 'timers', 'wake_word')],
])
def test_spotpear_presentation_follows_features(tmp_path: Path, features: list[str]) -> None:
    config = device_config(tmp_path, features, 'yamls/full-experience/single-bus/spotpear-ball-v2-full-afe.yaml')
    result = subprocess.run(
        [sys.executable, '-m', 'esphome', 'config', '--no-defaults', str(config)],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Could not resolve substitution' not in result.stderr, result.stderr
    expanded = tmp_path / 'expanded.yaml'
    expanded.write_text(result.stdout)
    document = yaml_util.load_yaml(expanded)
    assert document['substitutions']['firmware_features'] == features
    assert ('lvgl' in document) is ('display' in features)
    assert ('display' in document) is ('display' in features)
    if 'display' in features:
        pages = {str(page['id']) for page in document['lvgl']['pages']}
        if 'voice' not in features:
            def events(value):
                if isinstance(value, dict):
                    event = value.get('runtime_controller.event', {})
                    if isinstance(event, dict) and 'event' in event:
                        yield event['event']
                    for child in value.values():
                        yield from events(child)
                elif isinstance(value, list):
                    for child in value:
                        yield from events(child)
            assert not {'wake_word', 'manual_voice_toggle', 'voice_stop'} & set(events(document['lvgl']))
        assert ('call_idle_page' in pages) is ('voip' in features)
        assert ('timer_finished_page' in pages) is ('voice' in features and 'timers' in features)
    generated = subprocess.run(
        [sys.executable, '-m', 'esphome', 'compile', '--only-generate', str(config)],
        capture_output=True, text=True, timeout=180,
    )
    assert generated.returncode == 0, generated.stdout + generated.stderr


def test_device_parameter_overrides_are_not_shadowed_or_concatenated(tmp_path: Path) -> None:
    config = device_config(tmp_path, FULL + ['display'], 'yamls/full-experience/single-bus/spotpear-ball-v2-full-afe.yaml')
    document = yaml_util.load_yaml(config)
    document['substitutions'].update({
        'voip_transport': 'tcp', 'voip_sip_port': '5090', 'voip_rtp_port': '41000',
        'mww_task_priority': '5', 'afe_mode_options': ['sr_low_cost'],
        'afe_initial_mode': 'sr_low_cost',
    })
    config.write_text(yaml_util.dump(document))
    result = subprocess.run(
        [sys.executable, '-m', 'esphome', 'config', '--no-defaults', str(config)],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    expanded = tmp_path / 'expanded.yaml'
    expanded.write_text(result.stdout)
    current = yaml_util.load_yaml(expanded)
    assert current['voip_stack']['transport'] == 'tcp'
    assert int(current['voip_stack']['sip_port']) == 5090
    assert int(current['voip_stack']['rtp_port']) == 41000
    mode = next(item for item in current['select'] if item['id'] == 'aec_mode_select')
    assert mode['options'] == ['sr_low_cost']
    assert mode['initial_option'] == 'sr_low_cost'
    assert current['esp_afe']['type'] == 'sr'
    assert current['esp_afe']['mode'] == 'low_cost'
    boost = next(item for item in current['script'] if item['id'] == 'boost_mww_priority')
    body = boost['then'][-1]['lambda']
    assert 'vTaskPrioritySet(mww_task, 5);' in getattr(body, 'value', body)

    def mode_widgets(value):
        if isinstance(value, dict):
            if value.get('id') == 'ui_aec_mode_dd':
                yield value
            for child in value.values():
                yield from mode_widgets(child)
        elif isinstance(value, list):
            for child in value:
                yield from mode_widgets(child)

    widgets = list(mode_widgets(current['lvgl']))
    assert len(widgets) == 1
    assert widgets[0]['options'] == mode['options']


@pytest.mark.parametrize('profile_path', [
    'yamls/full-experience/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-full-afe.yaml',
    'yamls/experimental/waveshare-s3-touch-lcd-1.85c-box-v2/waveshare-s3-touch-lcd-1.85c-box-v2-full-aec.yaml',
])
@pytest.mark.parametrize('removed', [None, 'voip', 'voice', 'timers', 'display'])
@pytest.mark.timeout(240)
def test_waveshare_v2_round_composition(tmp_path: Path, profile_path: str, removed: str | None) -> None:
    # Native code generation may populate an empty font/component cache. Keep
    # the outer test budget above the subprocess's existing 180-second limit.
    profile = yaml_util.load_yaml(ROOT / profile_path)
    features = [name for name in profile['substitutions']['firmware_features'] if name != removed]
    config = device_config(tmp_path, features, profile_path)
    result = subprocess.run(
        [sys.executable, '-m', 'esphome', 'compile', '--only-generate', str(config)],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Could not resolve substitution' not in result.stderr, result.stderr


@pytest.mark.parametrize('processor', ['aec', 'afe'])
@pytest.mark.parametrize('removed', [None, 'display', 'sensor_dock'])
def test_box3_display_and_sensor_dock_are_independent(tmp_path: Path, processor: str, removed: str | None) -> None:
    profile_path = f'yamls/experimental/esp32-s3-box-3/esp32-s3-box-3-full-{processor}.yaml'
    profile = yaml_util.load_yaml(ROOT / profile_path)
    features = [name for name in profile['substitutions']['firmware_features'] if name != removed]
    config = device_config(tmp_path, features, profile_path)
    result = subprocess.run(
        [sys.executable, '-m', 'esphome', 'config', '--no-defaults', str(config)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Could not resolve substitution' not in result.stderr, result.stderr
    expanded = tmp_path / 'expanded.yaml'
    expanded.write_text(result.stdout)
    current = yaml_util.load_yaml(expanded)
    assert ('display' in current) is (removed != 'display')
    assert any(sensor.get('id') == 'radar_sensor' for sensor in current['binary_sensor']) is (removed != 'sensor_dock')
    assert any(bus.get('id') == 'external_i2c' for bus in current['i2c']) is (removed != 'sensor_dock')
    generated = subprocess.run(
        [sys.executable, '-m', 'esphome', 'compile', '--only-generate', str(config)],
        capture_output=True, text=True, timeout=120,
    )
    assert generated.returncode == 0, generated.stdout + generated.stderr


_HEADLESS_STANDALONE_PROFILES = [
    'yamls/voip-only/esphome-native/generic-s3-voip-esphome-native-full-duplex.yaml',
    'yamls/voip-only/esphome-native/generic-s3-voip-esphome-native-mic-only.yaml',
    'yamls/voip-only/esphome-native/generic-s3-voip-esphome-native-speaker-only.yaml',
    'yamls/voip-only/single-bus/generic-s3-voip.yaml',
    'yamls/voip-only/dual-bus/generic-s3-voip.yaml',
    'yamls/voip-only/single-bus/waveshare-s3-voip.yaml',
]
# Follow the maintained catalog, including new untracked entrypoints while
# excluding gitignored local diagnostics and secrets before opening any file.
_MAINTAINED_PHONE_YAMLS = [
    ROOT / path for path in subprocess.check_output(
        ['git', 'ls-files', '--cached', '--others', '--exclude-standard', '--',
         'yamls/voip-only', 'yamls/experimental'], cwd=ROOT, text=True).splitlines()
    if path.endswith('.yaml') and Path(path).name != 'secrets.yaml'
]
_STANDALONE_PHONE_CASES = [
    pytest.param(profile_path, features, id=f'features{index}-{profile_path}')
    for profile_path in _HEADLESS_STANDALONE_PROFILES
    for index, features in enumerate((['voip', 'status_led', 'diagnostics'], ['voip'], [], ['status_led', 'diagnostics']))
] + [
    pytest.param(str(path.relative_to(ROOT)), None,
                 id='shipped-default-' + str(path.relative_to(ROOT / 'yamls/voip-only')).replace('/', '-'))
    for path in sorted(_MAINTAINED_PHONE_YAMLS)
    if path.is_relative_to(ROOT / 'yamls/voip-only')
] + [
    pytest.param(str(path.relative_to(ROOT)), None,
                 id='experimental-default-' + path.name)
    for path in sorted(_MAINTAINED_PHONE_YAMLS)
    if path.is_relative_to(ROOT / 'yamls/experimental') and 'voip-only' in path.name
]


def _assert_standalone_callbacks_without_runtime(document, generated=None):
    from tests.native_profile import nodes, text

    assert 'runtime_controller' not in document
    assert not any(str(key).startswith('runtime_controller.')
                   for node in nodes(document) for key in node)
    for entry in document.get('external_components', []):
        assert 'runtime_controller' not in entry.get('components', [])
        assert 'esphome-runtime-controller' not in str(entry.get('source', ''))
    # An unused source substitution is not a loaded component. Inspect the
    # actual callbacks, lambdas and platform options, without that metadata.
    resolved = {key: value for key, value in document.items() if key != 'substitutions'}
    behavior = text(resolved)
    for forbidden in ('runtime_controller::', 'runtime_controller.event',
                      'USE_RUNTIME_CONTROLLER', 'id(runtime)'):
        assert forbidden not in behavior
    if 'voip_stack' in document:
        for event in ('on_calling', 'on_ringing', 'on_in_call', 'on_idle', 'on_hangup', 'on_call_failed'):
            assert event in document['voip_stack'], event
    if generated is not None:
        for forbidden in ('runtime_controller::', 'USE_RUNTIME_CONTROLLER',
                          '#include "esphome/components/runtime_controller/'):
            assert forbidden not in generated


@pytest.mark.parametrize('profile_path,features', _STANDALONE_PHONE_CASES)
@pytest.mark.timeout(180)
def test_standalone_phone_has_no_implicit_led_or_coordinator(tmp_path, profile_path, features):
    shipped = features is None
    original = yaml_util.load_yaml(ROOT / profile_path)
    if shipped:
        features = original['substitutions']['firmware_features']
    config = device_config(tmp_path, features, profile_path)
    if shipped:
        data = yaml_util.load_yaml(config)
        data.update({key: value for key, value in original.items() if key not in ('substitutions', 'packages')})
        data.setdefault('esphome', {})['build_path'] = str(tmp_path / 'build')
        config.write_text(yaml_util.dump(data))
    result = subprocess.run(
        [sys.executable, '-m', 'esphome', 'config', '--no-defaults', str(config)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Could not resolve substitution' not in result.stderr
    expanded = tmp_path / 'expanded.yaml'
    expanded.write_text(result.stdout)
    document = yaml_util.load_yaml(expanded)
    assert ('voip_stack' in document) is ('voip' in features)
    _assert_standalone_callbacks_without_runtime(document)
    if not profile_path.startswith('yamls/voip-only/'):
        return
    assert not {'runtime_controller', 'voice_assistant', 'micro_wake_word', 'sendspin'} & document.keys()
    assert ('light' in document) is ('status_led' in features or (shipped and 'display' in features))
    assert ('microphone' in document) is ('speaker-only' not in profile_path)
    assert ('speaker' in document) is ('mic-only' not in profile_path)
    samples = {'yamls/voip-only/single-bus/spotpear-ball-v2-voip.yaml',
               'yamls/voip-only/single-bus/waveshare-p4-touch-videophone-h264.yaml'}
    if shipped and profile_path not in samples:
        return
    generated = subprocess.run(
        [sys.executable, '-m', 'esphome', 'compile', '--only-generate', str(config)],
        capture_output=True, text=True, timeout=120,
    )
    assert generated.returncode == 0, generated.stdout + generated.stderr
    main_cpp = next(tmp_path.rglob('main.cpp'))
    generated_components = '\n'.join((main_cpp.parent / name).read_text()
                                     for name in ('main.cpp', 'esphome.h', 'esphome/core/defines.h'))
    _assert_standalone_callbacks_without_runtime(document, generated_components)


@pytest.mark.parametrize('profile_path', [
    'yamls/full-experience/single-bus/waveshare-p4-touch-full-afe-portrait.yaml',
    'yamls/full-experience/single-bus/waveshare-p4-touch-full-afe-landscape-videophone-jpeg.yaml',
])
@pytest.mark.parametrize('features', [
    [x for x in FULL if x != 'status_led'] + ['display', 'video'],
    *[[x for x in FULL + ['display', 'video'] if x not in ('status_led', removed)]
      for removed in ('display', 'voip', 'voice', 'timers', 'wake_word', 'music', 'sendspin')],
])
@pytest.mark.timeout(240)
def test_p4_feature_composition(tmp_path, profile_path, features):
    config = device_config(tmp_path, features, profile_path)
    result = subprocess.run(
        [sys.executable, '-m', 'esphome', 'config', '--no-defaults', str(config)],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Could not resolve substitution' not in result.stderr, result.stderr

    expanded = tmp_path / 'expanded.yaml'
    expanded.write_text(result.stdout)
    document = yaml_util.load_yaml(expanded)
    assert ('voip_stack' in document) is ('voip' in features)
    assert ('voice_assistant' in document) is ('voice' in features)
    assert ('micro_wake_word' in document) is ('voice' in features and 'wake_word' in features)
    assert ('sendspin' in document) is ('music' in features and 'sendspin' in features)
    assert ('lvgl' in document) is ('display' in features)
    if len(features) >= 9 or 'display' not in features:
        generated = subprocess.run(
            [sys.executable, '-m', 'esphome', 'compile', '--only-generate', str(config)],
            capture_output=True, text=True, timeout=180,
        )
        assert generated.returncode == 0, generated.stdout + generated.stderr


def test_legacy_runtime_packages_resolve_to_the_canonical_bindings(tmp_path):
    """Emulate a remote cache, without requiring sibling-relative includes."""
    from esphome.components.packages import resolve_packages
    runtime = Path(os.environ.get('RUNTIME_CONTROLLER_REPO', ROOT.parent / 'esphome-runtime-controller'))
    legacy = tmp_path / 'download/runtime'
    shutil.copytree(runtime / 'packages', legacy)
    for package in legacy.rglob('*.yaml'):
        text = package.read_text()
        # Only substitute Git transport with an isolated local cache. Package
        # merge, action composition and IDs still use ESPHome's native parser.
        import re
        text = re.sub(r'github://n-IA-hane/esphome-intercom/(packages/[^\s@]+)@[^\s]+',
                      lambda m: '!include ' + str(ROOT / m[1]), text)
        assert 'github://n-IA-hane/esphome-intercom/packages/' not in text
        package.write_text(text)
    config = tmp_path / 'legacy.yaml'
    config.write_text('packages:\n  runtime: !include ' + str(legacy / 'runtime_controller/full_controller_no_led.yaml') + '\n')
    resolved = resolve_packages(yaml_util.load_yaml(config))
    scripts = [s for s in resolved['script'] if s['id'] == 'runtime_voice_transition']
    assert len(scripts) == 1
    assert yaml_util.dump(scripts).count('event: va_stop_complete') == 2
    assert resolved['runtime_controller']['observe']['media_player'] == 'speaker_media_player'
    assert 'runtime_voice_start' in {s['id'] for s in resolved['script']}


@pytest.mark.parametrize('removed', [None, 'display', 'voip', 'voice', 'timers', 'wake_word', 'music', 'sendspin'])
@pytest.mark.timeout(180)
def test_first_generation_round_feature_composition(tmp_path, removed):
    features = [f for f in FULL + ['display'] if f not in ('status_led', removed)]
    profile = 'yamls/experimental/waveshare-s3-touch-lcd-1.85c/waveshare-s3-touch-lcd-1.85c-box-full-afe.yaml'
    config = device_config(tmp_path, features, profile)
    result = subprocess.run([sys.executable, '-m', 'esphome', 'config', '--no-defaults', str(config)],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Could not resolve substitution' not in result.stderr, result.stderr
    expanded = tmp_path / 'expanded.yaml'
    expanded.write_text(result.stdout)
    document = yaml_util.load_yaml(expanded)
    assert 'esp_aec' in document and 'esp_afe' not in document
    assert ('voip_stack' in document) is ('voip' in features)
    assert ('voice_assistant' in document) is ('voice' in features)
    assert ('lvgl' in document) is ('display' in features)

    if 'voip' in features and 'display' in features:
        from tests.native_profile import action_id

        scripts = {str(item['id']): item for item in document['script']}
        for bridge, target in (('call_to_settings', 'settings_open'), ('call_to_va', 'swipe_to_va')):
            assert target in scripts
            assert [action_id(action, 'script.execute') for action in scripts[bridge]['then']] == [target]
    if removed is None:
        generated = subprocess.run(
            [sys.executable, '-m', 'esphome', 'compile', '--only-generate', str(config)],
            capture_output=True, text=True, timeout=120,
        )
        assert generated.returncode == 0, generated.stdout + generated.stderr


@pytest.mark.parametrize('profile_path', [
    'yamls/voip-only/single-bus/spotpear-ball-v2-voip.yaml',
    'yamls/experimental/waveshare-s3-touch-lcd-1.85c/waveshare-s3-touch-lcd-1.85c-box-voip-only-afe.yaml',
])
@pytest.mark.parametrize('features', [['voip', 'display', 'status_led', 'diagnostics'], ['voip'], []])
@pytest.mark.timeout(180)
def test_standalone_display_phone_feature_composition(tmp_path, profile_path, features):
    config = device_config(tmp_path, features, profile_path)
    result = subprocess.run([sys.executable, '-m', 'esphome', 'compile', '--only-generate', str(config)],
                            capture_output=True, text=True, timeout=150)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Could not resolve substitution' not in result.stderr, result.stderr


@pytest.mark.parametrize('codec', ['jpeg', 'h264'])
@pytest.mark.parametrize('features', [['voip', 'display', 'video'], ['voip', 'display'], ['voip'], []])
@pytest.mark.timeout(180)
def test_p4_standalone_phone_keeps_codec_and_display_optional(tmp_path, codec, features):
    profile = f'yamls/voip-only/single-bus/waveshare-p4-touch-videophone-{codec}.yaml'
    config = device_config(tmp_path, features, profile)
    result = subprocess.run([sys.executable, '-m', 'esphome', 'compile', '--only-generate', str(config)],
                            capture_output=True, text=True, timeout=150)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Could not resolve substitution' not in result.stderr, result.stderr
    generated = next(tmp_path.rglob('main.cpp')).read_text()
    assert ('voip_stack::VoipStack' in generated) is ('voip' in features)
    assert ('esp_video_camera::ESPVideoCamera' in generated) is ('video' in features)
    assert ('esp_h264_video_source::' in generated) is ('video' in features and codec == 'h264')
    assert ('esp_jpeg_video_source::' in generated) is ('video' in features and codec == 'jpeg')


@pytest.mark.parametrize('features', [['voip'], []])
@pytest.mark.timeout(240)
def test_voice_pe_overlay_preserves_the_official_audio_owner(tmp_path, features):
    config = device_config(tmp_path, features,
                           'yamls/experimental/home-assistant-voice-pe/home-assistant-voice-pe-voip.yaml')
    result = subprocess.run([sys.executable, '-m', 'esphome', 'config', '--no-defaults', str(config)],
                            capture_output=True, text=True, timeout=210)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Could not resolve substitution' not in result.stderr, result.stderr
    expanded = tmp_path / 'expanded.yaml'
    expanded.write_text(result.stdout)
    document = yaml_util.load_yaml(expanded)
    assert ('voip_stack' in document) is ('voip' in features)
    assert 'esp_audio_stack' not in document
    assert 'esp_aec' not in document and 'esp_afe' not in document
    assert 'voice_assistant' in document


def test_full_ota_stops_music_and_announcements_independently(tmp_path):
    config = device_config(tmp_path, FULL)
    result = subprocess.run([sys.executable, '-m', 'esphome', 'config', '--no-defaults', str(config)],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    expanded = tmp_path / 'expanded.yaml'
    expanded.write_text(result.stdout)
    document = yaml_util.load_yaml(expanded)
    commands = [action['media_player.stop'] for action in maintenance_begin_actions(document)
                if 'media_player.stop' in action]
    assert len(commands) == 2
    assert [command.get('announcement', False) for command in commands] == [True, False]


def test_local_audio_provider_requests_https_certificate_bundle(tmp_path):
    config = device_config(tmp_path, ['voice'])
    result = subprocess.run([sys.executable, '-m', 'esphome', 'compile', '--only-generate', str(config)],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    generated = '\n'.join(p.read_text() for p in tmp_path.rglob('sdkconfig*') if p.is_file())
    assert 'CONFIG_MBEDTLS_CERTIFICATE_BUNDLE=y' in generated
    assert 'CONFIG_ESP_TLS_SKIP_SERVER_CERT_VERIFY=y' not in generated


@pytest.mark.parametrize('features', [['voip'], ['voip', 'display', 'video']])
def test_p4_local_idf_dependency_has_one_owner(tmp_path, features):
    config = device_config(tmp_path, features,
                           'yamls/voip-only/single-bus/waveshare-p4-touch-videophone-jpeg.yaml')
    document = yaml_util.load_yaml(config)
    document['substitutions']['intercom_idf_root'] = str(ROOT / 'idf_components')
    document['substitutions']['intercom_idf_ref'] = 'dev'
    config.write_text(yaml_util.dump(document))
    result = subprocess.run([sys.executable, '-m', 'esphome', 'config', '--no-defaults', str(config)],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    expanded = tmp_path / 'expanded.yaml'
    expanded.write_text(result.stdout)
    document = yaml_util.load_yaml(expanded)
    ppa = [item for item in document['esp32']['framework']['components'] if item['name'] == 'esp_driver_ppa']
    assert ppa == [{'name': 'esp_driver_ppa', 'path': str(ROOT / 'idf_components/esp_driver_ppa')}]
    csi = [item for item in document['esp32']['framework']['components'] if item['name'] == 'esp_video_csi_compat']
    assert csi == ([{'name': 'esp_video_csi_compat', 'path': str(ROOT / 'idf_components/esp_video_csi_compat')}]
                   if 'video' in features else [])


def test_local_gmf_override_only_binds_the_selected_afe(tmp_path):
    config = device_config(tmp_path, ['voice'])
    document = yaml_util.load_yaml(config)
    source = tmp_path / 'gmf'
    source.mkdir()
    document['substitutions']['gmf_ai_audio_source'] = str(source)
    config.write_text(yaml_util.dump(document))
    result = subprocess.run([sys.executable, '-m', 'esphome', 'config', '--no-defaults', str(config)],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    expanded = tmp_path / 'expanded.yaml'
    expanded.write_text(result.stdout)
    assert yaml_util.load_yaml(expanded)['esp_afe']['gmf_source'] == str(source)


@pytest.mark.parametrize('profile_path', [
    'full-experience/single-bus/waveshare-s3-full-afe.yaml',
    'full-experience/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-full-afe.yaml',
    'full-experience/single-bus/generic-s3-full-aec.yaml',
    'voip-only/single-bus/waveshare-s3-voip.yaml',
    'full-experience/esphome-native/generic-s3-full-esphome-native.yaml',
])
def test_resampler_uses_official_provider_for_every_audio_backend(profile_path: str) -> None:
    from tests.native_profile import profile

    document = profile(profile_path)
    providers = [item['source'] for item in document.get('external_components', [])
                 if 'resampler' in item.get('components', [])]
    assert not providers, 'Audio Stack and native audio both use the official resampler'
    assert any(speaker['platform'] == 'resampler' for speaker in document['speaker'])


@pytest.mark.parametrize('backend', [
    path for path in sorted((ROOT / 'packages/audio/profiles').glob('*.yaml'))
    if 'esp_audio_stack' in yaml_util.load_yaml(path)
], ids=lambda path: path.stem)
def test_audio_stack_backend_does_not_override_the_official_resampler(backend: Path) -> None:
    document = yaml_util.load_yaml(backend)
    providers = [item for item in document.get('external_components', [])
                 if 'esp_audio_stack' in item.get('components', [])]
    assert len(providers) == 1
    assert providers[0]['source'] == '${audio_stack_components_source}'
    assert 'resampler' not in providers[0]['components']


@pytest.mark.parametrize('features', [FULL, ['voip'], ['voice'], ['music']])
def test_ws3_uses_official_resampler_scheduling_configuration(tmp_path: Path, features: list[str]) -> None:
    config = device_config(tmp_path, features)
    result = subprocess.run(
        [sys.executable, '-m', 'esphome', 'config', '--no-defaults', str(config)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    expanded = tmp_path / 'expanded.yaml'
    expanded.write_text(result.stdout)
    document = yaml_util.load_yaml(expanded)
    assert document['esp_afe']['feed_task_core'] == 0
    resamplers = [speaker for speaker in document['speaker'] if speaker['platform'] == 'resampler']
    assert resamplers
    for speaker in resamplers:
        assert 'task_core' not in speaker, speaker['id']
        assert 'task_priority' not in speaker, speaker['id']
