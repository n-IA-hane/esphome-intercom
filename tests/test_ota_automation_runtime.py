"""Run the composed OTA automation on ESPHome's real host scheduler/scripts.

Only hardware actions are substituted. The blocking OTA data phase is modeled
on the main thread, as in ESPHome's actual OTA server, to expose stale waits.
"""
import os
import subprocess
import sys

import pytest
from esphome import yaml_util
from esphome.core import Lambda

from tests.native_profile import profile


@pytest.fixture(scope='module')
def ota_host_binary(tmp_path_factory):
    work = tmp_path_factory.mktemp('ota-native-host')
    device = profile('full-experience/single-bus/spotpear-ball-v2-full-afe.yaml')
    ota = next(item for item in device['ota'] if item['platform'] == 'esphome')

    def convert(value):
        if isinstance(value, list):
            return [convert(v) for v in value]
        if not isinstance(value, dict):
            return value
        if 'esp_audio_stack.is_idle' in value:
            return {'lambda': Lambda('return id(audio_idle);')}
        for key, body in value.items():
            if key == 'lvgl.pause':
                return {'lambda': Lambda('id(display_paused)=true; ++id(pause_count);')}
            if key == 'lvgl.resume':
                return {'lambda': Lambda('id(display_paused)=false; ++id(resume_count);')}
            if key == 'speaker.stop':
                speaker_id = body.get('id') if isinstance(body, dict) else body
                return {'lambda': Lambda('id(speaker_owned)=false;' if speaker_id == 'hw_speaker'
                                         else '++id(input_stops);')}
            if key == 'esp_audio_stack.stop':
                return {'lambda': Lambda('id(invalid_stop) |= id(speaker_owned); id(audio_idle)=false;')}
            if key == 'esp_audio_stack.start':
                return {'lambda': Lambda('id(audio_idle)=false; ++id(audio_starts);')}
            if key in ('script.execute', 'script.stop'):
                name = body.get('id') if isinstance(body, dict) else body
                if not name.startswith('firmware_'):
                    return {'lambda': Lambda('(void)0;')}
            if key.startswith(('media_player.', 'voice_assistant.', 'micro_wake_word.', 'voip_stack.', 'runtime_controller.')):
                return {'lambda': Lambda('(void)0;')}
            if key == 'lambda':
                return {'lambda': Lambda(str(body))}
        return {k: convert(v) for k, v in value.items()}

    scripts = [convert(script) for script in device['script'] if script['id'].startswith('firmware_')]
    for name, actions in [('test_begin', ota['on_begin']), ('test_error', ota['on_error'])]:
        scripts.append({'id': name, 'mode': 'parallel', 'then': convert(actions.get('then', actions) if isinstance(actions, dict) else actions)})
    setup = r'''
const char *scenario=std::getenv("OTA_SCENARIO");
if (scenario && std::strcmp(scenario,"before_begin")==0) {
  id(test_error).execute();
} else {
  id(test_begin).execute();
  // The production OTA data transfer blocks the ESPHome main loop.
  std::this_thread::sleep_for(std::chrono::milliseconds(2200));
  id(test_error).execute();
  if (scenario && std::strcmp(scenario,"duplicate_error")==0) id(test_error).execute();
  if (scenario && std::strcmp(scenario,"new_attempt")==0) id(test_begin).execute();
}
'''
    check = r'''
const char *scenario=std::getenv("OTA_SCENARIO");
bool ok=false;
if (scenario && std::strcmp(scenario,"before_begin")==0) {
  ok=!id(display_paused) && id(audio_starts)==0 && id(resume_count)==0;
} else if (scenario && std::strcmp(scenario,"new_attempt")==0) {
  ok=id(display_paused) && id(g_ota_in_progress) && id(audio_starts)==1;
} else {
  ok=!id(display_paused) && !id(g_ota_in_progress) && id(audio_starts)==1;
}
std::printf("paused=%d active=%d starts=%d pauses=%d resumes=%d\n",
 int(id(display_paused)),int(id(g_ota_in_progress)),id(audio_starts),id(pause_count),id(resume_count));
std::exit(ok && !id(invalid_stop) &&
          ((scenario && std::strcmp(scenario,"before_begin")==0) || id(input_stops)>0) ? 0 : 42);
'''
    document = {
        'esphome': {'name': 'ota-host-regression', 'includes': ['<cstdio>', '<cstdlib>', '<cstring>', '<thread>', '<chrono>'],
                    'on_boot': {'priority': -100, 'then': [{'lambda': Lambda(setup)}, {'delay': '500ms'}, {'lambda': Lambda(check)}]}},
        'host': {}, 'logger': {'level': 'ERROR', 'boot_log_level': 'ERROR'},
        'globals': [{'id': name, 'type': kind, 'initial_value': initial} for name, kind, initial in (
            ('g_ota_in_progress', 'bool', 'false'), ('audio_idle', 'bool', 'false'),
            ('display_paused', 'bool', 'false'), ('audio_starts', 'int', '0'),
            ('pause_count', 'int', '0'), ('resume_count', 'int', '0'),
            ('speaker_owned', 'bool', 'true'), ('invalid_stop', 'bool', 'false'),
            ('input_stops', 'int', '0'))],
        'script': scripts,
    }
    config = work / 'device.yaml'
    config.write_text(yaml_util.dump(document))
    result = subprocess.run([sys.executable, '-m', 'esphome', 'compile', str(config)],
                            capture_output=True, text=True, timeout=150)
    assert result.returncode == 0, result.stdout + result.stderr
    candidates = [p for name in ('program', 'firmware.elf') for p in work.rglob(name)
                  if p.is_file() and os.access(p, os.X_OK)]
    assert len(candidates) == 1, candidates
    return candidates[0]


@pytest.mark.parametrize('scenario', ['abort', 'before_begin', 'new_attempt', 'duplicate_error'])
@pytest.mark.timeout(180)
def test_ota_cancellation_owns_pending_automation(ota_host_binary, scenario):
    result = subprocess.run([str(ota_host_binary)], env={**os.environ, 'OTA_SCENARIO': scenario},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
