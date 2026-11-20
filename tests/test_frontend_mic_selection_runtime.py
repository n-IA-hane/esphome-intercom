"""Explicit microphone acquisition replaces the page's passive permission cache."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.js_runtime
ENGINE = Path(__file__).resolve().parents[1] / "custom_components/voip_stack/frontend/voip-stack-engine.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_successful_current_microphone_selection_replaces_negative_and_pending_probe():
    script = r'''
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const storage = () => {
  const values = new Map();
  return {getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key)};
};
globalThis.localStorage = storage();
globalThis.sessionStorage = storage();
globalThis.window = {location: {protocol: 'https:', host: 'ha.example'}, addEventListener() {},
  setTimeout, clearTimeout, setInterval, clearInterval};
const streams = new Map();
let acquire;
Object.defineProperty(globalThis, 'navigator', {value: {mediaDevices: {
  getUserMedia: constraints => acquire(constraints.audio?.deviceId?.exact || ""),
}}, configurable: true});
function stream(id) {
  const track = {stopped: false, getSettings: () => ({deviceId: id}), stop() {this.stopped = true;}};
  const value = {getAudioTracks: () => [track], getTracks: () => [track]};
  streams.set(id, track);
  return value;
}
const {voipStackEngine} = await import(pathToFileURL(ENGINE));
function engine() {
  globalThis.localStorage = storage();
  const value = new voipStackEngine.constructor();
  value.recordAutoAnswerMedia('audio', false);
  return value;
}
acquire = async id => stream(id);
const selected = engine();
await selected.setMediaDevice('audioinput', 'chosen');
assert.equal((await selected.checkAutoAnswerMedia('audio')).ready, true);
assert.equal(streams.get('chosen').stopped, true);
assert.equal(selected._mediaDevices.preference('audioinput'), 'chosen');
assert.equal(Object.hasOwn(selected, '_autoAnswer'), false);

const pending = engine();
pending.resetAutoAnswerMedia('audio');
let finishProbe;
acquire = id => id ? Promise.resolve(stream(id)) : new Promise((_resolve, reject) => {
  finishProbe = () => reject(new Error('probe denied'));
});
const oldProbe = pending.checkAutoAnswerMedia('audio');
while (!finishProbe) await new Promise(resolve => setImmediate(resolve));
await pending.setMediaDevice('audioinput', 'during-probe');
finishProbe();
assert.equal((await oldProbe).ready, false);
assert.equal((await pending.checkAutoAnswerMedia('audio')).ready, true);

const failed = engine();
acquire = async () => {throw new Error('denied');};
await assert.rejects(failed.setMediaDevice('audioinput', 'denied'), /denied/);
assert.equal(failed.autoAnswerMediaResult('audio').ready, false);
assert.equal(failed._mediaDevices.preference('audioinput'), '');

const concurrent = engine();
let releaseOld;
acquire = async id => id === 'old' ? new Promise(resolve => {releaseOld = () => resolve(stream(id));}) : stream(id);
const obsolete = concurrent.setMediaDevice('audioinput', 'old');
await concurrent.setMediaDevice('audioinput', 'new');
releaseOld();
await assert.rejects(obsolete, /superseded/);
assert.equal(concurrent._mediaDevices.preference('audioinput'), 'new');
assert.equal(concurrent.autoAnswerMediaResult('audio').ready, true);
assert.equal(streams.get('old').stopped, true);
'''
    result = subprocess.run(["node", "--input-type=module", "-e", f"const ENGINE={json.dumps(str(ENGINE))};\n" + script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
