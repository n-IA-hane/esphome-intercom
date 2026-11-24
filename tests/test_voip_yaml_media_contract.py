"""Contracts shared by maintained ESPHome VoIP media profiles."""

from pathlib import Path
import os
import re
import subprocess
import sys

import pytest
import esphome.config_validation as cv

from tests.native_profile import profile, by_id, nodes, action_id, text as values_text


ROOT = Path(__file__).resolve().parents[1]
VOIP_COMPONENTS = Path(os.environ.get("VOIP_STACK_REPO", ROOT.parent / "esphome-voip-stack")) / "esphome" / "components"
P4_RENDERER_COMPONENT = Path(os.environ.get(
    "ESP_VIDEO_RENDERER_REPO", ROOT.parent / "esphome-video-renderer"
)) / "esphome/components/p4_video_renderer"
H264_SOURCE_COMPONENT = VOIP_COMPONENTS / "esp_h264_video_source"
JPEG_SOURCE_COMPONENT = VOIP_COMPONENTS / "esp_jpeg_video_source"
pytestmark = [pytest.mark.architecture, pytest.mark.timeout(240)]
YAMLS = ROOT / "yamls"

PHYSICAL_AUDIO_STACK_PROFILES = (
    "experimental/waveshare-s3-touch-lcd-1.85c/waveshare-s3-touch-lcd-1.85c-box-full-afe.yaml",
    "full-experience/dual-bus/generic-s3-full-aec.yaml",
    "full-experience/single-bus/generic-s3-full-aec.yaml",
    "full-experience/single-bus/spotpear-ball-v2-full-afe.yaml",
    "full-experience/single-bus/waveshare-p4-touch-full-afe-landscape-videophone-jpeg.yaml",
    "full-experience/single-bus/waveshare-p4-touch-full-afe-portrait.yaml",
    "full-experience/single-bus/waveshare-s3-full-afe.yaml",
    "untested/generic-s3-full-afe.yaml",
    "voip-only/dual-bus/generic-s3-voip.yaml",
    "voip-only/single-bus/generic-s3-voip.yaml",
    "voip-only/single-bus/spotpear-ball-v2-voip.yaml",
)

SPOTPEAR_FULL_AFE = (
    YAMLS / "full-experience" / "single-bus" / "spotpear-ball-v2-full-afe.yaml"
)
P4_LANDSCAPE_FULL_AFE = (
    YAMLS
    / "full-experience"
    / "single-bus"
    / "waveshare-p4-touch-full-afe-landscape-videophone-jpeg.yaml"
)
P4_FULL_JPEG_PACKAGE = ROOT / "packages" / "voip" / "p4_full_video_jpeg.yaml"
HA_PHONE_PACKAGE = ROOT / "packages" / "voip" / "ha_phone.yaml"
VOIP_ONLY_PACKAGE = ROOT / "packages" / "voip_only.yaml"
P4_VIDEOPHONE_BASE = ROOT / "packages" / "voip" / "waveshare_p4_touch_videophone_base.yaml"
RINGTONE_ORCHESTRATION = ROOT / "packages" / "runtime" / "voip_ringtone_orchestration.yaml"
RUNTIME_MEDIA_PLAYER = (
    ROOT
    / "packages"
    / "media_player"
    / "runtime_controller_mono_media_player_48k.yaml"
)
SENDSPIN_ARTWORK = ROOT / "packages" / "media_player" / "sendspin_artwork.yaml"


def _voip_stack_block(text: str) -> str:
    match = re.search(r"(?m)^voip_stack:\n", text)
    if match is None:
        return ""
    tail = text[match.end() :]
    next_top_level = re.search(r"(?m)^\S", tail)
    return tail if next_top_level is None else tail[: next_top_level.start()]


def _format_section(block: str, name: str) -> str:
    match = re.search(rf"(?m)^    {re.escape(name)}:\n", block)
    if match is None:
        return ""
    tail = block[match.end() :]
    next_peer = re.search(r"(?m)^    \S", tail)
    return tail if next_peer is None else tail[: next_peer.start()]


def _has_s16le_mono_format(section: str, sample_rate: int, frame_ms: int) -> bool:
    entries = re.split(r"(?m)^      - ", section)
    return any(
        re.search(rf"(?m)^sample_rate:\s*{sample_rate}\s*$", entry)
        and re.search(r"(?m)^        pcm_format:\s*s16le\s*$", entry)
        and re.search(r"(?m)^        channels:\s*1\s*$", entry)
        and re.search(rf"(?m)^        frame_ms:\s*{frame_ms}\s*$", entry)
        for entry in entries[1:]
    )


def test_retired_ha_packages_fail_validation_instead_of_registering_duplicates() -> None:
    for name in ("ha_phone", "ha_integration", "ha_actions", "ha_api", "ha_api_runtime"):
        text = (ROOT / "packages/voip" / f"{name}.yaml").read_text()
        assert f"legacy_ha_package: {name}" in text
        assert "actions:" not in text


def test_retired_directory_and_media_route_packages_are_removed() -> None:
    for relative in ("packages/voip/phonebook_subscribe.yaml",
                     "packages/lvgl/p4_call_media_route.yaml",
                     "packages/lvgl/spotpear_call_media_route.yaml"):
        assert not (ROOT / relative).exists()
    for relative in ("packages/features/presentation/devices/waveshare-p4-phone.yaml",
                     "packages/features/presentation/devices/waveshare-p4-full-landscape.yaml",
                     "packages/features/presentation/devices/spotpear-ball-v2-phone.yaml",
                     "packages/features/voip/presentation/round.yaml",
                     "packages/voip/p4_full_video_jpeg.yaml"):
        text = (ROOT / relative).read_text()
        assert "call_media_route" not in text and "video_media_route_label" not in text


def test_physical_phone_presets_use_native_ha_surface() -> None:
    assert "ha_phone: !include" not in (ROOT / "packages/voip_only.yaml").read_text()
    assert "use_ha_as_first_contact: true" in (ROOT / "packages/voip_only.yaml").read_text()


def test_flac_ringtone_stop_preserves_pipeline_ownership() -> None:
    """Stopping a ring must clear its playlist without stopping call playback."""
    media = (ROOT / "packages/voip/ringtone_asset.yaml").read_text()
    orchestration = RINGTONE_ORCHESTRATION.read_text()

    assert "assets/sounds/ringtone.flac" in media
    assert "assets/sounds/ringtone.wav" not in media
    assert "id: tts_mixer_input" in orchestration
    assert "id: voip_mixer_input" not in orchestration
    assert "id: hw_speaker" not in orchestration
    assert "media_player.stop:" in orchestration
    assert "announcement: true" in orchestration
    assert "media_source::MediaSourceCommand::STOP" not in orchestration


def test_runtime_media_player_profiles_share_ringtone_lifecycle_owner() -> None:
    checked = 0
    for relative in PHYSICAL_AUDIO_STACK_PROFILES:
        config = profile(relative)
        if 'runtime_controller' not in config or 'voip_stack' not in config:
            continue
        scripts = config.get('script', [])
        for name in ('voip_start_ringtone', 'voip_stop_ringtone'):
            assert sum(script['id'] == name for script in scripts) == 1
        checked += 1
    assert checked >= 6


def test_resampling_profiles_accept_direct_esp_16khz_10ms() -> None:
    checked = 0
    for relative in (*PHYSICAL_AUDIO_STACK_PROFILES,
                     'full-experience/esphome-native/generic-s3-full-esphome-native.yaml'):
        audio = profile(relative).get('voip_stack', {}).get('audio', {})
        formats = [fmt for codec in audio.get('codecs', [])
                   if codec['codec'] == 'pcm' for fmt in codec.get('rx', [])]
        if formats:
            assert any((f['sample_rate'], f['pcm_format'], f['channels'], f['frame_ms'])
                       == (16000, 's16le', 1, 10) for f in formats), relative
            checked += 1
    assert checked >= 3


@pytest.mark.parametrize('relative', [
    'full-experience/single-bus/waveshare-s3-full-afe.yaml',
    'full-experience/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-full-afe.yaml',
    'full-experience/single-bus/waveshare-p4-touch-full-afe-landscape-videophone-jpeg.yaml',
    'full-experience/single-bus/waveshare-p4-touch-full-afe-portrait.yaml',
])
def test_full_profiles_declare_independent_pcm_preferences(relative: str) -> None:
    audio = profile(relative)['voip_stack']['audio']
    assert not {'tx', 'rx', 'tx_formats', 'rx_formats'} & audio.keys()
    assert len(audio['codecs']) == 1
    pcm = audio['codecs'][0]
    assert pcm['codec'] == 'pcm'
    assert [(f['sample_rate'], f['frame_ms']) for f in pcm['tx']] == [
        (16000, 20), (16000, 10), (48000, 10),
    ]
    assert [(f['sample_rate'], f['frame_ms']) for f in pcm['rx']] == [
        (48000, 10), (48000, 20), (16000, 20), (16000, 10),
    ]


def test_box_v1_retains_native_tx_and_real_48k_receive_envelope() -> None:
    phone = profile('experimental/waveshare-s3-touch-lcd-1.85c/waveshare-s3-touch-lcd-1.85c-box-voip-only-afe.yaml')
    pcm = next(codec for codec in phone['voip_stack']['audio']['codecs'] if codec['codec'] == 'pcm')
    assert [(fmt['sample_rate'], fmt['frame_ms']) for fmt in pcm['tx']] == [(16000, 20), (16000, 10), (48000, 10)]
    assert [(fmt['sample_rate'], fmt['frame_ms']) for fmt in pcm['rx']] == [(48000, 10), (48000, 20), (16000, 20), (16000, 10)]


def test_physical_audio_stack_speakers_bound_silent_tx_lifecycle() -> None:
    for relative in PHYSICAL_AUDIO_STACK_PROFILES:
        physical = [s for s in profile(relative)['speaker'] if s.get('platform') == 'esp_audio_stack']
        assert physical, relative
        assert all(s['timeout'] == '1s' for s in physical), relative


def test_ws3_uses_native_esp32_partition_configuration() -> None:
    relative = ('experimental/waveshare-s3-touch-lcd-1.85c/'
                'waveshare-s3-touch-lcd-1.85c-box-full-afe.yaml')
    config = profile(relative)
    assert str(config['esp32']['partitions']).endswith('partitions_16mb_huge_factory.csv')
    assert 'board_build.partitions' not in config['esphome'].get('platformio_options', {})


def test_spotpear_round_dialer_preserves_memory_and_routing_contracts() -> None:
    config = profile(SPOTPEAR_FULL_AFE)
    assert config['lvgl']['buffer_size'] in ('50%', 0.5)
    assert config['runtime_controller']['storage_in_psram'] is True
    assert by_id(config['globals'], 'voip_dial_buffer')['type'] == 'std::array<char, 33>'
    assert by_id(config['globals'], 'voip_dial_length')['type'] == 'uint8_t'
    dialer = by_id(config['lvgl']['pages'], 'call_dialer_page')
    assert sum('buttonmatrix' in node for node in nodes(dialer)) == 4
    for name in ('call_dial_row_123', 'call_dial_row_456', 'call_dial_row_789',
                 'call_dial_row_del0hash', 'call_dial_call_btn'):
        assert by_id(dialer, name)
    assert any(node.get('text') == '\U000F006E' for node in nodes(dialer))
    assert 'voip_dial_backspace' in values_text(dialer)
    assert "if (x == 2) return '#';" in values_text(dialer)
    row = by_id(dialer, 'call_dial_row_del0hash')
    assert [button['text'] for button in row['rows'][0]['buttons']] == ['*', '0', '#', '\U000F006E']
    dispatch = values_text(row['on_click'])
    for route in ("return x == 3;", "if (x == 0) return '*';",
                  "if (x == 1) return '0';", "if (x == 2) return '#';"):
        assert route in dispatch
    code = values_text(config['script'])
    assert 'circle_half_chord' not in code
    contacts = by_id(config['phone_list_view'], 'contacts_list_view')
    assert 'id(phone).get_contact_count()' in values_text(contacts['row_count'])
    assert 'get_contacts_csv()' not in code


def test_spotpear_concurrency_memory_policy_is_explicit_and_preallocated() -> None:
    config = profile(SPOTPEAR_FULL_AFE)
    for name in ('media_http_source', 'announcement_http_source'):
        source = by_id(config['media_source'], name)
        assert int(source['buffer_size']) == 32768
        assert cv.boolean(source['persistent_ring_buffer']) is True
        assert cv.boolean(source['task_stack_in_psram']) is True
    assert int(by_id(config['media_source'], 'sendspin_media_source')['buffer_size']) == 262144
    phone = config['voip_stack']
    assert cv.boolean(phone['audio_task_stacks_in_psram']) is True
    assert int(phone['tx_task_stack_size']) == 12288
    assert int(phone['rx_task_stack_size']) == 12288
    providers = {name for provider in config['external_components'] for name in provider['components']}
    assert 'voice_assistant' in config
    assert {'voice_assistant', 'speaker_source', 'audio_http'} <= providers
    assert 'speaker' not in providers
    assert by_id(config['display'], 's3_box_lcd')['psram_dma'] is True


@pytest.mark.parametrize('relative', [
    'voip-only/single-bus/spotpear-ball-v2-voip.yaml',
    'voip-only/single-bus/waveshare-s3-voip.yaml',
])
def test_standalone_ringtone_uses_the_file_source_and_player_owner(relative) -> None:
    config = profile(relative)
    player = by_id(config['media_player'], 'speaker_media_player')
    assert player['platform'] == 'speaker_source'
    assert float(player['volume_initial']) == 1.0
    pipeline = player['announcement_pipeline']
    assert pipeline['sources'] == ['ringtone_file_source']
    assert int(pipeline['sample_rate']) == 48000
    assert int(pipeline['num_channels']) == 1
    assert by_id(config['media_source'], 'ringtone_file_source')['platform'] == 'audio_file'
    assert by_id(config['audio_file'], 'voip_ringing_sound')
    ring = by_id(config['script'], 'voip_ringing_loop')
    plays = [node['media_player.play_media'] for node in nodes(ring)
             if 'media_player.play_media' in node]
    assert len(plays) == 1
    assert plays[0]['media_url'] == 'audio-file://voip_ringing_sound'
    assert cv.boolean(plays[0]['announcement']) is True
    stops = [node['media_player.stop'] for node in nodes(config['script'])
             if 'media_player.stop' in node]
    assert any(cv.boolean(stop['announcement']) for stop in stops)
    assert 'media_player.speaker.play_on_device_media_file' not in values_text(ring)


def test_sendspin_artwork_uses_the_official_image_platform() -> None:
    text = SENDSPIN_ARTWORK.read_text()

    assert "image:\n  - platform: sendspin" in text
    assert "current_image:\n      id: sendspin_cover_art" in text
    assert "generic_image:" not in text
    assert "github://pr#16057" not in text


def test_spotpear_layout_and_aec_sync_use_runtime_contracts() -> None:
    config = profile(SPOTPEAR_FULL_AFE)
    boot = values_text(config['esphome'].get('on_boot', []))
    assert 'compute_layout_metrics' not in boot and 'reposition_widgets' not in boot
    actions = config['lvgl']['on_boot']
    if isinstance(actions, dict):
        actions = actions['then']
    boot_scripts = [action_id(action, 'script.execute') for action in actions]
    metrics_index = boot_scripts.index('compute_layout_metrics')
    reposition_index = boot_scripts.index('reposition_widgets')
    assert metrics_index < reposition_index
    assert 'id(s3_video).attach_video_container(id(call_video_surface));' in values_text(actions[:metrics_index])
    options = by_id(config['lvgl']['pages'], 'ui_aec_mode_dd')['options']
    assert options == ['sr_low_cost', 'sr_high_perf']
    assert options == by_id(config['select'], 'aec_mode_select')['options']
    mode_sync = values_text(by_id(config['script'], 'settings_sync_mode'))
    assert 'id(aec_mode_select).current_option()' in mode_sync
    assert 'lvgl.dropdown.update' in mode_sync and 'get_selected_index()' in mode_sync
    assert 'lv_dropdown_set_options' not in mode_sync


def _master_volume_boot_writes(boot, scripts=()):
    """Find number controls, not getter/callback references, reachable at boot."""
    declarations = {str(item['id']): item.get('then', []) for item in scripts if 'id' in item}
    pending = [boot]
    visited = set()
    writes = []
    master = r'id\s*\(\s*master_volume\s*\)\s*\.\s*'
    while pending:
        for node in nodes(pending.pop()):
            for operation in ('set', 'increment', 'decrement', 'to_min', 'to_max'):
                action = 'number.' + operation
                if action in node and action_id(node, action) == 'master_volume':
                    writes.append(action)
            if 'script.execute' in node:
                target = str(action_id(node, 'script.execute'))
                if target in declarations and target not in visited:
                    visited.add(target)
                    pending.append(declarations[target])
            if 'lambda' not in node:
                continue
            code = str(node['lambda'])
            if re.search(master + r'(?:control|publish_state)\s*\(', code):
                writes.append('master_volume direct mutation')
            if re.search(r'\bset_master_volume(?:_q31)?\s*\(', code):
                writes.append('audio owner master setter')
            if re.search(master + r'make_call\s*\(\s*\)[^;]*\.\s*perform\s*\(', code):
                writes.append('master_volume NumberCall.perform')
            for variable in re.findall(r'\b(\w+)\s*=\s*' + master + r'make_call\s*\(\s*\)', code):
                if re.search(r'\b' + re.escape(variable) + r'\s*\.\s*perform\s*\(', code):
                    writes.append('master_volume staged NumberCall.perform')
            for target in re.findall(r'id\s*\(\s*(\w+)\s*\)\s*\.\s*execute\s*\(', code):
                if target in declarations and target not in visited:
                    visited.add(target)
                    pending.append(declarations[target])
    return writes


@pytest.mark.parametrize('boot,expected', [
    ([{'lambda': 'id(master_volume).add_on_state_callback([](float) { id(sync).execute(); });'}], False),
    ([{'lambda': 'const float value = id(master_volume).state;'}], False),
    ([{'lambda': 'auto unused = id(master_volume).make_call();'}], False),
    ([{'number.set': {'id': 'mic_gain', 'value': 0}}], False),
    *[([{'number.' + action: {'id': 'master_volume', 'value': 1}}], True)
      for action in ('set', 'increment', 'decrement', 'to_min', 'to_max')],
    ([{'lambda': 'id(master_volume).make_call().set_value(1).perform();'}], True),
    ([{'lambda': 'auto call = id(master_volume).make_call(); call.set_value(1); call.perform();'}], True),
    ([{'lambda': 'id(audio_stack).set_master_volume(0.01f);'}], True),
    ([{'lambda': 'id(master_volume).publish_state(1);'}], True),
])
def test_master_volume_boot_oracle_distinguishes_controls_from_observers(boot, expected):
    assert bool(_master_volume_boot_writes(boot)) is expected


def test_master_volume_boot_oracle_follows_called_scripts():
    scripts = [{'id': 'set_defaults', 'then': [{'number.set': {'id': 'master_volume', 'value': 1}}]}]
    assert _master_volume_boot_writes([{'script.execute': 'set_defaults'}], scripts)
    assert _master_volume_boot_writes([{'lambda': 'id(set_defaults).execute();'}], scripts)


def test_p4_full_profile_has_native_camera_and_sip_jpeg() -> None:
    config = profile(P4_LANDSCAPE_FULL_AFE)
    providers = {name for provider in config['external_components'] for name in provider['components']}
    assert 'voice_assistant' in config
    assert {'voice_assistant', 'speaker_source', 'audio_http'} <= providers
    assert {'esp_video_camera', 'video_renderer',
            'esp_jpeg_video_source', 'p4_video_renderer'} <= providers
    assert 'mipi_dsi' in providers
    assert any(display['platform'] == 'mipi_dsi' for display in config['display'])
    assert 'speaker' not in providers
    assert 'esp_h264_video_source' not in providers
    camera = config['esp_video_camera']
    for key, expected in {'id': 'p4_camera', 'name': 'P4 Camera', 'i2c_id': 'internal_i2c',
                          'device': 'jpeg', 'resolution': '800x800', 'jpeg_quality': 10,
                          'max_framerate': 10, 'rotation': 270, 'buffer_count': 2}.items():
        assert (float(camera[key]) if key == 'max_framerate' else camera[key]) == expected
    assert config['esp_afe']['output_prebuffer_frames'] == 1
    assert 'gmf_output_delay_samples' not in config['esp_afe']
    video = config['voip_stack']['video']
    assert (video['codec'], int(video['width']), int(video['height']), int(video['framerate'])) == ('jpeg', 800, 800, 10)
    assert not config['voip_stack'].get('video_debug', False)
    assert config['voip_stack']['audio_task_stacks_in_psram'] is True
    assert by_id(config['lvgl']['pages'], 'call_video_surface')
    code = values_text(config['script'])
    contacts = by_id(config['phone_list_view'], 'contacts_list_view')
    assert 'id(phone).get_contact_count()' in values_text(contacts['row_count'])
    assert 'get_contacts_csv()' not in code
    boot = config['esphome'].get('on_boot', [])
    assert not _master_volume_boot_writes(boot, config['script']), 'Boot must not overwrite master volume'
    assert 'id(master_volume).add_on_state_callback' in values_text(boot)
    video_control_pages = [page for page in config['lvgl']['pages']
                           if any(node.get('id') == 'ui_video_send_sw' for node in nodes(page))]
    assert [page['id'] for page in video_control_pages] == ['settings_p4_page']
    video_control = by_id(video_control_pages[0], 'ui_video_send_sw')
    assert 'on_change' in video_control and 'on_value' not in video_control
    video_commands = values_text(video_control['on_change'])
    assert 'id(voip_video_send).turn_on()' in video_commands
    assert 'id(voip_video_send).turn_off()' in video_commands
    video_sync = values_text(by_id(config['script'], 'settings_sync_video_send'))
    assert 'id(voip_video_send).state' in video_sync
    assert 'turn_on()' not in video_sync and 'turn_off()' not in video_sync
    video_entities = (ROOT / 'packages/voip/video_entities.yaml').read_text()
    assert 'video_send:' in video_entities and 'restore_mode: RESTORE_DEFAULT_ON' in video_entities
    sdk = config['esp32']['framework']['sdkconfig_options']
    assert sdk['CONFIG_ESP_HOSTED_DFLT_TASK_FROM_SPIRAM'] == 'y'
    assert sdk['CONFIG_SPIRAM_ALLOW_STACK_EXTERNAL_MEMORY'] == 'y'
    assert sdk['CONFIG_ESP_HOSTED_SDIO_TX_Q_SIZE'] == '20'
    assert sdk['CONFIG_ESP_HOSTED_SDIO_RX_Q_SIZE'] == '20'
    hosted = (ROOT / 'packages/board/esp32p4_c6_sdio.yaml').read_text()
    assert 'platform: esp32_hosted' not in hosted and 'http_request:' not in hosted
    update = (ROOT / 'packages/board/esp32p4_c6_update.yaml').read_text()
    assert 'platform: esp32_hosted' in update and 'http_request:' in update and 'update_interval: 6h' in update


def test_p4_full_jpeg_framerate_override_reaches_camera_and_media_owners(tmp_path) -> None:
    from esphome import yaml_util
    from tests.test_package_composition import device_config

    original = yaml_util.load_yaml(P4_LANDSCAPE_FULL_AFE)
    path = device_config(tmp_path, original['substitutions']['firmware_features'],
                         str(P4_LANDSCAPE_FULL_AFE.relative_to(ROOT)))
    document = yaml_util.load_yaml(path)
    document['substitutions']['p4_sip_jpeg_fps'] = '20'
    path.write_text(yaml_util.dump(document))
    expanded = subprocess.run([sys.executable, '-m', 'esphome', 'config', '--no-defaults', str(path)],
                              capture_output=True, text=True, timeout=180)
    assert expanded.returncode == 0, expanded.stdout + expanded.stderr
    resolved_path = tmp_path / 'expanded.yaml'
    resolved_path.write_text(expanded.stdout)
    resolved = yaml_util.load_yaml(resolved_path)
    assert float(resolved['esp_video_camera']['max_framerate']) == 20
    assert int(resolved['esp_jpeg_video_source']['framerate']) == 20
    assert int(resolved['p4_video_renderer']['framerate']) == 20
    assert int(resolved['voip_stack']['video']['framerate']) == 20
    assert resolved['esp_video_camera']['resolution'] == '800x800'
    assert resolved['esp_video_camera']['jpeg_quality'] == 10
    assert resolved['esp_video_camera']['buffer_count'] == 2


@pytest.mark.parametrize('entrypoint', [
    'waveshare-s3-touch-lcd-1.85c-box-v2-full.yaml',
    'waveshare-s3-touch-lcd-1.85c-box-v2-full-afe.yaml',
])
@pytest.mark.parametrize('override', [None, 20])
def test_ws3_full_public_profile_includes_qualified_jpeg_receive(tmp_path, entrypoint, override) -> None:
    from esphome import yaml_util
    from tests.test_package_composition import device_config

    relative = 'yamls/full-experience/single-bus/' + entrypoint
    original = yaml_util.load_yaml(ROOT / relative)
    path = device_config(tmp_path, original['substitutions']['firmware_features'], relative)
    if override is not None:
        document = yaml_util.load_yaml(path)
        document['substitutions']['s3_sip_jpeg_fps'] = str(override)
        path.write_text(yaml_util.dump(document))
    expanded = subprocess.run([sys.executable, '-m', 'esphome', 'config', '--no-defaults', str(path)],
                              capture_output=True, text=True, timeout=180)
    assert expanded.returncode == 0, expanded.stdout + expanded.stderr
    resolved_path = tmp_path / 'expanded.yaml'
    resolved_path.write_text(expanded.stdout)
    resolved = yaml_util.load_yaml(resolved_path)
    renderer = resolved['esp_video_renderer']
    expected_rate = override or 10
    assert (int(renderer['width']), int(renderer['height']), int(renderer['framerate'])) == (240, 240, expected_rate)
    assert renderer['id'] == 's3_video' and 'voip' not in renderer
    video = resolved['voip_stack']['video']
    assert (video['codec'], video['sink']) == ('jpeg', renderer['id'])
    assert (int(video['width']), int(video['height']), int(video['framerate'])) == (240, 240, expected_rate)
    assert by_id(resolved['lvgl']['pages'], 'call_video_page')
    assert by_id(resolved['lvgl']['pages'], 'call_video_surface')
    assert resolved['voip_stack']['on_video_start'] == [{'script.execute': 'suspend_mww_for_video'}]
    assert resolved['voip_stack']['on_video_end'] == [{'script.execute': 'resume_mww_after_video'}]
    assert any('micro_wake_word.stop' in action
               for action in by_id(resolved['script'], 'suspend_mww_for_video')['then'])


@pytest.mark.parametrize('without', ['display', 'voip'])
def test_ws3_public_profile_omits_jpeg_receiver_without_both_features(tmp_path, without) -> None:
    from esphome import yaml_util
    from tests.test_package_composition import device_config

    relative = 'yamls/full-experience/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-full.yaml'
    original = yaml_util.load_yaml(ROOT / relative)
    removed = {without} | ({'directory', 'blf', 'mwi'} if without == 'voip' else set())
    features = [name for name in original['substitutions']['firmware_features'] if name not in removed]
    path = device_config(tmp_path, features, relative)
    expanded = subprocess.run([sys.executable, '-m', 'esphome', 'config', '--no-defaults', str(path)],
                              capture_output=True, text=True, timeout=180)
    assert expanded.returncode == 0, expanded.stdout + expanded.stderr
    resolved_path = tmp_path / 'expanded.yaml'
    resolved_path.write_text(expanded.stdout)
    resolved = yaml_util.load_yaml(resolved_path)
    assert 'esp_video_renderer' not in resolved
    assert 'video' not in resolved.get('voip_stack', {})
    providers = {name for provider in resolved['external_components'] for name in provider['components']}
    assert 'esp_video_renderer' not in providers


def test_p4_sip_only_uses_espressif_hosted_video_receive_depths() -> None:
    config = profile('voip-only/single-bus/waveshare-p4-touch-videophone-jpeg.yaml')
    sdk = config['esp32']['framework']['sdkconfig_options']
    for suffix, expected in {'STATIC_RX_BUFFER_NUM': '16', 'DYNAMIC_RX_BUFFER_NUM': '64',
                             'DYNAMIC_TX_BUFFER_NUM': '64', 'TX_BA_WIN': '32', 'RX_BA_WIN': '32'}.items():
        assert sdk['CONFIG_WIFI_RMT_' + suffix] == expected
    assert sdk['CONFIG_LWIP_TCPIP_RECVMBOX_SIZE'] == '64'
    assert not _master_volume_boot_writes(config['esphome']['on_boot'], config.get('script', []))
    assert tuple(int(config['esp_afe'][key]) for key in ('task_priority', 'feed_task_priority', 'fetch_task_priority')) == (18, 18, 18)


def test_p4_h264_prioritizes_afe_without_changing_jpeg_scheduler() -> None:
    profile_dir = YAMLS / "voip-only" / "single-bus"
    h264 = (
        profile_dir / "waveshare-p4-touch-videophone-h264.yaml"
    ).read_text()
    jpeg = (
        profile_dir / "waveshare-p4-touch-videophone-jpeg.yaml"
    ).read_text()

    assert 'p4_afe_task_priority: "20"' in h264
    assert 'p4_afe_feed_task_priority: "19"' in h264
    assert 'p4_afe_fetch_task_priority: "20"' in h264
    assert "p4_afe_task_priority" not in jpeg
    assert "p4_afe_feed_task_priority" not in jpeg
    assert "p4_afe_fetch_task_priority" not in jpeg


def test_p4_codec_profiles_have_isolated_generated_builds() -> None:
    paths = []
    for name in ('waveshare-p4-touch-videophone-jpeg', 'waveshare-p4-touch-videophone-h264'):
        config = profile('voip-only/single-bus/' + name + '.yaml')
        assert config['esphome']['build_path'].endswith('/' + name)
        paths.append(Path(config['esphome']['build_path']).name)
    full = profile(P4_LANDSCAPE_FULL_AFE)
    assert full['esphome']['build_path'].endswith('/waveshare-p4-touch-full-afe-landscape')
    paths.append(Path(full['esphome']['build_path']).name)
    assert len(set(paths)) == 3


def test_p4_production_profiles_keep_video_debug_off_and_safe_mode_on() -> None:
    dedicated = profile('voip-only/single-bus/waveshare-p4-touch-videophone-jpeg.yaml')
    full = profile(P4_LANDSCAPE_FULL_AFE)
    for config in (dedicated, full):
        assert not config['voip_stack'].get('video_debug', False)
        assert not config.get('safe_mode', {}).get('disabled', False)
        assert len([ota for ota in config['ota'] if ota['platform'] == 'esphome']) == 1
    maintenance = by_id(full['script'], 'firmware_maintenance')
    assert 'esp_audio_stack.stop' in values_text(maintenance)
    assert 'lvgl.pause' in values_text(maintenance)


def test_p4_idle_animation_uses_rendered_page_state() -> None:
    config = profile(P4_LANDSCAPE_FULL_AFE)
    draw = by_id(config['script'], 'draw_display')
    conditions = [node['lambda'] for node in nodes(draw) if 'lambda' in node
                  and 'main:idle:' in str(node['lambda'])]
    assert len(conditions) == 1
    assert 'id(runtime_rendered_page).rfind("main:idle:", 0) == 0' in conditions[0]
    assert 'lv_disp_get_scr_act' not in conditions[0]
    code = values_text(draw)
    for page in ('settings', 'no_wifi', 'no_ha', 'timer_finished'):
        assert f'id(runtime_rendered_page) = "{page}";' in code


def test_p4_idle_animation_has_generic_call_and_page_gates() -> None:
    config = profile(P4_LANDSCAPE_FULL_AFE)
    page = by_id(config['lvgl']['pages'], 'main_page')
    assert 'ai_animation_loop' in values_text(page['on_unload'])
    start = by_id(config['script'], 'ui_call_started')
    assert any(action_id(node, 'script.stop') == 'ai_animation_loop' for node in nodes(start))
    end = by_id(config['script'], 'ui_call_ended')
    assert action_id(end['then'][0], 'script.execute') == 'runtime_ui_call_end'
    assert 'id(current_mode) = id(previous_mode)' in values_text(by_id(config['script'], 'runtime_ui_call_end'))
    tail = end['then']
    delay_index = next(i for i, action in enumerate(tail) if action.get('delay') == '4s')
    assert any(action_id(node, 'script.execute') == 'draw_display' for node in nodes(tail[delay_index+1:]))


def test_p4_idle_assistant_shows_selected_wake_word() -> None:
    config = profile(P4_LANDSCAPE_FULL_AFE)
    assert any(node.get('text') == 'Push to talk or say:' for node in nodes(config['lvgl']))
    assert by_id(config['lvgl'], 'va_wake_word_label')
    code = values_text(config['script'])
    for expression in ('id(mww).get_wake_words()', 'model->is_enabled()', 'model->get_wake_word()'):
        assert expression in code


def test_p4_full_jpeg_video_page_has_dedicated_lifecycle() -> None:
    config = profile(P4_LANDSCAPE_FULL_AFE)
    renderer = config['p4_video_renderer']
    assert 'show_call_video_page' in values_text(renderer['on_first_frame'])
    assert 'hide_call_video_page' in values_text(renderer['on_video_ended'])
    show = by_id(config['script'], 'show_call_video_page')
    assert action_id(show['then'][0], 'script.stop') == 'draw_display'
    assert action_id(show['then'][1], 'script.stop') == 'ai_animation_loop'
    assert 'id(runtime_rendered_page) = "call_video";' in values_text(show)
    draw = values_text(by_id(config['script'], 'draw_display'))
    assert 'id(runtime_rendered_page) == "call_video"' in draw
    assert 'id(phone).is_in_call()' in draw
    assert 'runtime_ui_call_end' in values_text(by_id(config['script'], 'ui_call_ended'))
    for obsolete in ('waveshare-p4-touch-full-afe-landscape-jpeg-native-800.yaml',
                     'waveshare-p4-touch-full-afe-landscape-sip-jpeg.yaml'):
        assert not (YAMLS / 'full-experience/single-bus' / obsolete).exists()


def test_p4_videophone_contact_navigation_waits_for_owner_callback() -> None:
    config = profile('voip-only/single-bus/waveshare-p4-touch-videophone-jpeg.yaml')
    contacts = by_id(config['phone_list_view'], 'contacts_list_view')
    assert contacts['parent_id'] == 'phone_contacts_container'
    assert 'contact->stable_id()' in values_text(contacts['row_key'])
    assert 'row.set_id(contact->stable_id())' in values_text(contacts['render_row'])
    assert 'row.set_name(contact->name)' in values_text(contacts['render_row'])
    selection = contacts['on_select']
    assert 'if (!id(phone).is_idle()) return;' in values_text(selection)
    assert 'id(phone).call_contact(row_id, row_scope);' in values_text(selection)
    assert 'id(phone).pickup_contact(row_id, row_scope);' in values_text(selection)
    assert 'get_current_destination()' not in values_text(selection)
    assert not any(action_id(node, 'script.execute') == 'update_status' for node in nodes(selection))
    callback = config['voip_stack']['on_destination_changed']
    assert 'id(videophone_peer) = destination;' in values_text(callback)
    assert any(action_id(node, 'script.execute') == 'update_status' for node in nodes(callback))
    status = values_text(by_id(config['script'], 'update_status'))
    assert 'idle ? id(phone).get_current_destination() : id(phone).get_active_peer_name()' in status
    assert 'if (!idle) id(videophone_peer) = peer;' in status
    assert 'lv_label_set_text(id(videophone_peer_label), peer.c_str());' in status


def test_shared_h264_codec_dependency_and_encoder_memory_policy() -> None:
    renderer_config = (P4_RENDERER_COMPONENT / "__init__.py").read_text()
    neutral_codec = (P4_RENDERER_COMPONENT.parent / "video_renderer" / "__init__.py").read_text()
    dependency_config = (VOIP_COMPONENTS / "voip_stack" / "__init__.py").read_text()
    source_config = (H264_SOURCE_COMPONENT / "__init__.py").read_text()
    h264_package = (ROOT / "packages/voip/p4_video_h264.yaml").read_text()
    assert "video_renderer.add_h264_component()" in renderer_config
    assert "voip_stack.add_h264_component()" in source_config
    assert "from esphome.components.video_renderer import add_h264_component" in dependency_config
    assert 'url="https://github.com/n-IA-hane/esp-h264-component.git"' in neutral_codec
    assert 'ref="176cda67716c24deef0e76e4451048981e154bd4"' in neutral_codec
    assert 'name="espressif/esp_h264", path=str(repository / "esp_h264")' in neutral_codec
    assert "CONFIG_ESP_H264_HW_ENCODER_PREFER_DB_SPIRAM" not in h264_package
    assert 'add_idf_sdkconfig_option("CONFIG_ESP_H264_HW_ENCODER_PREFER_DB_SPIRAM", True)' in source_config


def _renderer_method(source: str, name: str) -> str:
    # Each out-of-line member closes at the start of a line.
    return source.split(f"P4VideoRenderer::{name}(", 1)[1].split("\n}", 1)[0]


def test_p4_video_workers_use_display_owned_mainloop_presentation() -> None:
    core = (P4_RENDERER_COMPONENT / "p4_video_renderer.cpp").read_text()
    jpeg = (P4_RENDERER_COMPONENT / "p4_jpeg_decoder.cpp").read_text()
    h264 = (P4_RENDERER_COMPONENT / "p4_h264_decoder.cpp").read_text()
    presentation = (P4_RENDERER_COMPONENT / "p4_presentation.cpp").read_text()
    renderer_cpp = core + jpeg + h264 + presentation
    renderer_header = (P4_RENDERER_COMPONENT / "p4_video_renderer.h").read_text()
    renderer_config = (P4_RENDERER_COMPONENT / "__init__.py").read_text()
    adapter = (VOIP_COMPONENTS / "voip_stack" / "video_renderer_adapter.h").read_text()
    source_cpp = (H264_SOURCE_COMPONENT / "esp_h264_video_source.cpp").read_text()
    source_header = (H264_SOURCE_COMPONENT / "esp_h264_video_source.h").read_text()
    h264_package = (ROOT / "packages/voip/p4_video_h264.yaml").read_text()

    # Functional driver ownership and main-loop/lease behavior are exercised
    # by the canonical MIPI58 and Renderer42 gates. This composition contract
    # guards the repository boundary, not a second model of either pipeline.
    assert "public video_renderer::EncodedFrameRenderer" in renderer_header
    assert "static constexpr size_t surface_count_{1};" in renderer_header
    assert "kH264AccessUnitQueueDepth = 3" in renderer_header
    assert "kMaxAccessUnitBytes = 128 * 1024" in renderer_header
    assert "ulTaskNotifyTake(pdTRUE, portMAX_DELAY);" in renderer_cpp
    assert "xTaskNotifyGive(this->rx_task_handle_)" in renderer_cpp
    assert "vTaskDelay" not in renderer_cpp
    assert "xQueueCreateStatic(" in renderer_cpp
    assert "xQueueReceive(this->h264_au_queue_" in renderer_cpp
    assert "memcpy(slot.data, access_unit.data" not in renderer_cpp
    assert "jpeg_new_decoder_engine" in renderer_cpp
    assert "jpeg_decoder_process" in renderer_cpp
    assert "ESP_IMGFX_PIXEL_FMT_I420" in renderer_cpp
    assert "esp_imgfx_color_convert_process(" in renderer_cpp
    assert "PPA_SRM_ROTATION_ANGLE_0" in renderer_cpp
    assert "mipi_dsi::MipiDsi *display_" in renderer_header
    assert "rgb565_transform_ids" in renderer_config
    assert "draw_rgb565_transformed(" in renderer_cpp
    assert "ppa_do_scale_rotate_mirror" not in renderer_cpp
    assert "ppa_register_client" not in renderer_cpp
    assert "lv_image_create(" not in renderer_cpp
    assert "lv_image_set_src(" not in renderer_cpp
    assert "draw_pixels_at" not in renderer_cpp
    assert "LV_EVENT_REFR_READY" in renderer_cpp
    loop = _renderer_method(presentation, "loop")
    assert "generation ==" in loop
    assert loop.index("const bool page_active") < loop.index("this->present_surface_direct_(pending)")
    assert loop.index("this->present_surface_direct_(pending)") < loop.index("this->pending_surface_.compare_exchange_strong")
    refresh = _renderer_method(presentation, "on_display_refresh_ready_")
    assert "this->enable_loop_soon_any_context();" in refresh
    assert "draw_rgb565_transformed" not in refresh
    worker = _renderer_method(core, "rx_task_")
    assert "draw_rgb565_transformed" not in worker
    assert "present_surface_direct_" not in worker
    prepare = _renderer_method(core, "prepare")
    assert "allocate_session_resources_()" not in prepare
    assert "start_rx_task_()" not in prepare
    assert "this->allocate_session_resources_()" in _renderer_method(core, "setup")
    stop = _renderer_method(core, "stop")
    assert "this->rx_session_prepared_.store(false" in stop
    assert "stop_rx_task_()" not in stop
    assert "free_codec_resources_()" not in stop
    decode = _renderer_method(h264, "decode_h264_access_unit_")
    consume = _renderer_method(h264, "consume_h264_nal_")
    assert consume.index("esp_h264_dec_process(") < consume.index("input.raw_data.buffer += input.consume")
    assert decode.index("this->consume_h264_nal_(") < decode.index("cadence_.accept(output.pts)") < decode.index("this->render_i420_(")
    assert "this->cadence_.accept(unit.timestamp_90khz)" in adapter
    assert "h264_profile_level_id" in adapter
    assert "display_id: main_display" in h264_package

    assert "class EspH264VideoSource" in source_header
    assert "public esp_video_camera::RawVideoFrameConsumer" in source_header
    assert "ESP_H264_RAW_FMT_O_UYY_E_VYY" in source_cpp
    assert "PPA_SRM_COLOR_MODE_YUV420" in source_cpp
    assert "RawVideoPixelFormat::YUV420_OUYY_EVYY" in source_cpp
    assert "h264_profile_level_id_from_annex_b" in source_cpp
    assert "h264_same_subprofile" in source_cpp
    transform_start = source_cpp.index(
        "bool EspH264VideoSource::transform_to_encoder_yuv_("
    )
    transform_end = source_cpp.index(
        "bool EspH264VideoSource::encode_frame_(", transform_start
    )
    transform = source_cpp[transform_start:transform_end]
    assert "((frame.width - input_block_width) / 2U) & ~1U;" in transform
    assert "((frame.height - input_block_height) / 2U) & ~1U;" in transform
    assert "config.scale_x = scale;" in transform
    assert "config.scale_y = scale;" in transform
    assert "config.scale_x = 1.0f;" not in transform
    assert "kPpaYuv420ScaleStepUnits = 2U" in source_cpp
    assert "input_block_width * scale_units / kPpaScaleUnits ==" in transform
    assert "input_block_height * scale_units / kPpaScaleUnits ==" in transform
    profile_gate = source_cpp.index("if (this->profile_level_id_.empty())")
    profile_parse = source_cpp.index("h264_profile_level_id_from_annex_b")
    assert profile_gate < profile_parse
    assert "p4_video_renderer H.264 requires display_id" in renderer_config
    assert "rx_au_copy_max_us_" in renderer_header
    assert "copy_total_bytes=%llu" in renderer_cpp
    assert "esp_h264_enc_set_bitrate" in source_cpp
    assert "__wrap_esp_h264_malloc_prefer" not in source_cpp
    assert "capability.max_bitrate_bps" in source_cpp
    assert "ppa_unregister_client(this->ppa_)" in source_cpp
    source_setup = source_cpp[
        source_cpp.index("void EspH264VideoSource::setup()") :
        source_cpp.index("void EspH264VideoSource::on_shutdown()")
    ]
    assert "ppa_unregister_client(this->ppa_)" in source_setup
    consume_start = source_cpp.index(
        "void EspH264VideoSource::consume_raw_video_frame("
    )
    source_consume = source_cpp[
        consume_start : source_cpp.index(
            "bool EspH264VideoSource::start_tx_task_()", consume_start
        )
    ]
    assert "this->init_ppa_()" in source_setup
    assert "this->init_ppa_()" not in source_consume
    assert "p4_video_ppa::Guard" not in source_cpp
    assert "p4_video_ppa::Guard" not in renderer_cpp
    assert "p4_video_workload" not in source_cpp
    assert "p4_video_workload" not in renderer_cpp
    assert "if (this->ppa_ == nullptr) return;" in source_consume
    assert "ppa_rotation_for_clockwise(frame.rotation_degrees)" in source_cpp
    assert "this->h264_optimized_yuv_bytes_()" in renderer_cpp
    assert "H.264 decode failure: error=%d" in renderer_cpp
    assert "ulTaskNotifyTake(pdTRUE, portMAX_DELAY);" in source_cpp
    assert "vTaskDelay" not in source_cpp
    assert "request_key_frame()" in source_cpp
    assert "voip_stack::RtpFrameCadence90k cadence_{};" in source_header
    assert "this->cadence_.accept(frame.timestamp_90khz)" in source_cpp
    assert "static constexpr size_t kEncodedBufferBytes = 64 * 1024;" in (
        source_header
    )
    assert "struct TxSlot" in source_header
    assert "TxSlot tx_slots_[2]{};" in source_header
    assert "uint32_t sequence{0};" in source_header
    assert "frame_queue_" not in source_cpp
    assert "void EspH264VideoSource::tx_task_()" in source_cpp
    assert "start_managed_pinned_task(" in source_cpp
    assert "candidate.sequence - oldest_sequence" in source_cpp
    assert source_consume.index("this->cadence_.accept(") < (
        source_consume.index("this->transform_to_encoder_yuv_(")
    )
    assert "convert_avg_us=%u convert_max_us=%u" in source_cpp
    assert "encode_avg_us=%u encode_max_us=%u encoded_max_bytes=%u" in (
        source_cpp
    )
    request_key_frame = source_cpp[
        source_cpp.index("void EspH264VideoSource::request_key_frame()") :
        source_cpp.index("bool EspH264VideoSource::init_ppa_()")
    ]
    assert "xTaskNotifyGive" not in request_key_frame
    encode_frame = source_cpp[
        source_cpp.index("bool EspH264VideoSource::encode_frame_(") :
        source_cpp.index("void EspH264VideoSource::consume_raw_video_frame(")
    ]
    assert "xSemaphoreTake(this->control_mutex_, portMAX_DELAY)" in encode_frame
    assert encode_frame.index(
        "xSemaphoreTake(this->control_mutex_, portMAX_DELAY)"
    ) < encode_frame.index("this->callback_(this->callback_ctx_, access_unit);")
    assert encode_frame.index("this->callback_(this->callback_ctx_, access_unit);") < (
        encode_frame.index("xSemaphoreGive(this->control_mutex_)")
    )
    assert "this->tx_active_.load(std::memory_order_acquire)" in encode_frame
    assert "slot->state.store(2, std::memory_order_release);" in source_consume
    assert "PPA_TRANS_MODE_BLOCKING" in source_cpp
    assert "PPA_TRANS_MODE_NON_BLOCKING" not in source_cpp
    assert "ppa_done_" not in source_header
    assert "pending_yuv_" not in source_header
    source_start = source_cpp[
        source_cpp.index("bool EspH264VideoSource::start_video(") :
        source_cpp.index("void EspH264VideoSource::stop_video()")
    ]
    assert "this->restart_encoder_()" not in source_start
    assert (
        "this->force_idr_generation_.store(\n"
        "      generation, std::memory_order_release);"
        in source_start
    )
    assert "this->encode_frame_(" not in source_consume
    assert "generation ==" in encode_frame
    assert "this->tx_generation_.load(std::memory_order_acquire)" in encode_frame
    assert "this->start_tx_task_()" in source_setup
    assert "this->start_tx_task_()" not in source_start
    source_stop = source_cpp[
        source_cpp.index("void EspH264VideoSource::stop_video()") :
        source_cpp.index("void EspH264VideoSource::request_key_frame()")
    ]
    source_stop_request = source_cpp[
        source_cpp.index("bool EspH264VideoSource::request_video_stop_()") :
        source_cpp.index("void EspH264VideoSource::stop_video()")
    ]
    assert "this->tx_active_.exchange(false" in source_stop_request
    assert "this->tx_generation_.fetch_add(1" in source_stop_request
    assert "this->camera_->stop_raw_frame_consumer(this)" in source_stop_request
    assert "this->close_encoder_();" not in source_stop
    assert "ppa_unregister_client(this->ppa_)" not in source_stop
    assert "this->callback_(this->callback_ctx_, access_unit);" in encode_frame
    tx_task_start = source_cpp.index("void EspH264VideoSource::tx_task_()")
    tx_task = source_cpp[
        tx_task_start : source_cpp.index("\n}  // namespace", tx_task_start)
    ]
    assert "xSemaphoreTake(this->control_mutex_" not in tx_task
    assert "this->encode_frame_(" in tx_task
    assert source_stop.index("this->request_video_stop_()") < (
        source_stop.index("this->wait_for_tx_idle_()")
    )
    callback_revoke = source_stop.index("this->callback_ = nullptr;")
    assert source_stop.index("this->wait_for_tx_idle_()") < callback_revoke
    assert "this->stop_tx_task_()" not in source_stop
    assert "xSemaphoreTake(this->tx_idle_, 0)" in source_stop_request
    assert "this->callback_ctx_ = nullptr;" in source_stop
    shutdown = source_cpp[
        source_cpp.index("void EspH264VideoSource::on_shutdown()") :
        source_cpp.index("void EspH264VideoSource::dump_config()")
    ]
    assert shutdown.index("this->request_video_stop_();") < shutdown.index(
        "if (!this->stop_tx_task_()) return;"
    ) < shutdown.index("this->finish_shutdown_();")
    finish_shutdown = source_cpp[
        source_cpp.index("void EspH264VideoSource::finish_shutdown_()") :
        source_cpp.index("void EspH264VideoSource::dump_config()")
    ]
    # The completed worker and every outstanding camera/encoded owner gate all
    # resource destruction. A timeout must not fall through into cleanup.
    owner_guard = finish_shutdown.index("!this->tx_slots_idle_()")
    assert finish_shutdown.index("!this->reap_tx_task_(0)") < owner_guard
    guarded_return = finish_shutdown.index("return;", owner_guard)
    for cleanup in ("this->close_encoder_();", "this->free_resources_();",
                    "ppa_unregister_client(this->ppa_)"):
        assert guarded_return < finish_shutdown.index(cleanup)


def test_p4_video_boundaries_fail_closed() -> None:
    jpeg_source = (JPEG_SOURCE_COMPONENT / "esp_jpeg_video_source.cpp").read_text()
    h264_config = (H264_SOURCE_COMPONENT / "__init__.py").read_text()
    h264_source = (H264_SOURCE_COMPONENT / "esp_h264_video_source.cpp").read_text()
    renderer_config = (P4_RENDERER_COMPONENT / "__init__.py").read_text()
    core = (P4_RENDERER_COMPONENT / "p4_video_renderer.cpp").read_text()
    jpeg = (P4_RENDERER_COMPONENT / "p4_jpeg_decoder.cpp").read_text()
    h264_decoder = (P4_RENDERER_COMPONENT / "p4_h264_decoder.cpp").read_text()
    presentation = (P4_RENDERER_COMPONENT / "p4_presentation.cpp").read_text()

    assert "capability.width == this->width_" in jpeg_source
    assert "capability.height == this->height_" in jpeg_source
    assert "capability.max_fps == 0" in jpeg_source
    assert "this->active_bitrate_.store(this->bitrate_" in h264_source
    for source in (h264_config, renderer_config):
        assert "only_on_variant(supported=[VARIANT_ESP32P4])" in source
    submit = _renderer_method(core, "submit_frame")
    h264, jpeg_submit = submit.split("#else", 1)
    assert "access_unit.release_callback == nullptr" in h264
    rejected_queue = h264.split("if (xQueueSend", 1)[1].split("if (access_unit.key_frame)", 1)[0]
    assert "slot.access_unit = {};" in rejected_queue
    assert "release()" not in rejected_queue
    assert "access_unit.size > kMaxAccessUnitBytes" in h264
    assert "access_unit.size > this->rx_au_capacity_" in jpeg_submit
    assert "memcpy(this->rx_au_, access_unit.data, access_unit.size)" in jpeg_submit
    assert "access_unit.release();" in jpeg_submit
    assert "required > this->surface_capacity_bytes_" in jpeg
    assert "width <= this->max_decode_width_" in h264_decoder
    assert "macroblocks_w * macroblocks_h <= kH264Level30MaxMacroblocks" in h264_decoder
    assert re.search(r"surface_layout_size_\[pending\]\s*==\s*this->layout_size_", presentation)
    assert "session_generation !=" in h264_decoder


def test_native_full_and_audio_stack_full_have_reciprocal_pcm_formats() -> None:
    def formats(path, direction):
        audio = profile(path)['voip_stack']['audio']
        return {(f['sample_rate'], f['pcm_format'], f['channels'], f['frame_ms'])
                for codec in audio['codecs'] if codec['codec'] == 'pcm'
                for f in codec.get(direction, [])}
    native = 'full-experience/esphome-native/generic-s3-full-esphome-native.yaml'
    other = 'full-experience/single-bus/waveshare-s3-full-afe.yaml'
    assert formats(native, 'tx') & formats(other, 'rx')
    assert formats(other, 'tx') & formats(native, 'rx')
