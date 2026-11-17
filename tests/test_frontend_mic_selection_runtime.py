"""Explicit microphone acquisition replaces the page's passive permission cache."""

from pathlib import Path
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.js_runtime
ENGINE = Path(__file__).resolve().parents[1] / "custom_components/voip_stack/frontend/voip-stack-engine.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_successful_current_microphone_selection_replaces_negative_and_pending_probe():
    source = ENGINE.read_text()
    methods = []
    for name in ("_openAudioInput", "setMediaDevice", "_selectMicrophone", "checkAutoAnswerMedia",
                 "autoAnswerMediaResult", "recordAutoAnswerMedia"):
        start = source.find("  async " + name + "(")
        if start < 0:
            start = source.index("  " + name + "(")
        methods.append(source[start:source.index("\n  }", start) + 4])
    script = r'''
import assert from 'node:assert/strict';
const Engine = class { METHODS };
const streams = new Map();
let acquire;
Object.defineProperty(globalThis, 'navigator', {value: {mediaDevices: {
  getUserMedia: constraints => acquire(constraints.audio.deviceId.exact),
}}, configurable: true});
function stream(id) {
  const track = {stopped: false, getSettings: () => ({deviceId: id}), stop() {this.stopped = true;}};
  const value = {getAudioTracks: () => [track], getTracks: () => [track]};
  streams.set(id, track);
  return value;
}
function engine() {
  const value = new Engine();
  Object.assign(value, {_autoAnswerMedia: new Map(), _microphoneSwitchGeneration: 0,
    _audioContext: null, _captureNode: null, _emit() {},
    _audioCaptureConstraints: id => ({deviceId: {exact: id}}),
    _mediaDevices: {commit(_kind, id) {this.selected = id;}, refresh: async () => {}},
  });
  value.recordAutoAnswerMedia('audio', false);
  return value;
}
acquire = async id => stream(id);
const selected = engine();
await selected.setMediaDevice('audioinput', 'chosen');
assert.equal((await selected.checkAutoAnswerMedia('audio')).ready, true);
assert.equal(streams.get('chosen').stopped, true);
assert.equal(selected._mediaDevices.selected, 'chosen');
assert.equal(Object.hasOwn(selected, '_autoAnswer'), false);

const pending = engine();
pending._autoAnswerMedia.clear();
let finishProbe;
pending._probeAutoAnswerMedia = () => new Promise(resolve => {finishProbe = resolve;});
const oldProbe = pending.checkAutoAnswerMedia('audio');
await pending.setMediaDevice('audioinput', 'during-probe');
finishProbe({ready: false});
await oldProbe;
assert.equal((await pending.checkAutoAnswerMedia('audio')).ready, true);

const failed = engine();
acquire = async () => {throw new Error('denied');};
await assert.rejects(failed.setMediaDevice('audioinput', 'denied'), /denied/);
assert.equal(failed.autoAnswerMediaResult('audio').ready, false);
assert.equal(failed._mediaDevices.selected, undefined);

const concurrent = engine();
let releaseOld;
acquire = async id => id === 'old' ? new Promise(resolve => {releaseOld = () => resolve(stream(id));}) : stream(id);
const obsolete = concurrent.setMediaDevice('audioinput', 'old');
await concurrent.setMediaDevice('audioinput', 'new');
releaseOld();
await assert.rejects(obsolete, /superseded/);
assert.equal(concurrent._mediaDevices.selected, 'new');
assert.equal(concurrent.autoAnswerMediaResult('audio').ready, true);
assert.equal(streams.get('old').stopped, true);
'''.replace("METHODS", "\n".join(methods))
    result = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
